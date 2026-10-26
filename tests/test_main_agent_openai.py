import base64
import struct

import pytest
from openai.types.realtime.realtime_error_event import RealtimeErrorEvent
from openai.types.realtime.response_audio_delta_event import ResponseAudioDeltaEvent
from openai.types.realtime.response_done_event import ResponseDoneEvent
from openai.types.realtime.response_text_delta_event import ResponseTextDeltaEvent

from muca_server.agents.main_agent import MainAgentEvent
from muca_server.agents.openai.main_agent import (
    MAIN_TOKEN_LIMIT,
    MainAgentOpenAI,
    _looks_like_low_confidence_transcription,
    _strip_prompt_echo,
    log_usage,
    trim_context,
)
from muca_server.agents.options import MainAgentOptions


def make_agent(**kwargs) -> MainAgentOpenAI:
    return MainAgentOpenAI(
        options=MainAgentOptions(),
        client=object(),
        **kwargs,
    )


class FakeItems:
    def __init__(self, calls: list) -> None:
        self._calls = calls
        self.truncated: list[dict] = []
        self.deleted: list[str] = []

    async def create(self, *, item: dict) -> None:
        self._calls.append(item)

    async def delete(self, *, item_id: str) -> None:
        self.deleted.append(item_id)

    async def truncate(self, **kwargs) -> None:
        self.truncated.append(kwargs)


class FakeConversation:
    def __init__(self, calls: list) -> None:
        self.item = FakeItems(calls)


class FakeResponse:
    def __init__(self) -> None:
        self.created = 0
        self.params: list[dict | None] = []
        self.cancelled = 0

    async def create(self, *, response: dict | None = None) -> None:
        self.created += 1
        self.params.append(response)

    async def cancel(self) -> None:
        self.cancelled += 1


class FakeAudioBuffer:
    def __init__(self) -> None:
        self.appended: list[str] = []

    async def append(self, *, audio: str) -> None:
        self.appended.append(audio)


class FakeSession:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def update(self, *, session: dict) -> None:
        self.updates.append(session)


class FakeConnection:
    def __init__(self) -> None:
        self.item_calls: list = []
        self.conversation = FakeConversation(self.item_calls)
        self.response = FakeResponse()
        self.input_audio_buffer = FakeAudioBuffer()
        self.session = FakeSession()


class _Event:
    """A provider event with just the attributes _interpret reads."""

    def __init__(self, type: str, **fields) -> None:
        self.type = type
        for name, value in fields.items():
            setattr(self, name, value)


class _Obj:
    def __init__(self, **fields) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


def audio_delta(data: bytes) -> ResponseAudioDeltaEvent:
    return ResponseAudioDeltaEvent.model_construct(
        type="response.output_audio.delta",
        delta=base64.b64encode(data).decode("ascii"),
        content_index=0,
        event_id="e1",
        item_id="i1",
        output_index=0,
        response_id="r1",
    )


def text_delta(text: str) -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent.model_construct(
        type="response.output_text.delta",
        delta=text,
        content_index=0,
        event_id="e2",
        item_id="i1",
        output_index=0,
        response_id="r1",
    )


def response_done() -> ResponseDoneEvent:
    return ResponseDoneEvent.model_construct(
        type="response.done",
        event_id="e3",
        response=None,
    )


async def test_audio_deltas_reach_the_stream_audio_hook_as_float32():
    agent = make_agent()
    chunks = []

    async def record(chunk):
        chunks.append(chunk)

    agent.on_audio = record

    # PCM16 from the provider: silence, full scale, minus full scale.
    pcm16 = struct.pack("<3h", 0, 32767, -32767)
    await agent._interpret(audio_delta(pcm16))

    assert len(chunks) == 1
    assert struct.unpack("<3f", chunks[0].data) == (0.0, 1.0, -1.0)


async def test_text_deltas_reach_the_stream_text_hook():
    agent = make_agent()
    deltas = []

    async def record(text, is_final, *_):
        deltas.append((text, is_final))

    agent.on_text = record

    await agent._interpret(text_delta("Hello "))
    await agent._interpret(text_delta("there."))
    await agent._interpret(response_done())

    assert deltas == [
        ("Hello ", False),
        ("there.", False),
        ("", True),
    ]


