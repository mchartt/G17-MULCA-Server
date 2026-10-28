"""
"toggle"/"transcribe" listening modes: two separate, mutually exclusive
alternatives to "ambient" (every turn judged). In both, the intervention
agent is only consulted inside a window, never hears raw audio at all
(regardless of window state). "toggle" opens/closes only on an explicit
ArmListeningMessage (button/`/listen`), with no auto-close timer.
"transcribe" opens on a spoken wake phrase and auto-closes after silence
(there's no spoken "stop" to toggle on). Ambient mode (the default) must
behave exactly as before.
"""

import asyncio
from uuid import uuid4

import muca_server.agents.conversation as conversation_module
from muca_server.agents import AuxiliaryAgent, MainAgent, MainAgentOptions
from muca_server.agents.conversation import (
    Conversation,
    _addresses_assistant,
    _matches_dismissal,
    _matches_wake_phrase,
    _wake_phrase_has_question,
)
from muca_server.agents.main_agent import MainAgentState
from muca_server.agents.turn import Turn
from muca_server.audio import AudioChunk
from muca_server.cognition import CoalaPolicy, InterventionSignal, Utterance
from muca_server.cognition.strategies import Strategy, StrategyStore
from muca_server.protocol import parse_incoming_message
from muca_server.sessions import SessionState

STRATEGIES = StrategyStore(
    {
        "respond_when_addressed": Strategy(
            id="respond_when_addressed",
            trigger_description="addressed directly",
            prompt_template="Answer them. {prompt_fragment}",
            # These test utterances are modality="audio" (voice turns);
            # Strategy defaults to {"text"} only, which would otherwise
            # silently veto every signal below (on_intervention_signal's
            # modality guard, cognition/policy.py).
            modalities=frozenset({"text", "audio"}),
        ),
    }
)

STRATEGIES_WITH_ALWAYS_REPLY = StrategyStore(
    {
        "respond_when_addressed": Strategy(
            id="respond_when_addressed",
            trigger_description="addressed directly",
            prompt_template="Answer them. {prompt_fragment}",
            modalities=frozenset({"text", "audio"}),
        ),
        "always_reply": Strategy(
            id="always_reply",
            trigger_description="toggle/transcribe fallback",
            prompt_template="Respond directly. {prompt_fragment}",
            modalities=frozenset({"audio"}),
        ),
    }
)

VOICE = AudioChunk(data=b"\x00\x00\x00\x3f" * 240).to_base64()


class FakeInterventionAgent(AuxiliaryAgent):
    """An observer that always signals the same (fixed) decision, and
    counts how often it was actually asked to judge vs. just handed raw
    audio. `signal=None` means it declines ("do nothing")."""

    signal: InterventionSignal | None = InterventionSignal(
        strategy_id="respond_when_addressed",
        prompt_fragment="they asked a direct question",
    )

    def __init__(self, *, options) -> None:
        super().__init__(options=options)
        self.asked = 0
        self.audio_chunks_received = 0

    async def _trigger_response(
        self,
        *,
        max_tokens,
        instructions,
        add_to_conversation,
        **_,
    ) -> None:
        self.asked += 1
        await self._signal_decision(self.signal)

    async def _send_audio_chunk(self, chunk) -> None:
        self.audio_chunks_received += 1


class ScriptedMainAgent(MainAgent):
    def __init__(self, *, options) -> None:
        super().__init__(options=options)
        self.instructions: list[str] = []

    async def _trigger_response(
        self,
        *,
        max_tokens,
        instructions,
        add_to_conversation,
        **_,
    ) -> None:
        self.instructions.append(instructions)
        await self._stream_text("ok", is_final=True)


class DecliningInterventionAgent(FakeInterventionAgent):
    """Always signals None -- the intervention agent's own "do nothing"."""

    signal = None


class FixedAgentFactory:
    def __init__(self, *, auxiliary_cls: type = FakeInterventionAgent) -> None:
        self.main: ScriptedMainAgent | None = None
        self.auxiliary: FakeInterventionAgent | None = None
        self._auxiliary_cls = auxiliary_cls

    def build_main(self, options):
        self.main = ScriptedMainAgent(options=options)
        return self.main

    def build_auxiliary(self, options):
        self.auxiliary = self._auxiliary_cls(options=options)
        return self.auxiliary


def create_auxiliary():
    return parse_incoming_message(
        {
            "event": "create_auxiliary_agent",
            "auxiliaryAgentType": "intervention_detection",
        }
    )


def arm():
    return parse_incoming_message({"event": "arm_listening"})


