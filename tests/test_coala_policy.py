import asyncio
from datetime import date

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
            modalities=frozenset({"text", "audio"}),
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
        self._voice_participants: dict[int, str] = {}
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

    def voice_participants(self) -> dict[int, str]:
        return dict(self._voice_participants)

    async def instruct_main_agent(self, directive: str) -> None:
        self.instructions.append(directive)

    async def tell_observers(self, text: str) -> None:
        self.observer_messages.append(text)


def make_policy(**kwargs) -> tuple[CoalaPolicy, FakePort]:
    episodic = kwargs.pop("episodic_store", InMemoryEpisodicStore())
    port = kwargs.pop("port", None) or FakePort()

    policy = CoalaPolicy(
        strategy_store=STRATEGIES,
        episodic_store=episodic,
        **kwargs,
    )
    return policy, port


def make_utterance(
    speaker_id=1, name="Ada", content="hello", modality="text"
) -> Utterance:
    return Utterance(
        speaker_id=speaker_id,
        speaker_name=name,
        content=content,
        timestamp_ms=0,
        modality=modality,
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


async def test_a_spoken_turn_only_ever_gets_respond_when_addressed():
    policy, port = make_policy()
    await policy.start(port)

    await policy.on_utterance(make_utterance(modality="audio"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="participation_equality",
            prompt_fragment="invite Grace",
        ),
    )

    assert port.instructions == []


async def test_a_spoken_turns_mention_based_strategy_is_unaffected():
    policy, port = make_policy()
    await policy.start(port)

    await policy.on_utterance(make_utterance(modality="audio"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="they asked a direct question",
        ),
    )

    assert len(port.instructions) == 1
    assert "Answer them." in port.instructions[0]


async def test_a_typed_turn_is_unaffected_by_the_voice_only_veto():
    policy, port = make_policy()
    await policy.start(port)

    await policy.on_utterance(make_utterance(modality="text"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="participation_equality",
            prompt_fragment="invite Grace",
        ),
    )

    assert len(port.instructions) == 1


async def test_a_voice_only_strategy_is_vetoed_for_a_typed_turn():
    strategies = StrategyStore(
        {
            "participation_equality_voice": Strategy(
                id="participation_equality_voice",
                trigger_description="unequal voice participation",
                prompt_template="Balance voice participation. {prompt_fragment}",
                modalities=frozenset({"audio"}),
            ),
        }
    )
    policy = CoalaPolicy(
        strategy_store=strategies,
        episodic_store=InMemoryEpisodicStore(),
    )
    port = FakePort()
    await policy.start(port)

    await policy.on_utterance(make_utterance(modality="text"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="participation_equality_voice",
            prompt_fragment="invite Bob",
        ),
    )

    assert port.instructions == []


async def test_a_spoken_turn_tells_observers_only_mentions_apply():
    policy, port = make_policy()
    await policy.start(port)

    await policy.on_utterance(make_utterance(modality="audio"))

    assert "respond_when_addressed" in port.observer_messages[-1]
    assert "participation_equality" not in port.observer_messages[-1]


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


async def test_provide_summary_retrieves_text_and_voice_turns_together():
    """
    Retrieval has no modality concept at all -- text and voice turns share
    one conversation_id/log and are returned in one chronologically-ordered
    window, whichever modality asked for the summary. An untranscribed
    voice turn (no content) must not show up as a blank line.
    """

    episodic = InMemoryEpisodicStore()
    await episodic.log(
        EpisodicRecord(
            timestamp=0,
            conversation_id="conv-1",
            intervention_path="strategy",
            speaker="Ada",
            content="let's ship friday",
            modality="text",
        )
    )
    await episodic.log(
        EpisodicRecord(
            timestamp=1,
            conversation_id="conv-1",
            intervention_path="untranscribed",
            speaker="Ada",
            modality="audio",
        )
    )
    await episodic.log(
        EpisodicRecord(
            timestamp=2,
            conversation_id="conv-1",
            intervention_path="strategy",
            speaker="Ada",
            content="actually let's make it monday",
            modality="audio",
        )
    )

    strategies = StrategyStore(
        {
            "provide_summary": Strategy(
                id="provide_summary",
                trigger_description="",
                prompt_template="Summarize. {prompt_fragment}",
                modalities=frozenset({"text", "audio"}),
            ),
        }
    )
    policy = CoalaPolicy(strategy_store=strategies, episodic_store=episodic)
    port = FakePort()
    await policy.start(port)
    # A spoken question triggers the same retrieval path as a typed one.
    await policy.on_utterance(
        make_utterance(content="what did we decide?", modality="audio")
    )
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="provide_summary",
            prompt_fragment="",
            needs_history=False,
        ),
    )

    retrieved = port.instructions[0]
    assert "let's ship friday" in retrieved
    assert "actually let's make it monday" in retrieved
    # The untranscribed turn (empty content) contributes no blank line.
    assert "Ada: \n" not in retrieved


