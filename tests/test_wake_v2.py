"""
"wake_v2": "wake" with the fixes from the 15-28 Oct exports. Plain "wake"
must behave exactly as before (test_listening_modes.py).
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import muca_server.agents.conversation as conversation_module
import muca_server.agents.openai.intervention_agent as intervention_module
from muca_server.agents import MainAgentOptions
from muca_server.agents.conversation import Conversation
from muca_server.agents.openai.intervention_agent import (
    SILENCE_TOOL_NAME,
    TOOL_NAME,
    InterventionAgentOpenAI,
)
from muca_server.agents.openai.main_agent import MainAgentOpenAI
from muca_server.agents.options import AuxiliaryAgentOptions
from muca_server.agents.turn import Turn
from muca_server.agents.voice_control_phrases import voice_command_v2
from muca_server.cognition import CoalaPolicy, InterventionSignal
from muca_server.cognition.loader import (
    WAKE_V2_CONFIG_DIR,
    load_persona,
    load_strategy_store,
)
from muca_server.protocol import parse_incoming_message
from muca_server.sessions import SessionState
from test_intervention_agent_openai import (
    make_agent,
    record_decisions,
    response_done_event,
    tool_call_event,
)
from test_listening_modes import (
    STRATEGIES_WITH_ALWAYS_REPLY,
    DecliningInterventionAgent,
    FakeInterventionAgent,
    FixedAgentFactory,
    ScriptedMainAgent,
    create_auxiliary,
    interrupted,
    make_conversation,
    typed,
    voice_turn,
)


class ObservedInterventionAgent(DecliningInterventionAgent):
    """Declines, and keeps what it was told."""

    def __init__(self, *, options) -> None:
        super().__init__(options=options)
        self.told: list[str] = []

    async def _send_text(self, text: str, *, role: str) -> None:
        self.told.append(text)


class CappedMainAgent(ScriptedMainAgent):
    def __init__(self, *, options) -> None:
        super().__init__(options=options)
        self.max_tokens: list[int | None] = []

    async def _trigger_response(self, *, max_tokens, **kwargs) -> None:
        self.max_tokens.append(max_tokens)
        await super()._trigger_response(max_tokens=max_tokens, **kwargs)


class CappedFactory(FixedAgentFactory):
    def build_main(self, options):
        self.main_options = options
        self.main = CappedMainAgent(options=options)
        return self.main

    def build_auxiliary(self, options):
        self.auxiliary_options = options
        return super().build_auxiliary(options)


def turn_line(told: list[str]) -> str:
    return next(text for text in reversed(told) if "Turn facts" in text)


async def v2(auxiliary_cls: type = DecliningInterventionAgent, *, mode: str = "wake_v2"):
    state = SessionState(session_id=uuid4())
    factory = CappedFactory(auxiliary_cls=auxiliary_cls)
    conversation = Conversation(
        state=state,
        outbound_queue=asyncio.Queue(),
        main_agent_options=MainAgentOptions(),
        agent_factory=factory,
        policy=CoalaPolicy(strategy_store=STRATEGIES_WITH_ALWAYS_REPLY),
        listening_mode=mode,
    )
    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())
    return conversation, factory


def test_commands_whichever_is_said_last_and_short_or_named():
    assert voice_command_v2("Grazie mille! MULCA start") == "wake"
    assert voice_command_v2("MULCA stop. MULCA start") == "wake"
    assert voice_command_v2("MULCA start. Thank you, MULCA.") == "dismiss"
    assert voice_command_v2("Wake up. Start MULCA") == "wake"
    assert voice_command_v2("MULCA go out") == "dismiss"
    assert voice_command_v2("Esci") == "dismiss"
    assert voice_command_v2("Aspetta.") == "hold"
    assert voice_command_v2("one second") == "hold"
    assert voice_command_v2("MULCA, non a te") == "hold"
    # Buried in a longer sentence between students: neither.
    assert voice_command_v2("Eh basta che cerchi il scenario e sta pagina") is None
    assert voice_command_v2("The problem is that if we talk she stop.") is None
    assert voice_command_v2("Va bene, aspetta che apro il file e lo leggiamo") is None
    assert voice_command_v2("we should look at the budget") is None


async def test_the_mode_is_wake_with_the_fixes_from_handshake_or_server():
    conversation, factory = await v2()
    assert conversation._listening_mode == "wake"
    assert conversation._v2 is True
    assert factory.auxiliary_options.listening_mode == "wake_v2"
    assert factory.main_options.listening_mode == "wake_v2"

    conversation, factory = await make_conversation(listening_mode="ambient")
    await conversation.connect_agent(
        parse_incoming_message({"event": "connect", "listeningMode": "wake_v2"})
    )
    assert conversation._listening_mode == "wake"
    assert conversation._v2 is True


async def test_a_bare_start_restarts_the_session_with_a_one_word_ack():
    for mode in ("wake", "wake_v2"):
        conversation, factory = await v2(mode=mode)
        await voice_turn(conversation, "muca start")
        # Only a repeat gets the one-word ack, not the start of a session.
        assert "Say one word" not in factory.main.instructions[-1]
        assert factory.main.max_tokens[-1] is None
        conversation._engaged_since_ms = 1
        conversation._interruption_score = 2

        await voice_turn(conversation, "muca start")

        assert conversation._engaged_since_ms > 1, mode
        assert conversation._interruption_score == 0
        assert factory.auxiliary.asked == 0  # forced, never judged
        assert "Say one word" in factory.main.instructions[-1]
        assert factory.main.max_tokens[-1] is not None


async def test_start_mulca_wakes_too_and_alone_has_no_question():
    assert not conversation_module._wake_phrase_has_question("Start MULCA.")
    assert conversation_module._wake_phrase_has_question("start muca, what is due")
    for mode in ("wake", "wake_v2"):
        conversation, factory = await v2(mode=mode)

        await voice_turn(conversation, "Start MULCA.")

        assert conversation._listening_window_open is True, mode
        assert "Briefly acknowledge" in factory.main.instructions[-1], mode


async def test_without_the_ack_a_bare_start_only_restarts(monkeypatch):
    monkeypatch.setattr(conversation_module, "RESTART_ACK", False)
    for mode in ("wake", "wake_v2"):
        conversation, factory = await v2(mode=mode)
        await voice_turn(conversation, "muca start")
        conversation._engaged_since_ms = 1

        await voice_turn(conversation, "muca start")

        assert conversation._engaged_since_ms > 1, mode
        assert len(factory.main.instructions) == 1
        assert factory.auxiliary.asked == 0


async def test_a_start_with_a_question_restarts_and_is_judged():
    for mode in ("wake", "wake_v2"):
        conversation, factory = await v2(mode=mode)
        await voice_turn(conversation, "muca start")
        conversation._engaged_since_ms = 1

        await voice_turn(conversation, "muca start, what is the deadline")

        assert conversation._engaged_since_ms > 1, mode
        assert factory.auxiliary.asked == 1, mode


async def test_the_follow_up_to_a_bare_start_is_judged_not_forced():
    conversation, factory = await v2(auxiliary_cls=ObservedInterventionAgent)

    await voice_turn(conversation, "muca start")
    await voice_turn(conversation, "what is the deadline")

    assert factory.auxiliary.asked == 1
    assert len(factory.main.instructions) == 1  # only the ack
    assert "probably what they woke it for" in turn_line(factory.auxiliary.told)


async def test_in_wake_the_follow_up_gets_only_the_hint_line():
    conversation, factory = await v2(auxiliary_cls=ObservedInterventionAgent, mode="wake")

    await voice_turn(conversation, "muca start")
    await voice_turn(conversation, "what is the deadline")
    await voice_turn(conversation, "and the budget", speaker=2)

    def line(words):
        return next(text for text in factory.auxiliary.told if words in text)

    hint = conversation_module.FOLLOWUP_HINT_WAKE
    assert factory.auxiliary.asked == 2
    assert len(factory.main.instructions) == 1  # only the ack
    assert line("what is the deadline").endswith(hint)
    assert hint not in line("and the budget")
    assert not any("Turn facts" in text for text in factory.auxiliary.told)


async def test_a_hold_answers_nothing_and_keeps_the_session():
    conversation, factory = await v2(auxiliary_cls=FakeInterventionAgent)
    await voice_turn(conversation, "muca start, what is the deadline")

    await voice_turn(conversation, "aspetta", speaker=2)

    assert conversation._listening_window_open is True
    assert conversation._unaddressed_turns == 0
    assert factory.auxiliary.asked == 0
    assert len(factory.main.instructions) == 1


async def test_a_thanks_before_the_start_wakes_and_a_long_basta_doesnt_end():
    conversation, factory = await v2()

    await voice_turn(conversation, "grazie, muca start")
    assert conversation._listening_window_open is True

    await voice_turn(conversation, "eh basta che cerchi lo scenario sulla pagina", speaker=2)
    assert conversation._listening_window_open is True

    await voice_turn(conversation, "muca go out")
    assert conversation._listening_window_open is False


async def test_the_turn_that_ends_the_session_is_not_answered():
    # Plain "wake" still judges it (asked once); "wake_v2" doesn't.
    for mode, asked in (("wake", 1), ("wake_v2", 0)):
        conversation, factory = await v2(auxiliary_cls=FakeInterventionAgent, mode=mode)
        await voice_turn(conversation, "muca start, what is the deadline")
        conversation._interruption_score = 2
        conversation.turns.append(Turn(start_time=-1_000, end_time=0, speaker_id=1))
        conversation._pending_interruption_ms = 500

        await voice_turn(conversation, "no no we were saying the budget is late")

        assert conversation._listening_window_open is False, mode
        assert factory.auxiliary.asked == asked, mode


async def test_longer_sessions_and_more_talk_overs():
    conversation, _ = await v2()
    await conversation._handle_wake_phrase(speaker=1)
    start = conversation._engaged_since_ms
    step = conversation_module.INTERRUPTION_THRESHOLD_STEP_MS

    assert conversation._interruption_threshold(start) == 3
    assert conversation._interruption_threshold(start + 2 * step) == 2

    for elapsed, still_open in (
        (conversation_module.ENGAGED_MAX_MS, True),
        (conversation_module.ENGAGED_MAX_MS_V2, False),
    ):
        conversation._last_speech_ms = start + elapsed  # not a quiet room
        await conversation._check_engaged_session(start + elapsed)
        assert conversation._listening_window_open is still_open

    await conversation._handle_wake_phrase(speaker=1)
    await interrupted(conversation, 500)
    await interrupted(conversation, 500)
    assert conversation._listening_window_open is True


async def test_an_answer_to_mulca_by_name_heard_out_restarts_the_session():
    for mode in ("wake", "wake_v2"):
        conversation, _ = await v2(auxiliary_cls=FakeInterventionAgent, mode=mode)
        await voice_turn(conversation, "muca start")
        conversation._engaged_since_ms = 1

        await voice_turn(conversation, "muca, what is the deadline")
        assert conversation._engaged_since_ms == 1, mode  # not until heard out

        await conversation._end_assistant_turn()
        assert conversation._engaged_since_ms > 1, mode


async def test_no_restart_when_talked_over_unnamed_or_unanswered():
    # Talked over.
    conversation, _ = await v2(auxiliary_cls=FakeInterventionAgent)
    await voice_turn(conversation, "muca start")
    conversation._engaged_since_ms = 1
    await voice_turn(conversation, "muca, what is the deadline")
    await conversation._interrupt_assistant_turn()
    await conversation._end_assistant_turn()
    assert conversation._engaged_since_ms == 1

    # A follow-up without the name.
    conversation, _ = await v2(auxiliary_cls=FakeInterventionAgent)
    await voice_turn(conversation, "muca start")
    conversation._engaged_since_ms = 1
    await voice_turn(conversation, "and the one after that")
    await conversation._end_assistant_turn()
    assert conversation._engaged_since_ms == 1

    # Named, but the judge kept MULCA silent.
    conversation, _ = await v2()
    await voice_turn(conversation, "muca start")
    conversation._engaged_since_ms = 1
    await voice_turn(conversation, "muca, what is the deadline")
    await conversation._end_assistant_turn()
    assert conversation._engaged_since_ms == 1


async def stepped_back(reason: str, *, mode: str = "wake_v2", after_ms: int = 1_000):
    """MULCA's session ended for `reason`; a turn starts `after_ms` later."""

    conversation, factory = await v2(auxiliary_cls=FakeInterventionAgent, mode=mode)
    await voice_turn(conversation, "muca start")
    await conversation._disengage(reason)
    start = conversation._stepped_back_ms + after_ms
    conversation.turns.append(Turn(start_time=start, end_time=start + 2_000, speaker_id=2))
    return conversation, factory, start + 2_000