def audio(speaker: int):
    return parse_incoming_message(
        {
            "event": "audio_stream",
            "speaker": speaker,
            "audio": VOICE,
            "isNotifying": False,
            "playbackTimeMs": 0,
        }
    )


def utterance(text: str = "hello", *, always_reply: bool = False) -> Utterance:
    return Utterance(
        speaker_id=1,
        speaker_name="Ada",
        content=text,
        timestamp_ms=0,
        modality="audio",
        always_reply=always_reply,
    )


async def make_conversation(
    *,
    listening_mode: str = "toggle",
    strategies: StrategyStore = STRATEGIES,
    auxiliary_cls: type = FakeInterventionAgent,
):
    state = SessionState(session_id=uuid4())
    outbound: asyncio.Queue = asyncio.Queue()
    factory = FixedAgentFactory(auxiliary_cls=auxiliary_cls)
    policy = CoalaPolicy(strategy_store=strategies)

    conversation = Conversation(
        state=state,
        outbound_queue=outbound,
        main_agent_options=MainAgentOptions(),
        agent_factory=factory,
        policy=policy,
        listening_mode=listening_mode,
    )
    await conversation.start()
    await conversation.create_auxiliary_agent(create_auxiliary())
    return conversation, factory


async def test_a_closed_window_never_asks_the_intervention_agent():
    conversation, factory = await make_conversation()

    await conversation._judge(utterance())
    await conversation.wait_until_idle()

    assert factory.auxiliary.asked == 0
    assert not factory.main.instructions


async def test_arming_opens_a_window_that_lets_judgment_through():
    conversation, factory = await make_conversation()

    await conversation.arm_listening(arm())
    # always_reply=True: the real caller is _take_voice_turn, which
    # snapshots window state itself before calling _judge (see its own
    # comment) -- this test calls _judge directly, so it has to pass the
    # same snapshot a real in-window turn would have had.
    await conversation._judge(utterance(always_reply=True))
    await conversation.wait_until_idle()
    for _ in range(5):
        await asyncio.sleep(0)

    assert factory.auxiliary.asked == 1
    assert factory.main.instructions


async def test_a_second_press_closes_the_window_immediately():
    # The button carries no state of its own -- every press sends the
    # same message, and the server (not the client) decides whether that
    # means "open" or "close" based on its own current state.
    conversation, _ = await make_conversation()

    await conversation.arm_listening(arm())
    assert conversation._listening_window_open is True

    await conversation.arm_listening(arm())
    assert conversation._listening_window_open is False


async def test_the_window_stays_open_with_no_auto_close_timer():
    # Found live: an auto-close timer alongside manual open/close fought
    # the manual control, closing a window mid-conversation well before
    # the next press -- removed entirely for "toggle" mode. Once opened, a
    # window now stays open indefinitely, across any number of judged
    # turns, until an explicit press closes it.
    conversation, _ = await make_conversation()

    await conversation.arm_listening(arm())
    for _ in range(3):
        await conversation._judge(utterance(always_reply=True))
        await conversation.wait_until_idle()

    assert conversation._listening_window_open is True


async def test_arm_listening_is_a_no_op_outside_toggle_mode():
    # "toggle" and "transcribe" are separate, mutually exclusive modes, not
    # two input methods for one combined mode: a button press on a
    # "transcribe" link should do nothing.
    conversation, factory = await make_conversation(listening_mode="transcribe")

    await conversation.arm_listening(arm())

    assert conversation._listening_window_open is False


async def test_text_turns_are_judged_normally_even_with_a_closed_window():
    # "toggle"/"transcribe" are voice-UX concepts (the problem they fix was
    # continuous audio judgment); a text message or DM on a link in either
    # mode must not be silenced behind the window.
    conversation, factory = await make_conversation()

    text_utterance = Utterance(
        speaker_id=1,
        speaker_name="Ada",
        content="hello",
        timestamp_ms=0,
        modality="text",
    )
    await conversation._judge(text_utterance)
    await conversation.wait_until_idle()
    for _ in range(5):
        await asyncio.sleep(0)

    assert factory.auxiliary.asked == 1
    assert factory.main.instructions


async def test_the_intervention_agent_never_hears_raw_audio_in_this_mode():
    conversation, factory = await make_conversation()

    # Window open or not: the intervention agent never gets raw audio at
    # all in "toggle" mode, only _judge()'s text push.
    await conversation.arm_listening(arm())
    await conversation.route_user_audio(audio(speaker=1))

    assert factory.auxiliary.audio_chunks_received == 0