class FakeEmbedder:
    """
    Deterministic, no real API calls: a bag-of-words vector over a small
    fixed vocabulary, just enough to meaningfully rank similarity.
    """

    VOCAB = ["coffee", "tea", "budget", "deadline", "timeline"]

    async def embed(self, text: str) -> list[float]:
        lowered = text.lower()
        return [float(lowered.count(word)) for word in self.VOCAB]


async def test_remember_searches_by_the_extracted_query_not_the_whole_message():
    episodic = InMemoryEpisodicStore(embedder=FakeEmbedder())
    await episodic.log(
        EpisodicRecord(
            timestamp=0,
            conversation_id="conv-1",
            intervention_path="none",
            speaker="Ada",
            content="we should finalize the budget by Friday",
        )
    )
    await episodic.log(
        EpisodicRecord(
            timestamp=1,
            conversation_id="conv-1",
            intervention_path="none",
            speaker="Grace",
            content="I picked up a better coffee machine for the office",
        )
    )

    strategies = StrategyStore(
        {
            "remember": Strategy(
                id="remember",
                trigger_description="",
                prompt_template="Recall. {prompt_fragment}",
                modalities=frozenset({"text", "audio"}),
            ),
        }
    )
    policy = CoalaPolicy(strategy_store=strategies, episodic_store=episodic)
    port = FakePort()
    await policy.start(port)
    # The wrapper phrase ("please tell me something about") must not
    # dilute what gets embedded and searched -- only "coffee" should, so
    # the coffee record should rank above the budget one (there's no
    # relevance threshold, so with this few records both still appear --
    # see test_remember_says_so_when_nothing_is_logged_yet).
    await policy.on_utterance(
        make_utterance(content="please tell me something about coffee")
    )
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="remember", prompt_fragment=""),
    )

    retrieved = port.instructions[0]
    assert retrieved.index("coffee machine") < retrieved.index("budget")


async def test_remember_says_so_when_nothing_is_logged_yet():
    # search_similar ranks whatever candidates exist rather than applying
    # a minimum-relevance threshold -- "nothing found" is honest only
    # when the conversation has no logged content at all yet, not merely
    # a poor match. See cognition/policy.py's _retrieve_similar.
    episodic = InMemoryEpisodicStore(embedder=FakeEmbedder())

    strategies = StrategyStore(
        {
            "remember": Strategy(
                id="remember",
                trigger_description="",
                prompt_template="Recall. {prompt_fragment}",
                modalities=frozenset({"text", "audio"}),
            ),
        }
    )
    policy = CoalaPolicy(strategy_store=strategies, episodic_store=episodic)
    port = FakePort()
    await policy.start(port)
    await policy.on_utterance(
        make_utterance(content="tell me something about the vacation policy")
    )
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="remember", prompt_fragment=""),
    )

    assert "nothing found" in port.instructions[0]
    assert "vacation policy" in port.instructions[0]


async def test_a_private_conversations_turns_are_logged_too():
    """
    A DM's own episodic log is an audit record now, not a memory source --
    see test_a_dm_history_lookup_reads_the_linked_groups_log_only for the
    other half: this log is written, but never read back for retrieval.
    """

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
    # The record is deferred until the reply arrives (so it captures what
    # MULCA actually said, not just what triggered it).
    await policy.on_assistant_output("hi there", interrupted=False)

    records = await episodic.recall_recent("conv-1", 50)
    assert [r.content for r in records] == ["hello"]
    assert records[0].assistant_output == "hi there"


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


async def test_text_and_voice_participation_are_measured_separately():
    """
    Text and voice have their own separate rosters and their own separate
    turn counts: someone present in the voice call who never types (Bob,
    Eve) has no text participation to measure, and vice versa (Ada, Grace).
    One shared gini_index/participant_message_counts would conflate two
    different audiences.
    """

    policy, port = make_policy()
    await policy.start(port)
    port._participants = {1: "Ada", 2: "Grace"}
    port._voice_participants = {3: "Bob", 4: "Eve"}

    await policy.on_utterance(
        make_utterance(speaker_id=1, name="Ada", modality="text"),
    )
    await policy.on_intervention_signal(object(), None)
    await policy.on_utterance(
        make_utterance(speaker_id=3, name="Bob", modality="audio"),
    )
    await policy.on_intervention_signal(object(), None)

    variables = policy._wm.trigger_variables
    assert variables["participant_message_counts"] == {"Ada": 1, "Grace": 0}
    assert variables["voice_participant_message_counts"] == {"Bob": 1, "Eve": 0}


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