async def test_speech_lifecycle_events_are_raised():
    agent = make_agent()
    events = []

    async def record(event):
        events.append(event)

    agent.on_event = record

    await agent._interpret(
        _speech_event("input_audio_buffer.speech_started"),
    )
    await agent._interpret(
        _speech_event("input_audio_buffer.speech_stopped"),
    )

    assert events == [
        MainAgentEvent.USER_SPEECH_STARTED,
        MainAgentEvent.USER_SPEECH_STOPPED,
    ]


async def test_a_provider_error_does_not_raise():
    agent = make_agent()

    await agent._interpret(
        RealtimeErrorEvent.model_construct(
            type="error",
            event_id="e4",
            error={"message": "boom", "type": "server_error"},
        )
    )


def _speech_event(event_type: str):
    class _Event:
        type = event_type

    return _Event()


def test_persona_and_task_are_combined_for_the_connect_time_instructions():
    agent = make_agent(persona="You are MULCA.", task_knowledge="Keep the peace.")

    instructions = agent._build_instructions()

    assert "You are MULCA." in instructions
    assert "Keep the peace." in instructions


async def test_a_client_session_update_replaces_only_the_instructions():
    agent = make_agent()
    agent._connection = FakeConnection()

    await agent.update_session(
        {"instructions": "You are Foxy.", "modalities": ["text", "audio"]},
    )
    # Nothing to replace: the session keeps what it has.
    await agent.update_session({"modalities": ["audio"]})

    assert agent._connection.session.updates == [
        {"type": "realtime", "instructions": "You are Foxy."},
    ]


async def test_trigger_response_pushes_per_turn_content_as_an_item_not_session_instructions():
    agent = make_agent()
    agent._connection = FakeConnection()

    await agent._trigger_response(
        max_tokens=None,
        instructions="Current directive:\nSay hello.",
        add_to_conversation=True,
    )

    [item] = agent._connection.item_calls
    assert item["id"].startswith("directive_") and len(item["id"]) <= 32
    assert {key: value for key, value in item.items() if key != "id"} == {
        "type": "message",
        "role": "system",
        "content": [
            {"type": "input_text", "text": "Current directive:\nSay hello."},
        ],
    }
    assert agent._connection.response.created == 1
    assert agent._connection.conversation.item.deleted == []


async def test_only_the_latest_directive_stays_in_the_session():
    agent = make_agent()
    agent._connection = FakeConnection()

    for text in ("First.", "Second."):
        await agent._trigger_response(
            max_tokens=None,
            instructions=text,
            add_to_conversation=True,
        )

    first, second = agent._connection.item_calls
    assert agent._connection.conversation.item.deleted == [first["id"]]
    assert second["id"] != first["id"]


async def test_past_the_token_limit_the_oldest_half_of_the_messages_is_deleted():
    agent = make_agent()
    agent._connection = FakeConnection()
    for item_id, item_type in [
        ("a", "message"),
        ("call", "function_call"),
        ("b", "message"),
        ("c", "message"),
        ("d", "message"),
    ]:
        await agent._interpret(
            _Event(
                "conversation.item.added",
                item=_Obj(id=item_id, type=item_type),
            )
        )

    at_limit = _Obj(usage=_Obj(input_tokens=MAIN_TOKEN_LIMIT))
    await trim_context(agent, at_limit, MAIN_TOKEN_LIMIT)
    assert agent._connection.conversation.item.deleted == []

    past_limit = _Obj(usage=_Obj(input_tokens=MAIN_TOKEN_LIMIT + 1))
    await trim_context(agent, past_limit, MAIN_TOKEN_LIMIT)
    assert agent._connection.conversation.item.deleted == ["a", "b"]
    assert agent._item_ids == ["c", "d"]

    await agent._interpret(_Event("conversation.item.deleted", item_id="c"))
    assert agent._item_ids == ["d"]