async def test_a_per_link_handshake_override_wins_over_the_servers_own_mode():
    # The server defaults to "ambient" (as if MUCA_LISTENING_MODE were
    # unset); this link's own ConnectAgentMessage asks for "toggle" --
    # same override precedence as mainModel/interventionModel.
    conversation, factory = await make_conversation(listening_mode="ambient")

    await conversation.connect_agent(
        parse_incoming_message({"event": "connect", "listeningMode": "toggle"})
    )

    await conversation._judge(utterance())
    await conversation.wait_until_idle()

    assert factory.auxiliary.asked == 0  # closed window: never asked


async def test_a_declined_turn_inside_a_window_falls_back_to_always_reply():
    # The intervention agent itself chose "do nothing" -- "toggle"/
    # "transcribe"'s guarantee overrides that, same as a hard veto, rather
    # than letting an open window ever produce silence.
    conversation, factory = await make_conversation(
        strategies=STRATEGIES_WITH_ALWAYS_REPLY,
        auxiliary_cls=DecliningInterventionAgent,
    )

    await conversation.arm_listening(arm())
    await conversation._judge(utterance("define the thing", always_reply=True))
    await conversation.wait_until_idle()
    for _ in range(5):
        await asyncio.sleep(0)

    assert factory.auxiliary.asked == 1
    assert factory.main.instructions
    assert "Respond directly" in factory.main.instructions[0]


async def test_always_reply_never_fires_outside_non_ambient_modes():
    # Ambient mode never sets always_reply on an utterance in the first
    # place (conversation.py), but on_intervention_signal's own guard
    # should refuse to fire it even if something did ask for it.
    conversation, factory = await make_conversation(
        listening_mode="ambient",
        strategies=STRATEGIES_WITH_ALWAYS_REPLY,
        auxiliary_cls=DecliningInterventionAgent,
    )

    await conversation._judge(utterance("define the thing", always_reply=False))
    await conversation.wait_until_idle()
    for _ in range(5):
        await asyncio.sleep(0)

    assert factory.auxiliary.asked == 1
    assert not factory.main.instructions  # declined, and nothing forced it


async def test_ambient_mode_is_unaffected():
    conversation, factory = await make_conversation(listening_mode="ambient")

    await conversation._judge(utterance())
    await conversation.wait_until_idle()
    for _ in range(5):
        await asyncio.sleep(0)

    assert factory.auxiliary.asked == 1
    assert factory.main.instructions

    await conversation.route_user_audio(audio(speaker=1))
    assert factory.auxiliary.audio_chunks_received == 1


def test_waking_takes_a_start_command_not_a_mention():
    # Found live: one word ("mulca", "assistant", "bot") woke MULCA all the
    # time -- several groups build their own MULCAs/assistants/chatbots.
    for transcript in (
        "mulca start",
        "Mulca, start.",
        "MULCA please start",
        "muca start",  # how the transcriber often renders it
        "malca start",
        "assistant start",
        "assistente start",
        "chatbot start",
        "hey mulca start, what is the deadline",
        "ok so, mulca start",
    ):
        assert _matches_wake_phrase(transcript), transcript

    for transcript in (
        "mulca",
        "hey mulca",
        "Assistant?",
        "our assistant should show the deadline",
        "the assistant starts by asking a question",
        "what do you think, mulca?",
        "bot start",
        "let's start the mulca",
        "start",
        "",
    ):
        assert not _matches_wake_phrase(transcript), transcript


def test_once_awake_the_name_alone_addresses_muca():
    assert _addresses_assistant("what do you think, mulca?")
    assert _addresses_assistant("muca, and the budget?")
    assert _addresses_assistant("assistant, what about friday")
    assert not _addresses_assistant("our assistant should show the deadline")
    assert not _addresses_assistant("I think it is friday")


def test_wake_phrase_has_question_detects_a_real_question_in_the_same_breath():
    assert _wake_phrase_has_question("mulca start, could you define the UNG diagram")
    assert _wake_phrase_has_question("assistant start what time is the next class")

    # The command alone (plus stray punctuation/filler) -- no question.
    assert not _wake_phrase_has_question("mulca start")
    assert not _wake_phrase_has_question("Mulca, start.")
    assert not _wake_phrase_has_question("hey mulca please start")
    assert not _wake_phrase_has_question("")


async def test_the_wake_phrase_opens_the_window_hands_free():
    conversation, _ = await make_conversation(listening_mode="transcribe")
    assert conversation._listening_window_open is False

    await conversation._handle_wake_phrase(speaker=1)

    assert conversation._listening_window_open is True


