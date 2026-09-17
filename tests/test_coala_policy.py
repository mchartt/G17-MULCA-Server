from muca_server.cognition import CoalaPolicy, InterventionSignal, Utterance
from muca_server.cognition.memory import (
    EpisodicRecord,
    InMemoryEpisodicStore,
    InMemorySnapshotStore,
    MemorySnapshot,
    StoredUtterance,
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
    def __init__(
        self,
        *,
        conversation_id="conv-1",
        is_private=False,
        linked_conversation_id=None,
    ):
        self._conversation_id = conversation_id
        self._is_private = is_private
        self._linked_conversation_id = linked_conversation_id
        self._participants: dict[int, str] = {}
        self.instructions: list[str] = []
        self.observer_messages: list[str] = []

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def is_private(self) -> bool:
        return self._is_private

    @property
    def linked_conversation_id(self) -> str | None:
        return self._linked_conversation_id

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


async def test_an_assistant_reply_is_labeled_by_name_not_user_0():
    # The actual bug this covers: assistant_name defaulting to "" meant
    # Utterance.label() fell back to f"User {speaker_id}" ("User 0"), so the
    # summarizer (and anything else reading wm.history) saw MULCA's own
    # replies as if they were an anonymous participant.
    policy, port = make_policy(assistant_name="MULCA")
    await policy.start(port)
    await policy.on_utterance(make_utterance())

    await policy.on_assistant_output("Hello there.", interrupted=False)

    assert policy._wm.history[-1].label() == "MULCA"


async def test_the_summarizer_never_sees_the_assistants_own_turns():
    class RecordingSummarizer(FakeSummarizer):
        def __init__(self):
            self.seen_transcripts = []

        async def summarize(self, *, transcript, previous_summary, previous_roles):
            self.seen_transcripts.append(transcript)
            return await super().summarize(
                transcript=transcript,
                previous_summary=previous_summary,
                previous_roles=previous_roles,
            )

    summarizer = RecordingSummarizer()
    policy, port = make_policy(
        assistant_name="MULCA",
        summarizer=summarizer,
        management_cycle_interval=1,
    )
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="hi MULCA"))
    await policy.on_assistant_output("Hello there.", interrupted=False)
    await policy.on_intervention_signal(object(), None)

    assert "hi MULCA" in summarizer.seen_transcripts[0]
    assert "Hello there." not in summarizer.seen_transcripts[0]
    assert "MULCA:" not in summarizer.seen_transcripts[0]


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


class FakeSummarizer:
    async def summarize(self, *, transcript, previous_summary, previous_roles):
        return "a summary", "some roles"


async def test_store_factory_is_called_with_conversation_id_once_start_runs():
    seen_conversation_ids = []

    def store_factory(conversation_id):
        seen_conversation_ids.append(conversation_id)
        return InMemoryEpisodicStore(), InMemorySnapshotStore()

    policy, port = make_policy(
        episodic_store=None,
        store_factory=store_factory,
        port=FakePort(conversation_id="channel-42"),
    )

    assert seen_conversation_ids == []  # not called yet -- conversation_id unknown

    await policy.start(port)

    assert seen_conversation_ids == ["channel-42"]


async def test_a_management_cycle_saves_a_snapshot_not_just_close():
    snapshot_store = InMemorySnapshotStore()
    policy, port = make_policy(
        summarizer=FakeSummarizer(),
        snapshot_store=snapshot_store,
        management_cycle_interval=1,
    )
    await policy.start(port)

    await policy.on_utterance(make_utterance())
    await policy.on_intervention_signal(object(), None)

    saved = await snapshot_store.load(port.conversation_id)

    assert saved is not None
    assert saved.history_summary == "a summary"
    assert saved.participant_roles_summary == "some roles"


async def test_the_verbatim_tail_is_bounded_by_management_cycle_interval_not_a_fixed_constant():
    policy, port = make_policy(
        summarizer=FakeSummarizer(),
        management_cycle_interval=3,
    )
    await policy.start(port)

    for i in range(5):
        await policy.on_utterance(make_utterance(content=f"message {i}"))
        await policy.on_intervention_signal(object(), None)

    # 5 messages, cycle every 3rd -> one cycle ran (at message 3), truncating
    # to the last 3 -- then 2 more messages appended after that.
    assert len(policy._wm.history) <= 3 + 2


async def test_a_dm_from_a_recognized_participant_mirrors_the_group_summary():
    stores: dict[str, tuple] = {}

    def store_factory(conversation_id):
        return stores.setdefault(
            conversation_id,
            (InMemoryEpisodicStore(), InMemorySnapshotStore()),
        )

    _, group_snapshot_store = store_factory("instance:team-alpha")
    await group_snapshot_store.save(
        "instance:team-alpha",
        MemorySnapshot(
            history_summary="the group discussed the deadline",
            participant_roles_summary="Ada leads, Grace is quiet",
        ),
    )

    policy, port = make_policy(
        store_factory=store_factory,
        port=FakePort(
            conversation_id="dm:42",
            is_private=True,
            linked_conversation_id="instance:team-alpha",
        ),
    )
    await policy.start(port)
    await policy.on_utterance(make_utterance())

    # Not a separate field -- the exact same fields/rendering a group uses
    # for its own memory (_render_prompt's summary_block).
    assert policy._wm.history_summary == "the group discussed the deadline"
    assert policy._wm.participant_roles_summary == "Ada leads, Grace is quiet"