async def take(conversation, text: str, ended_at: int) -> None:
    async def collect(_speaker):
        return text

    conversation._collect_transcript = collect
    await conversation._take_voice_turn(2, ended_at)
    await conversation.wait_until_idle()


async def test_naming_mulca_right_after_it_steps_back_brings_it_back():
    for mode in ("wake", "wake_v2"):
        conversation, factory, ended_at = await stepped_back("silence", mode=mode)
        asked = factory.auxiliary.asked

        await take(conversation, "hey assistant, what was the deadline", ended_at)

        assert conversation._listening_window_open is True, mode
        assert factory.auxiliary.asked == asked + 1, mode  # judged, said to MULCA


async def test_no_step_back_in_late_unnamed_after_a_dismissal_or_a_stop():
    cases = (
        ("silence", "wake_v2", 6_000, "assistant, what was the deadline"),
        ("silence", "wake_v2", 1_000, "what was the deadline"),
        ("dismissed", "wake_v2", 1_000, "assistant, what was the deadline"),
        ("silence", "wake", 6_000, "assistant, what was the deadline"),
        ("dismissed", "wake", 1_000, "assistant, what was the deadline"),
        ("silence", "wake", 1_000, "muca stop"),
    )
    for reason, mode, after_ms, text in cases:
        conversation, _, ended_at = await stepped_back(reason, mode=mode, after_ms=after_ms)
        await take(conversation, text, ended_at)
        assert conversation._listening_window_open is False, (reason, mode, after_ms, text)