async def test_the_wake_phrase_window_closes_itself_after_silence(monkeypatch):
    import muca_server.agents.conversation as conversation_module

    monkeypatch.setattr(conversation_module, "WAKE_PHRASE_SILENCE_MS", 20)
    conversation, _ = await make_conversation(listening_mode="transcribe")

    await conversation._handle_wake_phrase(speaker=1)
    assert conversation._listening_window_open is True

    await asyncio.sleep(0.05)
    assert conversation._listening_window_open is False


async def test_a_repeated_wake_phrase_restarts_the_silence_timer(monkeypatch):
    # The wake phrase can also re-arm an already-open window (e.g. said
    # again mid-conversation) -- that should restart the silence clock
    # rather than leave a stale timer from the first utterance pending.
    import muca_server.agents.conversation as conversation_module

    monkeypatch.setattr(conversation_module, "WAKE_PHRASE_SILENCE_MS", 40)
    conversation, _ = await make_conversation(listening_mode="transcribe")

    await conversation._handle_wake_phrase(speaker=1)
    await asyncio.sleep(0.02)
    await conversation._handle_wake_phrase(speaker=1)  # restarts the timer
    await asyncio.sleep(0.03)
    assert conversation._listening_window_open is True  # not yet 40ms since restart

    await asyncio.sleep(0.03)
    assert conversation._listening_window_open is False


# ----------------------------------------------------------------------
# "wake": the wake phrase starts an engaged session, judged like ambient,
# until silence, the time limit, a dismissal or interruptions end it.
# ----------------------------------------------------------------------


def playback():
    return parse_incoming_message({"event": "playback_frame", "audio": VOICE})


def listening_states(conversation) -> list[bool]:
    states = []
    queue = conversation._outbound_queue
    while not queue.empty():
        message = queue.get_nowait()
        if getattr(message, "event", None) == "listening_state":
            states.append(message.open)
    return states


async def voice_turn(conversation, text: str, *, speaker: int = 1) -> None:
    """A spoken turn whose transcript is `text` ("" for untranscribed)."""

    async def collect(_speaker):
        return text

    conversation._collect_transcript = collect
    await conversation._take_voice_turn(speaker, 0)
    await conversation.wait_until_idle()
    for _ in range(5):
        await asyncio.sleep(0)


async def interrupted(conversation, heard_ms, *, lasted_ms=1_000, text="wait, stop"):
    """MUCA talked over after `heard_ms`, by a turn of `lasted_ms`."""

    conversation.turns.append(Turn(start_time=0, end_time=lasted_ms, speaker_id=1))
    conversation._pending_interruption_ms = heard_ms
    await conversation._settle_interruption(1, lasted_ms, text)


async def test_wake_dormant_turns_are_not_judged_and_the_judge_hears_nothing():
    conversation, factory = await make_conversation(listening_mode="wake")

    await voice_turn(conversation, "we should look at the budget")
    await voice_turn(conversation, "")
    await conversation.route_user_audio(audio(speaker=1))
    conversation.main_agent_state = MainAgentState.ANSWERING
    await conversation.route_playback_frame(playback())
    await conversation._send_to_listeners(AudioChunk(data=b"\x00" * 400))

    assert factory.auxiliary.asked == 0
    assert factory.auxiliary.audio_chunks_received == 0
    assert not factory.main.instructions


async def test_the_wake_phrase_engages_without_the_judge_and_announces_once():
    conversation, factory = await make_conversation(
        listening_mode="wake",
        strategies=STRATEGIES_WITH_ALWAYS_REPLY,
        auxiliary_cls=DecliningInterventionAgent,
    )

    await voice_turn(conversation, "muca start, what is the deadline")
    assert factory.auxiliary.asked == 0  # forced, never judged
    assert len(factory.main.instructions) == 1

    # Already engaged: naming MULCA again is judged, not forced.
    await voice_turn(conversation, "muca, and the budget?")

    assert conversation._listening_window_open is True
    assert factory.auxiliary.asked == 1
    assert len(factory.main.instructions) == 1  # declined, nothing forced
    assert listening_states(conversation) == [True]


async def test_engaged_turns_are_judged_like_ambient_and_heard():
    conversation, factory = await make_conversation(
        listening_mode="wake",
        strategies=STRATEGIES_WITH_ALWAYS_REPLY,
        auxiliary_cls=DecliningInterventionAgent,
    )
    await voice_turn(conversation, "muca start, what is the deadline")

    await voice_turn(conversation, "I think it is friday", speaker=2)
    await conversation.route_user_audio(audio(speaker=2))

    assert factory.auxiliary.asked == 1
    assert len(factory.main.instructions) == 1  # declined, nothing forced
    assert factory.auxiliary.audio_chunks_received == 1


