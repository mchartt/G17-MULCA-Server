"""
Spoken turns, end to end at the conversation level, with no provider.

The behaviours ported from the Unity package's SpeechToSpeechSession: a
spoken turn reaches cognition once transcribed, silence is an absence of
frames, the assistant's turn ends once playback drains (exactly once), and
a user talking over the assistant stops it.
"""

import asyncio
from uuid import uuid4

from muca_server.agents import (
    AgentCapabilities,
    AuxiliaryAgent,
    Conversation,
    MainAgent,
    MainAgentEvent,
    MainAgentOptions,
    MainAgentState,
)
from muca_server.agents.turn import Turn
from muca_server.audio import AudioChunk
from muca_server.cognition import Utterance
from muca_server.protocol import parse_incoming_message
from muca_server.sessions import SessionState

VOICE = AudioChunk(data=b"\x00\x00\x00\x3f" * 240).to_base64()


class ListeningMainAgent(MainAgent):
    """A main agent whose provider detects speech, as OpenAI's does."""

    def __init__(self, *, options: MainAgentOptions, vad: bool) -> None:
        super().__init__(options=options)
        self.capabilities = AgentCapabilities(vad=vad, interrupt=False)
        self.triggers: list[dict] = []
        self.stopped = 0

    async def _trigger_response(self, **kwargs) -> None:
        self.triggers.append(kwargs)

    async def stop_speaking(self) -> None:
        self.stopped += 1
        await super().stop_speaking()


class Factory:
    def __init__(self, *, vad: bool) -> None:
        self.vad = vad
        self.main: ListeningMainAgent | None = None

    def build_main(self, options):
        self.main = ListeningMainAgent(options=options, vad=self.vad)
        return self.main

    def build_auxiliary(self, options):
        return AuxiliaryAgent(options=options)


class RecordingPolicy:
    """Hears every utterance; answers each one."""

    def __init__(self) -> None:
        self.utterances: list[Utterance] = []
        self.untranscribed: list[tuple[int, str, int]] = []
        self.port = None

    async def start(self, port) -> None:
        self.port = port

    async def close(self) -> None:
        pass

    async def on_utterance(self, utterance: Utterance) -> bool:
        self.utterances.append(utterance)
        await self.port.instruct_main_agent("answer")
        return False

    async def on_intervention_signal(self, agent_guid, signal) -> None:
        pass

    async def on_assistant_output(self, text, *, interrupted) -> None:
        pass

    async def wait_for_turn_to_finish(self) -> None:
        pass

    def observer_catchup_text(self) -> str:
        return ""

    async def on_session_event(self, event, detail="", *, speaker="", data=None):
        pass

    def last_turn_strategy(self):
        return None

    async def on_untranscribed_turn(
        self,
        speaker_id: int,
        speaker_name: str,
        timestamp_ms: int,
    ) -> None:
        self.untranscribed.append((speaker_id, speaker_name, timestamp_ms))


def make_conversation(*, vad: bool = True, silence_timeout_ms: int = 400):
    state = SessionState(session_id=uuid4())
    state.is_connected = True
    outbound: asyncio.Queue = asyncio.Queue()
    factory = Factory(vad=vad)
    policy = RecordingPolicy()

    conversation = Conversation(
        state=state,
        outbound_queue=outbound,
        main_agent_options=MainAgentOptions(name="MULCA"),
        agent_factory=factory,
        policy=policy,
        silence_timeout_ms=silence_timeout_ms,
    )

    return conversation, factory, policy, outbound


def audio(speaker: int, *, voice: bool = True, notifying: bool = False):
    return parse_incoming_message(
        {
            "event": "audio_stream",
            "speaker": speaker if voice else -1,
            "audio": VOICE if voice else "",
            "isNotifying": notifying,
            "playbackTimeMs": 0,
        }
    )


async def drain(outbound: asyncio.Queue) -> list:
    messages = []
    while not outbound.empty():
        messages.append(outbound.get_nowait())
    return messages


def record_text(agent) -> list:
    received: list = []

    async def send(text: str, *, role: str = "system") -> None:
        received.append(text)

    agent._send_text = send
    return received


async def speak_one_turn(conversation, factory, *, speaker: int, item_id: str):
    """User audio, the provider's VAD confirming it, then silence."""

    await conversation.route_user_audio(audio(speaker))

    factory.main.last_input_item_id = item_id
    await conversation._handle_main_agent_event(
        MainAgentEvent.USER_SPEECH_STARTED,
    )
    await conversation.route_user_audio(audio(speaker))
    assert conversation.current_speaker == speaker

    await conversation._handle_main_agent_event(
        MainAgentEvent.USER_SPEECH_STOPPED,
    )
    await conversation.route_user_audio(audio(speaker, voice=False))
    assert conversation.current_speaker == -1


# ----------------------------------------------------------------------
# Spoken turns reach cognition
# ----------------------------------------------------------------------