async def test_a_stop_during_the_grace_cancels_it():
    for mode in ("wake", "wake_v2"):
        for stop in ("spoken", "typed"):
            conversation, _, ended_at = await stepped_back("silence", mode=mode)
            stepped_back_ms = conversation._stepped_back_ms
            if stop == "spoken":
                await take(conversation, "muca stop", ended_at)
            else:
                await typed(conversation, "MULCA stop")
            assert conversation._stepped_back_ms == 0, (mode, stop)

            # Still within 5 s of stepping back, and naming it.
            start = stepped_back_ms + 3_000
            conversation.turns.append(
                Turn(start_time=start, end_time=start + 1_000, speaker_id=2)
            )
            await take(conversation, "hey assistant, what was the deadline", start + 1_000)
            assert conversation._listening_window_open is False, (mode, stop)


async def test_spoken_turns_come_with_facts_instead_of_the_variables():
    conversation, factory = await v2(auxiliary_cls=ObservedInterventionAgent)
    await voice_turn(conversation, "muca start, what is the deadline")

    await voice_turn(conversation, "I think it is friday", speaker=2)

    told = factory.auxiliary.told
    line = turn_line(told)
    assert line.startswith("User 2: I think it is friday\nTurn facts: ")
    assert "names MULCA=no" in line
    assert "follow-up of MULCA's last answer=no (MULCA last answered User 1, 0 s ago)" in line
    assert not any("Current trigger variables" in text for text in told)

    await voice_turn(conversation, "muca, are you sure?")
    line = turn_line(factory.auxiliary.told)
    assert 'names MULCA=yes ("muca")' in line
    assert "follow-up of MULCA's last answer=yes" in line

    conversation, factory = await make_conversation(
        listening_mode="wake", auxiliary_cls=ObservedInterventionAgent
    )
    await voice_turn(conversation, "muca start, what is the deadline")
    await voice_turn(conversation, "I think it is friday", speaker=2)
    assert any("Current trigger variables" in text for text in factory.auxiliary.told)
    assert not any("Turn facts" in text for text in factory.auxiliary.told)