async def test_a_bare_wakes_next_turn_is_judged_not_forced():
    # Found in the 15-28 Oct exports: 140 of 168 forced replies were wrong.
    conversation, factory = await make_conversation(
        listening_mode="wake",
        strategies=STRATEGIES_WITH_ALWAYS_REPLY,
        auxiliary_cls=DecliningInterventionAgent,
    )

    await voice_turn(conversation, "muca start")
    await voice_turn(conversation, "something unrelated", speaker=2)
    await voice_turn(conversation, "what is the deadline")
    await voice_turn(conversation, "and another thing")

    assert factory.auxiliary.asked == 3
    assert len(factory.main.instructions) == 1  # only the ack


async def test_untranscribed_turns_are_judged_only_in_ambient():
    # Never inside a window: with no words to judge, the observer answered
    # MULCA's own last reply, over and over (found live, 14 Oct).
    for mode, engage, judged in [
        ("wake", False, 0),
        ("wake", True, 0),
        ("transcribe", False, 0),
        ("ambient", False, 1),
    ]:
        conversation, factory = await make_conversation(listening_mode=mode)
        if engage:
            await conversation._handle_wake_phrase(speaker=1)

        await voice_turn(conversation, "")

        assert factory.auxiliary.asked == judged, mode


async def test_text_turns_are_judged_while_wake_is_dormant():
    conversation, factory = await make_conversation(listening_mode="wake")

    await conversation._judge(
        Utterance(
            speaker_id=1,
            speaker_name="Ada",
            content="hello",
            timestamp_ms=0,
            modality="text",
        )
    )
    await conversation.wait_until_idle()

    assert factory.auxiliary.asked == 1


async def test_a_quiet_room_ends_the_session_and_speech_keeps_it_alive():
    conversation, _ = await make_conversation(listening_mode="wake")
    await conversation._handle_wake_phrase(speaker=1)
    start = conversation._last_speech_ms
    silence = conversation_module.ENGAGED_SILENCE_MS

    conversation.turns.append(Turn(start_time=start, speaker_id=1))
    await conversation._check_engaged_session(start + silence)
    assert conversation._listening_window_open is True
    assert conversation._last_speech_ms == start + silence

    conversation.turns[-1].end_time = start + silence
    await conversation._check_engaged_session(start + 2 * silence - 1)
    assert conversation._listening_window_open is True
    await conversation._check_engaged_session(start + 2 * silence)
    assert conversation._listening_window_open is False
    assert listening_states(conversation) == [True, False]


async def test_the_time_limit_waits_for_a_playing_reply():
    conversation, _ = await make_conversation(listening_mode="wake")
    await conversation._handle_wake_phrase(speaker=1)
    cap = conversation._engaged_since_ms + conversation_module.ENGAGED_MAX_MS

    conversation.main_agent_state = MainAgentState.ANSWERING
    await conversation._check_engaged_session(cap)
    assert conversation._listening_window_open is True

    conversation.main_agent_state = MainAgentState.INACTIVE
    await conversation._check_engaged_session(cap + 1)
    assert conversation._listening_window_open is False


async def test_naming_muca_mid_session_restarts_nothing():
    # Found live: every passing mention reset the time limit and the
    # interruption score, so an engaged session never ended.
    conversation, _ = await make_conversation(listening_mode="wake")
    await voice_turn(conversation, "muca start")
    conversation._engaged_since_ms = 1
    conversation._interruption_score = 1
    conversation._warned_about_interruptions = True

    await voice_turn(conversation, "muca, what about the budget")

    assert conversation._engaged_since_ms == 1
    assert conversation._interruption_score == 1
    assert conversation._warned_about_interruptions is True


async def test_turns_not_addressed_to_muca_end_the_session():
    conversation, factory = await make_conversation(
        listening_mode="wake",
        strategies=STRATEGIES_WITH_ALWAYS_REPLY,
        auxiliary_cls=DecliningInterventionAgent,
    )
    limit = conversation_module.ENGAGED_MAX_UNADDRESSED_TURNS
    await voice_turn(conversation, "muca start, what is the deadline")

    for _ in range(limit - 1):
        await voice_turn(conversation, "I think it is friday", speaker=2)
    await voice_turn(conversation, "muca, are you sure?")  # counts as naming it
    for _ in range(limit - 1):
        await voice_turn(conversation, "ok let us move on", speaker=2)
    assert conversation._listening_window_open is True
    asked = factory.auxiliary.asked

    await voice_turn(conversation, "next point", speaker=2)

    assert conversation._listening_window_open is False
    assert factory.auxiliary.asked == asked + 1  # judged, then it sleeps
    assert listening_states(conversation) == [True, False]