async def test_a_transcribed_spoken_turn_reaches_the_policy_attributed():
    conversation, factory, policy, _ = make_conversation()
    await conversation.start()
    await conversation.route_participants(
        parse_incoming_message(
            {"event": "participants", "participants": [{"id": 5, "name": "Ada"}]},
        )
    )

    await speak_one_turn(conversation, factory, speaker=5, item_id="in-1")
    await conversation._handle_transcript("in-1", "  what time is it?  ")
    await conversation.wait_until_idle()

    assert [(u.speaker_id, u.speaker_name, u.content, u.modality)
            for u in policy.utterances] == [
        (5, "Ada", "what time is it?", "audio"),
    ]


async def test_transcript_listeners_get_each_spoken_turn_and_a_failing_one_changes_nothing():
    # The Group Coordinator keeps the call's transcript (router._on_voice).
    conversation, factory, policy, _ = make_conversation()
    heard = []

    async def listener(speaker, name, text, ended_at):
        heard.append((speaker, name, text))

    async def broken(*_):
        raise RuntimeError("boom")

    conversation.transcript_listeners += [broken, listener]
    await conversation.start()
    await conversation.route_participants(
        parse_incoming_message(
            {"event": "participants", "participants": [{"id": 5, "name": "Ada"}]},
        )
    )

    await speak_one_turn(conversation, factory, speaker=5, item_id="in-1")
    await conversation._handle_transcript("in-1", "  I'll print the survey  ")
    await conversation.wait_until_idle()

    assert heard == [(5, "Ada", "I'll print the survey")]
    assert [u.content for u in policy.utterances] == ["I'll print the survey"]


async def test_the_main_agent_also_gets_the_speaker_tagged_transcript():
    # Found live: unlike a text turn (which sends the main agent the exact
    # "speaker: text" line), a voice turn never did -- the main agent only
    # ever heard raw audio, with no durable per-speaker text record to
    # summarize from later, unlike the auxiliary/intervention agent, which
    # already got this. Same tagged line, same pattern, now for both.
    conversation, factory, policy, _ = make_conversation()
    await conversation.start()
    await conversation.route_participants(
        parse_incoming_message(
            {"event": "participants", "participants": [{"id": 5, "name": "Ada"}]},
        )
    )
    received = record_text(factory.main)

    await speak_one_turn(conversation, factory, speaker=5, item_id="in-1")
    await conversation._handle_transcript("in-1", "what time is it?")
    await conversation.wait_until_idle()

    assert "Ada: what time is it?" in received


async def test_an_untranscribed_turn_is_logged_and_observers_still_asked(
    monkeypatch,
):
    """
    Noise, or a provider transcription failure: no transcript ever arrives.
    Every other outcome gets an audit row (even "decided not to act") --
    this shouldn't be the one silent exception, and the observers (who
    already heard the raw audio) should still get a chance to judge.
    """

    import muca_server.agents.conversation as conversation_module

    monkeypatch.setattr(conversation_module, "TRANSCRIPT_WAIT_SECONDS", 0.01)

    conversation, factory, policy, _ = make_conversation()
    await conversation.start()
    await conversation.route_participants(
        parse_incoming_message(
            {"event": "participants", "participants": [{"id": 5, "name": "Ada"}]},
        )
    )

    await speak_one_turn(conversation, factory, speaker=5, item_id="in-1")
    # No _handle_transcript call -- nothing ever arrives for "in-1".
    await conversation.wait_until_idle()

    assert policy.utterances == []
    assert len(policy.untranscribed) == 1
    speaker_id, speaker_name, _timestamp_ms = policy.untranscribed[0]
    assert (speaker_id, speaker_name) == (5, "Ada")


async def test_a_spoken_turn_is_answered_in_audio_and_a_text_turn_in_text():
    conversation, factory, _, _ = make_conversation()
    await conversation.start()

    await speak_one_turn(conversation, factory, speaker=5, item_id="in-1")
    await conversation._handle_transcript("in-1", "hello")
    await conversation.wait_until_idle()

    await conversation.route_user_text(
        parse_incoming_message(
            {"event": "user_text", "speaker": 5, "text": "hi"},
        )
    )
    await conversation.wait_until_idle()

    assert [t["output_modality"] for t in factory.main.triggers] == [
        "audio",
        "text",
    ]


async def test_a_transcript_that_arrives_before_the_turn_closes_is_used():
    conversation, factory, policy, _ = make_conversation()
    await conversation.start()

    await conversation.route_user_audio(audio(5))
    factory.main.last_input_item_id = "in-1"
    await conversation._handle_main_agent_event(
        MainAgentEvent.USER_SPEECH_STARTED,
    )
    await conversation.route_user_audio(audio(5))
    await conversation._handle_transcript("in-1", "early")
    await conversation._handle_main_agent_event(
        MainAgentEvent.USER_SPEECH_STOPPED,
    )
    await conversation.route_user_audio(audio(5, voice=False))
    await conversation.wait_until_idle()

    assert [u.content for u in policy.utterances] == ["early"]