async def test_a_groups_own_summary_is_sent_once_then_not_again():
    """
    A group's own live session already has everything its summary covers
    (it was there when it was said), so resending it every turn is just
    waste -- unlike a DM reading a linked group's summary, which has no
    other way to see that content at all (see the DM test below).
    """

    policy, port = make_policy()
    await policy.start(port)
    policy._wm.history_summary = "Previously, the team discussed the deadline."

    await policy.on_utterance(make_utterance(content="first message"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )
    await policy.on_assistant_output("ok", interrupted=False)

    await policy.on_utterance(make_utterance(content="second message"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    assert "Previously, the team discussed the deadline." in port.instructions[0]
    assert "Previously, the team discussed the deadline." not in port.instructions[1]

    # Without a course schedule, nothing about dates or deliverables.
    assert "Today is" not in port.instructions[0]

    # The live session hears every message itself after connecting, so the
    # verbatim tail is not resent (and billed again) with every reply.
    assert "Recent messages in this conversation" in port.instructions[0]
    assert "Recent messages in this conversation" not in port.instructions[1]


async def test_every_reply_is_told_today_and_the_next_deliverable():
    schedule = {
        "timezone": "Europe/Rome",
        "groups": {"A": "Mondays"},
        "deliverables": [
            {"what": "the moon landing plan", "due": {"A": date(2999, 1, 7)}},
        ],
        "then": "nothing",
    }
    policy, port = make_policy(schedule=schedule)
    await policy.start(port)

    for content in ("first message", "second message"):
        await policy.on_utterance(make_utterance(content=content))
        await policy.on_intervention_signal(
            object(),
            InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
        )
        await policy.on_assistant_output("ok", interrupted=False)

    for prompt in port.instructions:
        assert "Today is " in prompt
        assert "Group A: the moon landing plan, at the tutoring on Mon 7 Jan 2999" in prompt


async def test_a_dms_linked_summary_is_sent_every_turn_not_just_once():
    """
    Unlike a group's own summary, a DM's session was never part of the
    linked group's conversation -- there is no live fallback, so this must
    keep refreshing every turn, not just the first.
    """

    stores: dict[str, tuple] = {}

    def store_factory(conversation_id):
        return stores.setdefault(
            conversation_id,
            (InMemoryEpisodicStore(), InMemorySnapshotStore()),
        )

    _, group_snapshot_store = store_factory("instance:team-alpha")
    await group_snapshot_store.save(
        "instance:team-alpha",
        MemorySnapshot(history_summary="The team discussed the deadline."),
    )

    policy, port = make_policy(
        store_factory=store_factory,
        port=FakePort(
            conversation_id="dm:1",
            is_private=True,
            linked_conversation_id="instance:team-alpha",
        ),
    )
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="first message"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )
    await policy.on_assistant_output("ok", interrupted=False)

    await policy.on_utterance(make_utterance(content="second message"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    assert "The team discussed the deadline." in port.instructions[0]
    assert "The team discussed the deadline." in port.instructions[1]


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


async def test_cooldown_survives_a_reconnect_not_just_total_message_count():
    """
    Found live: a reconnect (OpenAI's 60-minute limit, the Watchdog's tier-1
    recovery, a network blip) builds a brand-new WorkingMemory and restored
    only history_summary/participant_roles_summary/total_message_count/
    history -- cooldown state silently reset to empty, so a strategy that
    had just fired could immediately fire again right after a reconnect.
    Reproduced here as two separate CoalaPolicy instances sharing one
    snapshot_store, standing in for before/after a real reconnect.
    """

    snapshot_store = InMemorySnapshotStore()
    episodic_store = InMemoryEpisodicStore()

    first = CoalaPolicy(
        strategy_store=STRATEGIES,
        episodic_store=episodic_store,
        snapshot_store=snapshot_store,
    )
    port = FakePort()
    await first.start(port)
    await first.on_utterance(make_utterance())
    await first.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="participation_equality",
            prompt_fragment="invite Grace",
        ),
    )
    assert len(port.instructions) == 1

    await first.close()  # Saves a snapshot, same as a real session ending.

    # A fresh CoalaPolicy, same conversation_id -- standing in for the
    # session reopening after a reconnect.
    second = CoalaPolicy(
        strategy_store=STRATEGIES,
        episodic_store=episodic_store,
        snapshot_store=snapshot_store,
    )
    second_port = FakePort(conversation_id=port.conversation_id)
    await second.start(second_port)

    await second.on_utterance(make_utterance())
    await second.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="participation_equality",
            prompt_fragment="invite Grace again",
        ),
    )

    # Still in cooldown -- restored from the snapshot, not reset.
    assert second_port.instructions == []


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