STRATEGIES_WITH_OWN_INITIATIVE = StrategyStore(
    {
        **{s.id: s for s in STRATEGIES_WITH_ALWAYS_REPLY.all()},
        "participation_equality_voice": Strategy(
            id="participation_equality_voice",
            trigger_description="unequal participation",
            prompt_template="Invite the quiet one. {prompt_fragment}",
            modalities=frozenset({"audio"}),
        ),
    }
)


async def engaged_with_judge(signal):
    conversation, factory = await make_conversation(
        listening_mode="wake",
        strategies=STRATEGIES_WITH_OWN_INITIATIVE,
        auxiliary_cls=DecliningInterventionAgent,
    )
    await voice_turn(conversation, "muca start, what is the deadline")
    factory.auxiliary.signal = signal
    return conversation, factory


async def test_a_follow_up_the_judge_sees_as_addressed_keeps_muca_awake():
    # No name, but the intervention agent's own choice says it was asked.
    conversation, _ = await engaged_with_judge(
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment="")
    )
    limit = conversation_module.ENGAGED_MAX_UNADDRESSED_TURNS

    for _ in range(limit + 2):
        await voice_turn(conversation, "and what about friday?", speaker=2)

    assert conversation._listening_window_open is True
    assert conversation._unaddressed_turns == 0


async def test_muca_jumping_in_on_its_own_doesnt_keep_it_awake():
    conversation, _ = await engaged_with_judge(
        InterventionSignal(strategy_id="participation_equality_voice", prompt_fragment="")
    )
    limit = conversation_module.ENGAGED_MAX_UNADDRESSED_TURNS

    for _ in range(limit):
        await voice_turn(conversation, "I think it is friday", speaker=2)

    assert conversation._listening_window_open is False


async def test_a_direct_wake_phrase_call_restarts_the_session():
    conversation, _ = await make_conversation(listening_mode="wake")
    await conversation._handle_wake_phrase(speaker=1)
    conversation._engaged_since_ms = 0
    conversation._interruption_score = 3
    conversation._warned_about_interruptions = True

    await conversation._handle_wake_phrase(speaker=1)

    assert conversation._engaged_since_ms > 0
    assert conversation._interruption_score == 0
    assert conversation._warned_about_interruptions is False
    assert listening_states(conversation) == [True]


def test_dismissal_matching():
    # As flexible as _matches_wake_phrase: a plain substring search, no
    # length ceiling, regardless of the name's presence or anything else
    # in the sentence -- a versatile, longer way of saying goodbye matches
    # exactly as well as a short one.
    for transcript in (
        "thank you very much muca",
        "ok that's all",
        "grazie mille mulca",
        "Mulca, grazie.",
        "thanks, assistant",
        "MULCA stop",
        "stop, muca",
        # Found live (group 10, 14 Oct):
        "MULCA turn off.",
        "Ok, power off.",
        "switch it off",
        "shut down please",
        "mulca spegniti",
        "Mulca, off",
        "muca stai zitto",
        "basta così mulca",
        "stop mulca",
        "go to sleep",  # redundant with bare "sleep" below, kept as a check
        "sleep",
        "mulca, sleep",
        "vai a dormire mulca",  # redundant with bare "dormire" below
        "dormire",
        "end session",
        "end the session",
        "mulca, end session",
        "that's enough",
        "fine sessione",
        "fine della sessione mulca",
        "go ahead and wrap it up, thanks for all the help today",
        # Found live not to work before:
        "please sleep mulca",
        "please shut up",
        "go to bed",
        "quiet mulca",
        "ok bye",
        "not now mulca",
        "shh",
        "vai via",
        "pausa",
    ):
        assert _matches_dismissal(transcript)
    assert not _matches_dismissal("let us move on")
    for transcript in ("I turned it off and on", "the offline mode", "off-topic"):
        assert not _matches_dismissal(transcript), transcript
    # A thanks between students, not to MULCA (found live, 19-25 Oct).
    for transcript in (
        "grazie, adesso passiamo al prossimo punto",
        "No grazie.",
        "Mi sento bene, grazie.",
        "Grazie mille di parlare.",
        "thanks Ada, now let us move on to the budget",
    ):
        assert not _matches_dismissal(transcript), transcript
    # Known, accepted cost of this flexibility (see DISMISSAL_PATTERN's own
    # comment): a longer sentence that happens to contain a dismissal word
    # now also matches, even when it wasn't really goodbye -- the mirror
    # image of _matches_wake_phrase's own "any sentence naming MULCA opens
    # a window" trade-off.
    assert _matches_dismissal("thanks muca, can you also check the budget")
    assert _matches_dismissal("stop sharing your screen and look at this, Ada")