async def test_the_socket_is_not_blocked_while_a_turn_is_judged():
    conversation, _, policy, _ = make_conversation(vad=False)
    release = asyncio.Event()

    async def slow_wait() -> None:
        await release.wait()

    policy.wait_for_turn_to_finish = slow_wait
    await conversation.start()

    text = parse_incoming_message(
        {"event": "user_text", "speaker": 1, "text": "hi"},
    )

    # Queuing returns at once, and audio still flows while it is judged.
    await asyncio.wait_for(conversation.route_user_text(text), timeout=1)
    await asyncio.wait_for(conversation.route_user_audio(audio(2)), timeout=1)
    assert conversation.current_speaker == 2

    release.set()
    await asyncio.wait_for(conversation.wait_until_idle(), timeout=1)


# ----------------------------------------------------------------------
# Silence and playback, from the clock
# ----------------------------------------------------------------------


async def test_no_frames_for_the_silence_timeout_ends_the_users_turn():
    conversation, factory, _, _ = make_conversation(
        vad=False,
        silence_timeout_ms=50,
    )
    await conversation.start()

    await conversation.route_user_audio(audio(3))
    assert conversation.current_speaker == 3

    await asyncio.sleep(0.35)

    assert conversation.current_speaker == -1
    assert not conversation.turns[-1].is_open


async def test_the_assistants_turn_ends_exactly_once_after_playback_drains():
    conversation, factory, _, outbound = make_conversation(vad=False)
    await conversation.start()
    await conversation.create_auxiliary_agent(
        parse_incoming_message(
            {
                "event": "create_auxiliary_agent",
                "auxiliaryAgentType": "intervention_detection",
            }
        )
    )
    observer = next(iter(conversation.auxiliary_agents.values()))
    notices = record_text(observer)

    await conversation._handle_main_agent_event(
        MainAgentEvent.AGENT_SPEECH_STARTED,
    )
    await conversation._handle_main_agent_audio(
        AudioChunk(data=bytes(4 * 2400)),
    )
    await conversation.route_user_audio(audio(1, voice=False, notifying=True))
    await asyncio.sleep(0.01)
    assert conversation.main_agent_state is MainAgentState.ANSWERING

    # Generation ends while the client is still playing: the turn holds.
    await conversation._handle_main_agent_event(
        MainAgentEvent.AGENT_SPEECH_STOPPED,
    )
    await conversation.route_user_audio(audio(1, voice=False, notifying=True))
    await asyncio.sleep(0.01)
    assert conversation.is_answering

    # Playback drains; the client keeps reporting it, frame after frame.
    for _ in range(5):
        await conversation.route_user_audio(
            audio(1, voice=False, notifying=False),
        )
        await asyncio.sleep(0.01)

    assert conversation.main_agent_state is MainAgentState.INACTIVE
    assert not conversation.turns[-1].is_open
    assert sum("stopped speaking." in n for n in notices) == 1


async def test_without_client_reports_playback_is_estimated_from_audio_sent():
    conversation, factory, _, _ = make_conversation(
        vad=False,
        silence_timeout_ms=50,
    )
    await conversation.start()

    await conversation._handle_main_agent_event(
        MainAgentEvent.AGENT_SPEECH_STARTED,
    )
    # 200 ms of audio, and a client that never sends a frame.
    await conversation._handle_main_agent_audio(
        AudioChunk(data=bytes(4 * 4800)),
    )
    await asyncio.sleep(0.05)
    assert conversation.is_answering

    await conversation._handle_main_agent_event(
        MainAgentEvent.AGENT_SPEECH_STOPPED,
    )
    await asyncio.sleep(0.5)

    assert not conversation.is_answering
    assert conversation.assistant_spoke_last


async def test_spoken_audio_is_split_into_frames_a_unity_client_can_read():
    conversation, _, _, outbound = make_conversation()
    await conversation.start()
    await drain(outbound)

    await conversation._handle_main_agent_audio(
        AudioChunk(data=bytes(4 * 7000)),
    )

    frames = [m for m in await drain(outbound) if m.audio]
    assert len(frames) == 3
    assert all(
        len(AudioChunk.from_base64(m.audio).data) // 4 <= 3000
        for m in frames
    )


# ----------------------------------------------------------------------
# Barge-in
# ----------------------------------------------------------------------


async def test_a_user_talking_over_the_assistant_stops_and_clears_it():
    conversation, factory, _, outbound = make_conversation(vad=False)
    await conversation.start()
    conversation.main_agent_state = MainAgentState.ANSWERING
    conversation.turns.append(
        Turn(start_time=0, speaker_id=0, speaker_name="MULCA"),
    )
    await drain(outbound)

    await conversation.route_user_audio(audio(4))

    assert factory.main.stopped == 1
    assert conversation.main_agent_state is MainAgentState.INACTIVE
    assert any(m.clear_audio_queue for m in await drain(outbound)
               if m.event == "state_update")