async def test_episodic_log_attributes_to_the_pending_utterance_not_a_later_one():
    # The actual bug this covers: _finish_turn used to read wm.history[-1],
    # which could already be a *different* utterance by the time an
    # observer's decision arrives (its callback carries no reference to
    # which utterance it was judging) -- found from a real duplicate/
    # missing episodic-log entry, not hypothetically.
    episodic_store = InMemoryEpisodicStore()
    policy, port = make_policy(episodic_store=episodic_store)
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="message A"))

    # Stands in for a second utterance's on_utterance() call landing before
    # this decision resolves.
    policy._wm.append_history(make_utterance(content="message B"))

    await policy.on_intervention_signal(object(), None)

    [record] = await episodic_store.recall_recent(port.conversation_id, 50)
    assert record.content == "message A"


async def test_the_episodic_log_captures_the_assistants_actual_reply():
    # Nothing wrote this before at all -- the log wrote strategy_id/
    # prompt_fragment (why MULCA was triggered), never what MULCA actually
    # said, since the reply text does not exist yet at _finish_turn time.
    episodic_store = InMemoryEpisodicStore()
    policy, port = make_policy(episodic_store=episodic_store)
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="hi"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    # Deferred: not written yet, since the reply hasn't arrived.
    assert await episodic_store.recall_recent(port.conversation_id, 50) == []

    await policy.on_assistant_output("Hello there.", interrupted=False)

    [record] = await episodic_store.recall_recent(port.conversation_id, 50)
    assert record.content == "hi"
    assert record.assistant_output == "Hello there."


async def test_observer_catchup_text_is_empty_with_no_history():
    policy, port = make_policy()
    await policy.start(port)

    assert policy.observer_catchup_text() == ""


async def test_observer_catchup_text_carries_restored_history():
    """
    After a restart, WM's history is restored from a snapshot before any
    observer exists to have seen it live -- this is what a freshly
    connecting observer gets instead, so it isn't judging blind.
    """

    policy, port = make_policy()
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="the deadline is friday"))

    catchup = policy.observer_catchup_text()

    assert "the deadline is friday" in catchup
    # Raw history, never the compressed summary -- an observer only ever
    # reads wm.history/trigger variables directly, same as every other turn.
    assert "Summary of the conversation" not in catchup


async def test_an_untranscribed_turn_is_logged_without_touching_wm():
    """
    A voice turn with no transcript (noise, provider failure) still gets an
    audit row -- every other outcome does, this shouldn't be the silent
    exception -- but there's no content to add to conversational memory or
    judge, so WM itself (history, message count) is untouched.
    """

    episodic = InMemoryEpisodicStore()
    policy, port = make_policy(episodic_store=episodic)
    await policy.start(port)

    await policy.on_untranscribed_turn(1, "Mats", 1000)

    records = await episodic.recall_recent(port.conversation_id, 50)
    assert len(records) == 1
    assert records[0].intervention_path == "untranscribed"
    assert records[0].speaker == "Mats"
    assert records[0].modality == "audio"
    assert records[0].content == ""

    assert policy._wm.history == []
    assert policy._wm.total_message_count == 0


async def test_wait_for_turn_to_finish_resolves_immediately_when_nothing_fired():
    policy, port = make_policy()
    await policy.start(port)
    await policy.on_utterance(make_utterance())

    waiter = asyncio.create_task(policy.wait_for_turn_to_finish())
    await asyncio.sleep(0)
    assert not waiter.done()

    await policy.on_intervention_signal(object(), None)

    await asyncio.wait_for(waiter, timeout=1)