async def test_a_chosen_silence_is_logged_with_its_reason():
    agent = make_agent()
    decisions = await record_decisions(agent)

    await agent._interpret(
        tool_call_event(
            name=SILENCE_TOOL_NAME,
            arguments={"reason": "teammate_talk", "note": "Ada asks Bo"},
        )
    )
    await agent._interpret(response_done_event())

    assert decisions == [
        InterventionSignal(
            strategy_id="",
            prompt_fragment="",
            silence_reason="no_intervention: teammate_talk -- Ada asks Bo",
        )
    ]


class FakeSession:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def update(self, *, session) -> None:
        self.updates.append(session)


class FakeRealtimeConnection:
    def __init__(self) -> None:
        self.session = FakeSession()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()


def fake_client(connection):
    class Manager:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *args):
            return None

    return SimpleNamespace(realtime=SimpleNamespace(connect=lambda **_: Manager()))


async def connected_session(listening_mode: str) -> dict:
    connection = FakeRealtimeConnection()
    agent = InterventionAgentOpenAI(
        options=AuxiliaryAgentOptions(listening_mode=listening_mode),
        client=fake_client(connection),
        strategies=load_strategy_store(),
        persona="p",
        task_knowledge="t",
    )
    await agent._connect()
    await agent._disconnect()
    return connection.session.updates[0]


