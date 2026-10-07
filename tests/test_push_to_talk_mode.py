"""
push_to_talk listening mode: the intervention agent is only consulted
inside a window opened by ArmListeningMessage, never hears raw audio at
all (regardless of window state), and the window auto-closes after
silence. Ambient mode (the default) must behave exactly as before.
"""

import asyncio
from uuid import uuid4

from muca_server.agents import AuxiliaryAgent, MainAgent, MainAgentOptions
from muca_server.agents.conversation import Conversation
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
            trigger_description="push_to_talk fallback",
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
    listening_mode: str = "push_to_talk",
    ptt_silence_ms: int = 50,
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
        ptt_silence_ms=ptt_silence_ms,
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


async def test_the_window_closes_itself_after_silence():
    conversation, _ = await make_conversation(ptt_silence_ms=20)

    await conversation.arm_listening(arm())
    assert conversation._listening_window_open is True

    await asyncio.sleep(0.05)

    assert conversation._listening_window_open is False


async def test_a_judged_turn_inside_the_window_restarts_its_timer():
    conversation, _ = await make_conversation(ptt_silence_ms=40)

    await conversation.arm_listening(arm())
    await asyncio.sleep(0.03)
    # always_reply=True: see the comment in the test above.
    await conversation._judge(utterance(always_reply=True))  # restarts the 40ms timer
    await conversation.wait_until_idle()

    await asyncio.sleep(0.03)
    assert conversation._listening_window_open is True  # would be closed otherwise

    await asyncio.sleep(0.03)
    assert conversation._listening_window_open is False


async def test_text_turns_are_judged_normally_even_with_a_closed_window():
    # push_to_talk is a voice-UX concept (the problem it fixes was
    # continuous audio judgment); a text message or DM on a push_to_talk
    # link must not be silenced behind the button.
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
    # all in push_to_talk, only _judge()'s text push.
    await conversation.arm_listening(arm())
    await conversation.route_user_audio(audio(speaker=1))

    assert factory.auxiliary.audio_chunks_received == 0


async def test_a_per_link_handshake_override_wins_over_the_servers_own_mode():
    # The server defaults to "ambient" (as if MUCA_LISTENING_MODE were
    # unset); this link's own ConnectAgentMessage asks for push_to_talk --
    # same override precedence as mainModel/interventionModel.
    conversation, factory = await make_conversation(listening_mode="ambient")

    await conversation.connect_agent(
        parse_incoming_message(
            {"event": "connect", "listeningMode": "push_to_talk"}
        )
    )

    await conversation._judge(utterance())
    await conversation.wait_until_idle()

    assert factory.auxiliary.asked == 0  # closed window: never asked


async def test_a_declined_turn_inside_a_window_falls_back_to_always_reply():
    # The intervention agent itself chose "do nothing" -- push_to_talk's
    # guarantee overrides that, same as a hard veto, rather than letting
    # an open window ever produce silence.
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


async def test_always_reply_never_fires_outside_push_to_talk():
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