def test_usage_is_priced_per_modality_and_added_to_the_session_total():
    agent = make_agent(model="gpt-realtime-2.1-mini")
    usage = _Obj(
        input_token_details=_Obj(
            text_tokens=1_000_000,
            audio_tokens=1_000_000,
            cached_tokens_details=_Obj(text_tokens=500_000, audio_tokens=0),
        ),
        output_token_details=_Obj(text_tokens=0, audio_tokens=1_000_000),
    )

    cost = log_usage(agent, "gpt-realtime-2.1-mini", _Obj(usage=usage))

    # 0.5M text at $0.60, 0.5M cached text at $0.06, 1M audio at $10,
    # 1M audio out at $20.
    assert cost == pytest.approx(0.3 + 0.03 + 10 + 20)
    assert agent.cost_usd == pytest.approx(cost)
    assert log_usage(agent, "gpt-realtime-2.1-mini", _Obj(usage=None)) == 0


async def test_trigger_response_pushes_nothing_when_there_is_no_new_content():
    agent = make_agent()
    agent._connection = FakeConnection()

    await agent._trigger_response(
        max_tokens=None,
        instructions="",
        add_to_conversation=True,
    )

    assert agent._connection.item_calls == []
    assert agent._connection.response.created == 1


# ----------------------------------------------------------------------
# Audio, speech events and interruption (parity with MainAgentOpenAi.cs)
# ----------------------------------------------------------------------


def response_created(response_id="r1", modalities=("audio",)):
    return _Event(
        "response.created",
        response=_Obj(id=response_id, output_modalities=list(modalities)),
    )


def response_done_with(status: str):
    return _Event("response.done", response=_Obj(status=status))


def assistant_item_added(item_id="item-1"):
    return _Event(
        "response.output_item.added",
        item=_Obj(id=item_id, role="assistant"),
    )


def record_events(agent) -> list:
    events: list = []

    async def record(event) -> None:
        events.append(event)

    agent.on_event = record
    return events


async def test_speech_started_is_raised_once_on_the_first_audio_of_a_response():
    agent = make_agent()
    events = record_events(agent)

    await agent._interpret(response_created())
    await agent._interpret(audio_delta(struct.pack("<2h", 1, 2)))
    await agent._interpret(audio_delta(struct.pack("<2h", 3, 4)))

    assert events == [MainAgentEvent.AGENT_SPEECH_STARTED]


async def test_speech_stopped_follows_response_done_for_a_spoken_reply_only():
    agent = make_agent()
    events = record_events(agent)

    await agent._interpret(response_created(modalities=("text",)))
    await agent._interpret(text_delta("hi"))
    await agent._interpret(response_done())
    assert MainAgentEvent.AGENT_SPEECH_STOPPED not in events

    await agent._interpret(response_created("r2"))
    await agent._interpret(audio_delta(struct.pack("<2h", 1, 2)))
    await agent._interpret(response_done())
    assert events[-1] == MainAgentEvent.AGENT_SPEECH_STOPPED


async def test_client_audio_is_sent_to_the_provider_as_pcm16():
    agent = make_agent()
    agent._connection = FakeConnection()

    from muca_server.audio import AudioChunk

    await agent._send_audio_chunk(
        AudioChunk(data=struct.pack("<2f", 1.0, -1.0)),
    )

    sent = base64.b64decode(agent._connection.input_audio_buffer.appended[0])
    assert struct.unpack("<2h", sent) == (32767, -32767)


async def test_transcript_deltas_are_tagged_as_audio_and_final_carries_the_status():
    agent = make_agent()
    deltas = []

    async def record(text, is_final, modality, interrupted):
        deltas.append((text, is_final, modality, interrupted))

    agent.on_text = record

    await agent._interpret(response_created())
    await agent._interpret(
        _Event("response.output_audio_transcript.delta", delta="Hel"),
    )
    await agent._interpret(response_done_with("cancelled"))

    assert deltas == [
        ("Hel", False, "audio", False),
        ("", True, "audio", True),
    ]


async def test_user_transcripts_reach_the_transcript_hook():
    agent = make_agent()
    received = []

    async def record(item_id, transcript):
        received.append((item_id, transcript))

    agent.on_transcript = record

    await agent._interpret(
        _Event(
            "conversation.item.input_audio_transcription.completed",
            item_id="in-1",
            transcript="hello there",
        )
    )

    assert received == [("in-1", "hello there")]