async def test_a_dismissal_ends_the_session_unanswered_and_never_starts_one():
    conversation, factory = await make_conversation(listening_mode="wake")

    await voice_turn(conversation, "thanks muca")
    assert conversation._listening_window_open is False
    assert not factory.main.instructions

    await voice_turn(conversation, "muca start, what is the deadline")
    await voice_turn(conversation, "thanks muca")

    assert conversation._listening_window_open is False
    assert len(factory.main.instructions) == 1
    assert factory.auxiliary.asked == 0
    assert listening_states(conversation) == [True, False]


async def typed(conversation, text: str, *, mentions: bool = False) -> None:
    await conversation._take_text_turn(
        parse_incoming_message(
            {
                "event": "user_text",
                "speaker": 1,
                "speakerName": "Ada",
                "text": text,
                "mentionsAssistant": mentions,
            }
        )
    )
    await conversation.wait_until_idle()


async def test_a_typed_stop_naming_muca_ends_the_voice_session():
    conversation, _ = await make_conversation(listening_mode="wake")

    await voice_turn(conversation, "muca start, what is the deadline")
    await typed(conversation, "thanks, stop sharing the screen")  # not named
    assert conversation._listening_window_open is True

    await typed(conversation, "MULCA stop")
    assert conversation._listening_window_open is False

    await voice_turn(conversation, "muca start, what is the deadline")
    await typed(conversation, "<@1557331332789702666>  stop", mentions=True)
    assert conversation._listening_window_open is False


async def test_interruptions_are_scored_by_how_long_muca_was_heard(monkeypatch):
    # Scoring only: keep the session from ending partway through.
    monkeypatch.setattr(conversation_module, "INTERRUPTION_THRESHOLD_START", 10)
    conversation, _ = await make_conversation(listening_mode="wake")
    await conversation._handle_wake_phrase(speaker=1)

    await interrupted(conversation, 500)
    await interrupted(conversation, 400)
    assert conversation._interruption_score == 2
    await interrupted(conversation, 2_000)
    assert conversation._interruption_score == 2
    await interrupted(conversation, 4_000)
    assert conversation._interruption_score == 1
    await interrupted(conversation, 9_000)
    assert conversation._interruption_score == 0  # never below 0

    # A backchannel or a cough doesn't count.
    await interrupted(conversation, 500, lasted_ms=300, text="mm")
    assert conversation._interruption_score == 0


async def test_interruptions_warn_the_judge_at_half_then_end_the_session():
    conversation, _ = await make_conversation(listening_mode="wake")
    await conversation._handle_wake_phrase(speaker=1)

    await interrupted(conversation, 500)  # 1: half of 2
    assert conversation._warned_about_interruptions is True
    assert conversation._listening_window_open is True

    await interrupted(conversation, 500)  # 2
    assert conversation._listening_window_open is False


async def test_the_interruption_threshold_drops_over_time_to_one():
    conversation, _ = await make_conversation(listening_mode="wake")
    await conversation._handle_wake_phrase(speaker=1)
    start = conversation._engaged_since_ms
    step = conversation_module.INTERRUPTION_THRESHOLD_STEP_MS

    assert conversation._interruption_threshold(start) == 2
    assert conversation._interruption_threshold(start + step) == 1
    assert conversation._interruption_threshold(start + 2 * step) == 1

    # At the floor, the first talk-over that counts ends the session.
    conversation._engaged_since_ms = start - step
    await interrupted(conversation, 500)
    assert conversation._listening_window_open is False


async def test_a_reply_heard_out_resets_the_interruption_score():
    conversation, _ = await make_conversation(listening_mode="wake")
    await conversation._handle_wake_phrase(speaker=1)
    await interrupted(conversation, 500)
    assert conversation._interruption_score == 1

    await conversation._end_assistant_turn()

    assert conversation._interruption_score == 0


# ----------------------------------------------------------------------
# Session events: voice calls and wake sessions, logged for analysis.
# ----------------------------------------------------------------------


class EventLog:
    def __init__(self, policy):
        self.events = []

        async def record(event, detail="", *, speaker="", data=None):
            self.events.append((event, detail, speaker))

        policy.on_session_event = record

    def names(self):
        return [event for event, _, _ in self.events]


def voice_roster(*people):
    return parse_incoming_message(
        {
            "event": "participants",
            "modality": "audio",
            "participants": [{"id": i, "name": n} for i, n in people],
        }
    )


