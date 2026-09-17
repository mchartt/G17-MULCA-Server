from muca_server.cognition import CoalaPolicy, InterventionSignal, Utterance
from muca_server.cognition.memory import (
    EpisodicRecord,
    InMemoryEpisodicStore,
)
from muca_server.cognition.strategies import Strategy, StrategyStore

STRATEGIES = StrategyStore(
    {
        "participation_equality": Strategy(
            id="participation_equality",
            trigger_description="unequal participation",
            prompt_template="Balance participation. {prompt_fragment}",
            cooldown_messages=5,
        ),
        "respond_when_addressed": Strategy(
            id="respond_when_addressed",
            trigger_description="addressed directly",
            prompt_template="Answer them. {prompt_fragment}",
        ),
    }
)


class FakePort:
    def __init__(self, *, conversation_id="conv-1", is_private=False):
        self._conversation_id = conversation_id
        self._is_private = is_private
        self._participants: dict[int, str] = {}
        self.instructions: list[str] = []
        self.observer_messages: list[str] = []

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def is_private(self) -> bool:
        return self._is_private

    def participants(self) -> dict[int, str]:
        return dict(self._participants)

    async def instruct_main_agent(self, directive: str) -> None:
        self.instructions.append(directive)

    async def tell_observers(self, text: str) -> None:
        self.observer_messages.append(text)


def make_policy(**kwargs) -> tuple[CoalaPolicy, FakePort]:
    episodic = kwargs.pop("episodic_store", InMemoryEpisodicStore())
    port = kwargs.pop("port", None) or FakePort()

    policy = CoalaPolicy(
        strategy_store=STRATEGIES,
        main_persona="You are MULCA.",
        main_task="Keep the peace.",
        episodic_store=episodic,
        **kwargs,
    )
    return policy, port


def make_utterance(speaker_id=1, name="Ada", content="hello") -> Utterance:
    return Utterance(
        speaker_id=speaker_id,
        speaker_name=name,
        content=content,
        timestamp_ms=0,
    )


async def test_a_forced_mention_bypasses_the_observers():
    policy, port = make_policy()
    await policy.start(port)

    consult = await policy.on_utterance(
        make_utterance(content="@mainllm are you there?"),
    )

    assert consult is False
    assert len(port.instructions) == 1
    assert "Forced intervention" in port.instructions[0]


async def test_an_ordinary_utterance_asks_the_observers():
    policy, port = make_policy()
    await policy.start(port)

    consult = await policy.on_utterance(make_utterance())

    assert consult is True
    assert port.instructions == []


async def test_a_selected_strategy_instructs_the_main_agent():
    policy, port = make_policy()
    await policy.start(port)
    await policy.on_utterance(make_utterance())

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="they asked a direct question",
        ),
    )

    assert len(port.instructions) == 1
    assert "Answer them." in port.instructions[0]


async def test_no_signal_means_no_instruction():
    policy, port = make_policy()
    await policy.start(port)
    await policy.on_utterance(make_utterance())

    await policy.on_intervention_signal(object(), None)

    assert port.instructions == []


async def test_a_strategy_in_cooldown_is_vetoed():
    policy, port = make_policy()
    await policy.start(port)

    # Fire it once.
    await policy.on_utterance(make_utterance())
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="participation_equality",
            prompt_fragment="invite Grace",
        ),
    )
    assert len(port.instructions) == 1

    # It should not fire again immediately.
    await policy.on_utterance(make_utterance())
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="participation_equality",
            prompt_fragment="invite Grace again",
        ),
    )

    assert len(port.instructions) == 1


async def test_provide_summary_always_retrieves_regardless_of_the_signal():
    episodic = InMemoryEpisodicStore()
    await episodic.log(
        EpisodicRecord(
            timestamp=0,
            conversation_id="conv-1",
            intervention_path="none",
            speaker="Ada",
            content="the sky is blue",
        )
    )

    strategies = StrategyStore(
        {
            "provide_summary": Strategy(
                id="provide_summary",
                trigger_description="",
                prompt_template="Summarize. {prompt_fragment}",
            ),
        }
    )
    policy = CoalaPolicy(
        strategy_store=strategies,
        main_persona="p",
        main_task="t",
        episodic_store=episodic,
    )
    port = FakePort()
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="give me a summary"))

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="provide_summary",
            prompt_fragment="",
            needs_history=False,
        ),
    )

    assert "the sky is blue" in port.instructions[0]


async def test_a_private_conversation_is_never_logged():
    episodic = InMemoryEpisodicStore()
    port = FakePort(is_private=True)
    policy, _ = make_policy(episodic_store=episodic, port=port)
    await policy.start(port)

    await policy.on_utterance(make_utterance())
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="",
        ),
    )

    assert await episodic.recall_recent("conv-1", 50) == []


async def test_an_assistant_reply_is_appended_to_history():
    policy, port = make_policy()
    await policy.start(port)
    await policy.on_utterance(make_utterance())

    await policy.on_assistant_output(
        "Hello there.",
        interrupted=False,
    )

    assert policy._wm.history[-1].content == "Hello there."
    assert policy._wm.history[-1].speaker_id == 0


async def test_a_present_but_silent_participant_reaches_trigger_variables():
    # Exercises the real wiring (port.participants() -> on_utterance ->
    # compute_trigger_variables), not just the pure function in isolation --
    # this is what the roster message from the Discord/client side feeds.
    policy, port = make_policy()
    await policy.start(port)
    port._participants = {1: "Ada", 2: "Grace"}

    await policy.on_utterance(make_utterance(speaker_id=1, name="Ada"))

    variables = policy._wm.trigger_variables
    assert variables["participant_message_counts"] == {"Ada": 1, "Grace": 0}
    assert variables["gini_index"] > 0.0