def test_prompt_echo_is_cut_and_real_speech_kept():
    prompt = (
        'The conversation is in English or Italian, sometimes switching '
        'between the two. One participant is an AI assistant named '
        'MULCA, often addressed by that name, "assistant", or "bot", and '
        'woken with "MULCA start".'
    )

    # Confirmed live: the full prompt verbatim, and its first sentence
    # duplicated, are real transcripts an ASR model produced instead of
    # actual speech.
    assert _strip_prompt_echo(prompt, prompt) == ""
    assert _strip_prompt_echo(
        "The conversation is in English or Italian, sometimes switching "
        "between the two. The conversation is in English or Italian, "
        "sometimes switching between the two.",
        prompt,
    ) == ""

    # Found live, 19-25 Oct -- each one woke MULCA up ("MULCA start"):
    # wrapped, with punctuation moved, and after real speech.
    assert _strip_prompt_echo(
        'context: ###\nThe conversation is in English or Italian, sometimes '
        'switching between the two. One participant is an AI assistant '
        'named MULCA, often addressed by that name, "assistant", or "bot", '
        'and woken with "MULCA start".\n###',
        prompt,
    ) == ""
    assert _strip_prompt_echo(
        'context: the conversation is in English or Italian, sometimes '
        'switching between the two. One participant is an AI assistant '
        'named MULCA, often addressed by that name, "assistant", or "bot", '
        'and woken with "MULCA start".',
        prompt,
    ) == ""
    assert _strip_prompt_echo(
        "Ah, perché magari posso prendere dagli artefatti. The conversation "
        "is in English or Italian, sometimes switching between the two. One "
        "participant is an AI assistant named MULCA, often addressed by "
        'that name, "assistant," or "bot," and woken with "MULCA start."',
        prompt,
    ) == "Ah, perché magari posso prendere dagli artefatti."
    assert _strip_prompt_echo(
        "context: ###\nThe conversation is in English or Italian, sometimes "
        "switching between the two. Ciao Eero.",
        prompt,
    ) == "Ciao Eero."

    assert _strip_prompt_echo("what time is the meeting", prompt) == (
        "what time is the meeting"
    )
    assert _strip_prompt_echo("", prompt) == ""

    # Found live: a bare "MULCA." or "assistant" always shares words with
    # the prompt -- without a minimum run, genuine short wake-word attempts
    # were swallowed along with the real multi-sentence echo.
    for real in ("MULCA.", "MULCA", "assistant", "Hey MULCA", "MULCA start."):
        assert _strip_prompt_echo(real, prompt) == real


async def test_a_transcript_that_echoes_the_bias_prompt_is_discarded():
    # Found live: the ASR model occasionally hallucinates by echoing its
    # own vocabulary-bias prompt back instead of transcribing real speech
    # (little/no real signal to transcribe from). Treated the same as no
    # transcript at all -- conversation.py already handles that as noise,
    # not a real utterance -- rather than treating it as something someone
    # actually said.
    agent = make_agent()
    received = []

    async def record(item_id, transcript):
        received.append((item_id, transcript))

    agent.on_transcript = record

    await agent._interpret(
        _Event(
            "conversation.item.input_audio_transcription.completed",
            item_id="in-1",
            transcript=agent._transcription_prompt(),
        )
    )

    assert received == [("in-1", "")]


def test_low_confidence_detection_is_a_plain_average_over_logprobs():
    confident = [_Obj(logprob=-0.1), _Obj(logprob=-0.2), _Obj(logprob=-0.05)]
    unconfident = [_Obj(logprob=-2.0), _Obj(logprob=-3.5), _Obj(logprob=-1.8)]

    assert not _looks_like_low_confidence_transcription(confident)
    assert _looks_like_low_confidence_transcription(unconfident)

    # No signal either way -- not a reason to discard.
    assert not _looks_like_low_confidence_transcription(None)
    assert not _looks_like_low_confidence_transcription([])