async def test_the_v2_judge_must_call_a_tool_and_silence_is_one(monkeypatch):
    session = await connected_session("wake_v2")
    assert session["tool_choice"] == "required"
    assert [tool["name"] for tool in session["tools"]] == [SILENCE_TOOL_NAME, TOOL_NAME]
    assert SILENCE_TOOL_NAME in session["instructions"]

    session = await connected_session("wake")
    assert session["tool_choice"] == "auto"
    assert [tool["name"] for tool in session["tools"]] == [TOOL_NAME]

    monkeypatch.setattr(intervention_module, "FORCED_TOOL_V2", False)
    session = await connected_session("wake_v2")
    assert session["tool_choice"] == "auto"
    assert [tool["name"] for tool in session["tools"]] == [TOOL_NAME]
    assert "call no tool at all" in session["instructions"]


def test_the_v2_prompts_override_only_their_own_strategies():
    ids = ["maintain_focus", "respond_when_addressed", "provide_summary", "remember"]
    default = load_strategy_store(ids)
    store = load_strategy_store(ids, overrides_dir=WAKE_V2_CONFIG_DIR)

    assert store.ids() == ids
    assert store.get("maintain_focus") == default.get("maintain_focus")
    for sid in ids[1:]:
        assert store.get(sid).trigger_description != default.get(sid).trigger_description
        assert store.get(sid).prompt_template == default.get(sid).prompt_template
    assert "Turn facts" in load_persona("intervention_default", config_dir=WAKE_V2_CONFIG_DIR)


def test_the_v2_transcription_prompt_has_no_wake_phrase_to_echo():
    def agent(mode):
        return MainAgentOpenAI(
            options=MainAgentOptions(name="MULCA", listening_mode=mode),
            client=object(),
        )

    assert "MULCA start" in agent("wake")._transcription_prompt()
    assert "MULCA start" not in agent("wake_v2")._transcription_prompt()


def test_a_transcript_of_only_the_prompt_wrapper_is_empty():
    from muca_server.agents.openai.main_agent import _strip_prompt_wrapper

    assert _strip_prompt_wrapper("context:") == ""
    assert _strip_prompt_wrapper("context: ###") == ""
    assert _strip_prompt_wrapper("context: Hello.") == "Hello."
    assert (
        _strip_prompt_wrapper(
            "Eh, ma di testa? You will receive additional context/instructions "
            "(separated by ### delimiters) from the user."
        )
        == "Eh, ma di testa?"
    )
    assert _strip_prompt_wrapper("The context of use is a school.") == (
        "The context of use is a school."
    )


async def test_a_chosen_silence_goes_into_the_turns_log():
    from test_coala_policy import make_policy, make_utterance

    from muca_server.cognition.memory import InMemoryEpisodicStore

    episodic_store = InMemoryEpisodicStore()
    policy, port = make_policy(episodic_store=episodic_store)
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="ok", modality="audio"))

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="",
            prompt_fragment="",
            silence_reason="no_intervention: reaction_or_fragment",
        ),
    )

    [record] = await episodic_store.recall_recent(port.conversation_id, 50)
    assert record.intervention_path == "none"
    assert record.prompt_fragment == "no_intervention: reaction_or_fragment"