async def test_linked_group_summary_refreshes_on_every_message_not_just_once():
    # The actual point: a DM session can stay open and get reused by the
    # same student across a whole deployment (weeks/months) -- seeding this
    # only once at start(), forever, would freeze it on whatever the group
    # looked like the first time that student ever DMed.
    stores: dict[str, tuple] = {}

    def store_factory(conversation_id):
        return stores.setdefault(
            conversation_id,
            (InMemoryEpisodicStore(), InMemorySnapshotStore()),
        )

    _, group_snapshot_store = store_factory("instance:team-alpha")
    await group_snapshot_store.save(
        "instance:team-alpha",
        MemorySnapshot(history_summary="week 1: kickoff"),
    )

    policy, port = make_policy(
        store_factory=store_factory,
        port=FakePort(
            conversation_id="dm:42",
            is_private=True,
            linked_conversation_id="instance:team-alpha",
        ),
    )
    await policy.start(port)
    await policy.on_utterance(make_utterance())
    assert policy._wm.history_summary == "week 1: kickoff"

    # The group's memory moves on while this DM session stays open.
    await group_snapshot_store.save(
        "instance:team-alpha",
        MemorySnapshot(history_summary="week 10: launch went well"),
    )

    await policy.on_utterance(make_utterance())
    assert policy._wm.history_summary == "week 10: launch went well"


async def test_a_dm_from_an_unrecognized_sender_gets_no_linked_summary():
    policy, port = make_policy(
        port=FakePort(
            conversation_id="dm:99",
            is_private=True,
            linked_conversation_id=None,
        ),
    )
    await policy.start(port)
    await policy.on_utterance(make_utterance())

    assert policy._wm.history_summary == ""
    assert policy._wm.participant_roles_summary == ""


async def test_a_group_conversation_is_unaffected_by_linked_conversation_id():
    # linked_conversation_id is a DM-only concept; even if a port somehow
    # set one, is_private=False must still suppress it.
    policy, port = make_policy(
        port=FakePort(
            conversation_id="group-1",
            is_private=False,
            linked_conversation_id="instance:team-alpha",
        ),
    )
    await policy.start(port)
    await policy.on_utterance(make_utterance())

    assert policy._wm.history_summary == ""
    assert policy._wm.participant_roles_summary == ""


async def test_a_dm_gets_no_summarization_of_its_own_just_a_history_trim():
    policy, port = make_policy(
        summarizer=FakeSummarizer(),
        port=FakePort(conversation_id="dm:42", is_private=True),
        management_cycle_interval=2,
    )
    await policy.start(port)

    for i in range(4):
        await policy.on_utterance(make_utterance(content=f"message {i}"))
        await policy.on_intervention_signal(object(), None)

    # No summarizer call happened for this DM (FakeSummarizer.summarize
    # would have overwritten history_summary with "a summary" if it had) --
    # trimming is the only thing that happens, bounding raw history.
    assert policy._wm.history_summary == ""
    assert len(policy._wm.history) <= 2 + 1


async def test_the_prompt_includes_this_conversation_verbatim_not_just_the_summary():
    # Covers the restart case: a freshly-connected session has never
    # actually seen wm.history (restored from a snapshot), so it has to
    # reach the model through the prompt text, not just the live session.
    policy, port = make_policy()
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="the number is 6"))

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    assert "the number is 6" in port.instructions[0]


async def test_a_groups_snapshot_saves_before_the_first_management_cycle():
    # The bug this covers: "Mats says X in the group, then immediately DMs
    # about X" failing because nothing had been saved yet -- a snapshot only
    # existed once every management_cycle_interval messages, or on close().
    snapshot_store = InMemorySnapshotStore()
    policy, port = make_policy(
        snapshot_store=snapshot_store,
        management_cycle_interval=8,
    )
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="the number is 6"))
    await policy.on_intervention_signal(object(), None)

    saved = await snapshot_store.load(port.conversation_id)

    assert saved is not None
    assert any(u.content == "the number is 6" for u in saved.history_tail)


async def test_a_dm_prompt_includes_the_linked_groups_verbatim_tail():
    stores: dict[str, tuple] = {}

    def store_factory(conversation_id):
        return stores.setdefault(
            conversation_id,
            (InMemoryEpisodicStore(), InMemorySnapshotStore()),
        )

    _, group_snapshot_store = store_factory("instance:team-alpha")
    await group_snapshot_store.save(
        "instance:team-alpha",
        MemorySnapshot(
            history_tail=[
                StoredUtterance(
                    speaker_id=1,
                    speaker_name="Mats",
                    content="the number is 6",
                    timestamp_ms=1,
                ),
            ],
        ),
    )

    policy, port = make_policy(
        store_factory=store_factory,
        port=FakePort(
            conversation_id="dm:153532335998894089",
            is_private=True,
            linked_conversation_id="instance:team-alpha",
        ),
    )
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="what is the number?"))

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    assert "Mats: the number is 6" in port.instructions[0]
