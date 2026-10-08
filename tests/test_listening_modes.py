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

from muca_server.agents import AuxiliaryAgent, MainAgent, MainAgentOptions
from muca_server.agents.conversation import (
    Conversation,
    _matches_wake_phrase,
    _wake_phrase_has_question,
)
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


def test_wake_phrase_matching_is_deterministic_not_fuzzy_guessing():
    # Anchored on bare "assist\w*" -- not "MULCA" (confirmed this session
    # to transcribe unreliably), not a full phonetic/LLM judgement
    # (confirmed this session to be inconsistent), and deliberately no
    # required lead-in word like "hey": confirmed live that the
    # transcriber sometimes drops a short, quiet lead-in word entirely,
    # which a "hey assistant"-only pattern then misses completely (a real
    # turn transcribed as just "Assistant?").
    assert _matches_wake_phrase("assistant")
    assert _matches_wake_phrase("Assistant?")
    assert _matches_wake_phrase("hey assistant")
    assert _matches_wake_phrase("assistent")  # transcription near-miss
    assert _matches_wake_phrase("assistants")  # plural
    # The tradeoff of dropping the lead-in requirement: "assistant"
    # mentioned in passing now also opens the window, not just a direct
    # address -- accepted deliberately, in exchange for actually detecting
    # the bare "Assistant?" case above, which real transcripts produced.
    assert _matches_wake_phrase("what do you think about assistants in general")
    # Other aliases, on request -- including "mulca"/"muca" despite being
    # the transcription-unreliable word this phrase was built to avoid
    # depending on alone: an extra alternative only adds detection chances.
    assert _matches_wake_phrase("hey chatbot")
    assert _matches_wake_phrase("mulca")
    assert _matches_wake_phrase("muca")

    # "bot" added on request -- the transcription bias prompt already
    # tells the ASR this is a recognized way to address it, but the wake
    # phrase itself never matched it. Bare "bot" only, not "bot\w*": unlike
    # the other alternatives, a wildcard here would also match "bottle"/
    # "bother"/"bottom"/"botox" -- ordinary words with no relation to
    # addressing it at all, which is a materially worse false-positive
    # tradeoff than "assistants"/"chatbots" being plural forms of the same
    # concept.
    assert _matches_wake_phrase("hey bot")
    assert _matches_wake_phrase("Bot?")
    assert not _matches_wake_phrase("I forgot my bottle")
    assert not _matches_wake_phrase("don't bother")
    assert not _matches_wake_phrase("from the bottom")

    assert not _matches_wake_phrase("hello there")
    assert not _matches_wake_phrase("")


def test_wake_phrase_has_question_detects_a_real_question_in_the_same_breath():
    # "hey mulca, could you define the UNG diagram" -- a real question rode
    # along with the trigger word in the same breath, not just the trigger
    # word alone (used to decide between the bare "Listening."
    # acknowledgement and leading a real answer with it, same reply).
    assert _wake_phrase_has_question(
        "hey mulca, could you define the UNG diagram"
    )
    assert _wake_phrase_has_question("assistant what time is the next class")

    # Bare trigger word alone (plus stray punctuation/filler) -- no real
    # question rode along with it.
    assert not _wake_phrase_has_question("assistant")
    assert not _wake_phrase_has_question("Assistant?")
    assert not _wake_phrase_has_question("hey assistant")
    assert not _wake_phrase_has_question("mulca")
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