async def test_a_low_confidence_transcript_is_discarded():
    # Found live: coherent, fluent sentences ("I'm reading a book.")
    # hallucinated from pure kitchen noise -- no relation to the bias
    # prompt at all, so _strip_prompt_echo alone wouldn't catch it.
    # -1.5 checked against a real live sample before re-enabling this:
    # across a full test session, the only confirmed-genuine utterance
    # under this cutoff was a wake-phrase attempt ("assistente"); every
    # other sub-threshold transcript was confirmed hallucinated.
    agent = make_agent()
    received = []

    async def record(item_id, transcript):
        received.append((item_id, transcript))

    agent.on_transcript = record

    await agent._interpret(
        _Event(
            "conversation.item.input_audio_transcription.completed",
            item_id="in-1",
            transcript="I'm reading a book.",
            logprobs=[_Obj(logprob=-3.0), _Obj(logprob=-2.5)],
        )
    )

    assert received == [("in-1", "")]


async def test_a_low_confidence_dismissal_attempt_is_not_discarded():
    # Found live: short, clipped "stop"/"shut up" commands are exactly the
    # kind of utterance that scores low confidence -- discarding them left
    # an engaged session with no way to close by voice at all.
    agent = make_agent()
    received = []

    async def record(item_id, transcript):
        received.append((item_id, transcript))

    agent.on_transcript = record

    await agent._interpret(
        _Event(
            "conversation.item.input_audio_transcription.completed",
            item_id="in-1",
            transcript="stop",
            logprobs=[_Obj(logprob=-3.0), _Obj(logprob=-2.5)],
        )
    )

    assert received == [("in-1", "stop")]


async def test_a_low_confidence_wake_attempt_is_discarded():
    # Unlike a dismissal: the ASR invents "assistant" boilerplate over
    # near-silence, and honoring it makes MULCA talk over the group.
    agent = make_agent()
    received = []

    async def record(item_id, transcript):
        received.append((item_id, transcript))

    agent.on_transcript = record

    await agent._interpret(
        _Event(
            "conversation.item.input_audio_transcription.completed",
            item_id="in-1",
            transcript="assistente",
            logprobs=[_Obj(logprob=-3.0), _Obj(logprob=-2.5)],
        )
    )

    assert received == [("in-1", "")]


async def test_a_confident_transcript_is_unaffected():
    agent = make_agent()
    received = []

    async def record(item_id, transcript):
        received.append((item_id, transcript))

    agent.on_transcript = record

    await agent._interpret(
        _Event(
            "conversation.item.input_audio_transcription.completed",
            item_id="in-1",
            transcript="what time is the meeting",
            logprobs=[_Obj(logprob=-0.1), _Obj(logprob=-0.2)],
        )
    )

    assert received == [("in-1", "what time is the meeting")]


async def test_a_transcript_with_no_logprobs_at_all_is_unaffected():
    # Not every provider response includes them -- no signal either way,
    # not a reason to discard.
    agent = make_agent()
    received = []

    async def record(item_id, transcript):
        received.append((item_id, transcript))

    agent.on_transcript = record

    await agent._interpret(
        _Event(
            "conversation.item.input_audio_transcription.completed",
            item_id="in-1",
            transcript="what time is the meeting",
        )
    )

    assert received == [("in-1", "what time is the meeting")]


async def test_speech_started_remembers_the_input_item():
    agent = make_agent()
    record_events(agent)

    await agent._interpret(
        _Event("input_audio_buffer.speech_started", item_id="in-7"),
    )

    assert agent.last_input_item_id == "in-7"


async def test_stop_speaking_cancels_and_truncates_the_assistant_item_at_playback():
    agent = make_agent()
    agent._connection = FakeConnection()
    record_events(agent)

    await agent._interpret(response_created())
    await agent._interpret(assistant_item_added("item-9"))
    # 24000 samples of PCM16 = 1000 ms generated.
    await agent._interpret(audio_delta(bytes(2 * 24_000)))
    agent.playback_time_ms = 400

    await agent.stop_speaking()

    assert agent._connection.response.cancelled == 1
    assert agent._connection.conversation.item.truncated == [
        {"item_id": "item-9", "content_index": 0, "audio_end_ms": 400},
    ]