async def test_wait_for_turn_to_finish_waits_for_the_reply_when_one_fired():
    policy, port = make_policy()
    await policy.start(port)
    await policy.on_utterance(make_utterance())

    waiter = asyncio.create_task(policy.wait_for_turn_to_finish())
    await asyncio.sleep(0)

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )
    await asyncio.sleep(0)
    assert not waiter.done()  # a reply was triggered -- still waiting on it

    await policy.on_assistant_output("Hi!", interrupted=False)

    await asyncio.wait_for(waiter, timeout=1)


async def test_wait_for_turn_to_finish_times_out_instead_of_hanging_forever():
    episodic_store = InMemoryEpisodicStore()
    policy, port = make_policy(
        episodic_store=episodic_store,
        turn_timeout_seconds=0.05,
    )
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="hi"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )
    # on_assistant_output is never called -- e.g. interrupted, or the model
    # produced nothing.

    await asyncio.wait_for(policy.wait_for_turn_to_finish(), timeout=1)

    # The pending record still gets flushed, just without assistant_output.
    [record] = await episodic_store.recall_recent(port.conversation_id, 50)
    assert record.content == "hi"
    assert record.assistant_output is None


# ----------------------------------------------------------------------
# Memory scoping
# ----------------------------------------------------------------------


class EphemeralPort(FakePort):
    is_ephemeral = True


async def test_a_session_nobody_named_keeps_its_memory_in_process_only():
    requested: list[str] = []

    def store_factory(conversation_id):
        requested.append(conversation_id)
        return InMemoryEpisodicStore(), InMemorySnapshotStore()

    policy = CoalaPolicy(
        strategy_store=STRATEGIES,
        store_factory=store_factory,
    )
    await policy.start(EphemeralPort(conversation_id="a-random-uuid"))

    assert requested == []
    assert isinstance(policy._episodic_store, InMemoryEpisodicStore)


async def test_an_ephemeral_sessions_in_process_store_still_gets_an_embedder():
    """
    Found live: an ephemeral session's throwaway InMemoryEpisodicStore was
    built with no embedder at all, so 'remember' raised instead of
    degrading -- caught by the try/except in _retrieve_similar, but it
    shouldn't have needed to reach there in the first place for a
    perfectly configured server.
    """

    strategies = StrategyStore(
        {
            "remember": Strategy(
                id="remember",
                trigger_description="",
                prompt_template="Recall. {prompt_fragment}",
                modalities=frozenset({"text", "audio"}),
            ),
        }
    )
    policy = CoalaPolicy(
        strategy_store=strategies,
        store_factory=lambda conversation_id: (None, None),
        embedder=FakeEmbedder(),
    )
    port = EphemeralPort(conversation_id="a-random-uuid")
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="tell me about coffee"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="remember", prompt_fragment=""),
    )

    assert "couldn't search memory" not in port.instructions[0]


async def test_closing_logs_a_turn_whose_reply_never_arrived():
    episodic = InMemoryEpisodicStore()
    policy, port = make_policy(episodic_store=episodic)
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="anyone?"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="asked",
        ),
    )

    await policy.close()

    records = await episodic.recall_recent("conv-1", 10)
    assert [r.content for r in records] == ["anyone?"]
    assert records[0].assistant_output is None


async def test_a_dm_history_lookup_reads_the_linked_groups_log_only():
    stores: dict[str, tuple] = {}

    def store_factory(conversation_id):
        if conversation_id not in stores:
            stores[conversation_id] = (
                InMemoryEpisodicStore(),
                InMemorySnapshotStore(),
            )
        return stores[conversation_id]

    group_log, _ = store_factory("instance:team-alpha")
    await group_log.log(
        EpisodicRecord(
            timestamp=1.0,
            conversation_id="instance:team-alpha",
            intervention_path="strategy",
            speaker="Ada",
            content="the deadline is friday",
        )
    )

    port = FakePort(
        conversation_id="dm:1",
        is_private=True,
        linked_conversation_id="instance:team-alpha",
    )
    policy = CoalaPolicy(
        strategy_store=STRATEGIES,
        store_factory=store_factory,
        assistant_name="MULCA",
    )
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="when is it due?"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="asked about the deadline",
            needs_history=True,
        ),
    )
    await policy.on_assistant_output("it's due friday", interrupted=False)

    assert "the deadline is friday" in port.instructions[-1]
    # The DM's own store gets an audit record of its own turn (it isn't a
    # memory source, but it isn't a black hole either) -- the retrieval
    # above still only ever pulled from the linked group's log.
    dm_log, _ = stores["dm:1"]
    dm_records = await dm_log.recall_recent("dm:1", 10)
    assert [r.content for r in dm_records] == ["when is it due?"]