async def test_voice_call_starts_joins_leaves_and_ends_are_logged():
    conversation, _ = await make_conversation(listening_mode="wake")
    log = EventLog(conversation._policy)

    await conversation.route_participants(voice_roster())  # first: empty
    await conversation.route_participants(voice_roster((1, "Ada")))
    await conversation.route_participants(voice_roster((1, "Ada"), (2, "Bo")))
    await conversation.route_participants(voice_roster((2, "Bo")))
    await conversation.route_participants(voice_roster((2, "Bo")))  # resent
    await conversation.route_participants(voice_roster())

    assert log.events == [
        ("voice_call_started", "Ada", ""),
        ("voice_joined", "", "Bo"),
        ("voice_left", "", "Ada"),
        ("voice_call_ended", "Bo", ""),
    ]


async def test_a_call_already_under_way_at_connect_is_not_a_start():
    conversation, _ = await make_conversation(listening_mode="wake")
    log = EventLog(conversation._policy)

    await conversation.route_participants(voice_roster((1, "Ada")))

    assert log.events == [
        ("voice_call_ongoing", "already in the call at connect: Ada", ""),
    ]


async def test_wake_sessions_log_their_trigger_and_why_they_ended():
    conversation, _ = await make_conversation(listening_mode="wake")
    log = EventLog(conversation._policy)

    await voice_turn(conversation, "muca start, what is the deadline")
    await voice_turn(conversation, "ok thanks muca")
    await voice_turn(conversation, "muca start")
    await conversation.close()

    assert [(e, d) for e, d, _ in log.events] == [
        ("wake_started", "muca start, what is the deadline"),
        ("wake_ended", "dismissed"),
        ("wake_started", "muca start"),
        ("wake_ended", "session closed"),
    ]


REPLY = (
    "Here is a quick summary of your doubts: you tested the audio, decided "
    "to stay in Italian, and want to read the papers before building."
)


def test_a_turn_repeating_a_fresh_reply_is_a_suspected_echo():
    from muca_server.agents.conversation import _is_echo

    replies = [(100_000, REPLY)]
    # Heard back almost word for word, soon after.
    assert _is_echo(
        "a quick summary of your doubts you tested the audio decided to stay in Italian",
        replies,
        110_000,
    )
    # Long after the reply, short, or someone quoting a few words of it in
    # their own sentence: not an echo.
    assert not _is_echo("a quick summary of your doubts you tested the audio", replies, 200_000)
    assert not _is_echo("a quick summary", replies, 110_000)
    assert not _is_echo(
        "she said a quick summary of your doubts but I think we should do it in English instead today",
        replies,
        110_000,
    )


async def test_a_suspected_echo_is_logged_and_still_judged():
    conversation, factory = await make_conversation(listening_mode="wake")
    log = EventLog(conversation._policy)
    await voice_turn(conversation, "muca start")
    conversation._recent_replies = [(conversation_module._now_ms(), REPLY)]
    asked = factory.auxiliary.asked

    await voice_turn(conversation, "a quick summary of your doubts you tested the audio decided to stay")

    assert "echo_suspected" in log.names()
    assert factory.auxiliary.asked == asked + 1  # logged only, nothing dropped


async def test_talk_overs_are_logged_with_their_score():
    conversation, _ = await make_conversation(listening_mode="wake")
    await conversation._handle_wake_phrase(speaker=1)
    log = EventLog(conversation._policy)

    await interrupted(conversation, 500)

    assert log.names() == ["wake_talked_over"]
    assert log.events[0][1] == "after 500ms, score 1/2"


async def test_a_failing_event_log_never_breaks_the_conversation():
    conversation, _ = await make_conversation(listening_mode="wake")

    async def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    conversation._policy.on_session_event = broken

    await voice_turn(conversation, "muca start")

    assert conversation._listening_window_open is True


async def test_a_context_reset_clears_every_agent_and_catches_the_observer_up():
    conversation, factory = await make_conversation(listening_mode="ambient")
    cleared, told = [], []

    async def clear_main():
        cleared.append("main")

    async def clear_observer():
        cleared.append("observer")

    async def tell(text, role="user"):
        told.append(text)

    factory.main.clear_context = clear_main
    factory.auxiliary.clear_context = clear_observer
    factory.auxiliary.send_text = tell
    # The policy's own hook is what the agents call when they're full.
    assert factory.main.on_context_full == conversation._policy.on_context_full

    await conversation.reset_agent_contexts("the memory")
    await conversation.wait_until_idle()

    assert cleared == ["main", "observer"]
    assert told == ["the memory"]