async def test_stop_speaking_after_generation_still_truncates_but_does_not_cancel():
    agent = make_agent()
    agent._connection = FakeConnection()
    record_events(agent)

    await agent._interpret(response_created())
    await agent._interpret(assistant_item_added("item-9"))
    await agent._interpret(audio_delta(bytes(2 * 24_000)))
    await agent._interpret(response_done_with("completed"))
    agent.playback_time_ms = 5000  # past what was generated

    await agent.stop_speaking()

    assert agent._connection.response.cancelled == 0
    assert agent._connection.conversation.item.truncated == [
        {"item_id": "item-9", "content_index": 0, "audio_end_ms": 1000},
    ]


async def test_a_new_response_does_not_truncate_the_previous_one():
    agent = make_agent()
    agent._connection = FakeConnection()
    record_events(agent)

    await agent._interpret(response_created())
    await agent._interpret(assistant_item_added("item-9"))
    await agent._interpret(audio_delta(bytes(2 * 2400)))
    await agent._interpret(response_done_with("completed"))

    await agent.trigger_response(instructions="next")

    assert agent._connection.conversation.item.truncated == []


async def test_trigger_response_passes_modality_and_maps_unity_max_tokens():
    agent = make_agent()
    agent._connection = FakeConnection()

    await agent._trigger_response(
        max_tokens=2_147_483_647,
        instructions="",
        add_to_conversation=False,
        output_modality="text",
    )

    assert agent._connection.response.params == [
        {
            "output_modalities": ["text"],
            "max_output_tokens": "inf",
            "conversation": "none",
        }
    ]


async def test_a_late_cancel_error_is_not_reported_as_a_problem(caplog):
    agent = make_agent()

    await agent._interpret(
        RealtimeErrorEvent.model_construct(
            type="error",
            event_id="e9",
            error={"code": "response_cancel_not_active", "message": "x"},
        )
    )

    assert "provider error" not in caplog.text


def test_the_session_detects_and_transcribes_speech_but_never_answers_alone():
    config = make_agent()._session_config()
    turn_detection = config["audio"]["input"]["turn_detection"]

    assert turn_detection["create_response"] is False
    assert turn_detection["interrupt_response"] is False
    assert config["audio"]["input"]["transcription"]["model"]
    assert config["audio"]["input"]["format"] == {
        "type": "audio/pcm",
        "rate": 24_000,
    }


async def test_a_full_session_asks_for_its_memory_to_be_renewed_once():
    # docs/MEMORY_DESIGN.md: instead of the oldest half being dropped with
    # nothing in its place, the conversation renews the memory.
    agent = make_agent()
    agent._connection = FakeConnection()
    for item_id in ("a", "b", "c", "d"):
        await agent._interpret(
            _Event("conversation.item.added", item=_Obj(id=item_id, type="message"))
        )
    reported = []

    async def renew():
        reported.append(True)

    agent.on_context_full = renew
    full = _Obj(usage=_Obj(input_tokens=MAIN_TOKEN_LIMIT + 1))

    await trim_context(agent, full, MAIN_TOKEN_LIMIT)
    await trim_context(agent, full, MAIN_TOKEN_LIMIT)

    assert reported == [True]  # once, not every response
    assert agent._connection.conversation.item.deleted == []

    await agent.clear_context()
    assert agent._connection.conversation.item.deleted == ["a", "b", "c", "d"]
    assert agent._item_ids == []

    await trim_context(agent, full, MAIN_TOKEN_LIMIT)
    assert reported == [True, True]  # re-armed by the clear


async def test_a_renewal_that_never_comes_falls_back_to_dropping_half():
    agent = make_agent()
    agent._connection = FakeConnection()
    for item_id in ("a", "b", "c", "d"):
        await agent._interpret(
            _Event("conversation.item.added", item=_Obj(id=item_id, type="message"))
        )

    async def renew():
        pass  # never clears

    agent.on_context_full = renew
    await trim_context(
        agent, _Obj(usage=_Obj(input_tokens=MAIN_TOKEN_LIMIT + 1)), MAIN_TOKEN_LIMIT
    )
    overflowing = _Obj(usage=_Obj(input_tokens=int(MAIN_TOKEN_LIMIT * 1.5) + 1))
    await trim_context(agent, overflowing, MAIN_TOKEN_LIMIT)

    assert agent._connection.conversation.item.deleted == ["a", "b"]
