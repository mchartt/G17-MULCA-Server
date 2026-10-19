import asyncio
import time

from muca_server.cognition import CoalaPolicy, InterventionSignal, Utterance
from muca_server.cognition.policy import INTERIM_ACK_MAX_TOKENS
from muca_server.cognition.memory import (
    EpisodicRecord,
    InMemoryEpisodicStore,
    InMemorySnapshotStore,
    MemorySnapshot,
    MemorySummary,
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


STRATEGIES_WITH_RIDERS = StrategyStore(
    {
        "respond_when_addressed": Strategy(
            id="respond_when_addressed",
            trigger_description="addressed directly",
            prompt_template="Answer them. {prompt_fragment}",
            modalities=frozenset({"text", "audio"}),
        ),
        "pleasant_conversation": Strategy(
            id="pleasant_conversation",
            trigger_description="offensive language",
            prompt_template="Address the offense. {prompt_fragment}",
            overlay_template="Also briefly note the offense.",
        ),
        "participation_equality_text": Strategy(
            id="participation_equality_text",
            trigger_description="unequal participation",
            prompt_template="Balance participation. {prompt_fragment}",
            cooldown_messages=5,
            overlay_template="Also briefly invite the quiet one.",
        ),
    }
)


STRATEGIES_WITH_ALWAYS_REPLY = StrategyStore(
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
        "always_reply": Strategy(
            id="always_reply",
            trigger_description="toggle/transcribe fallback",
            prompt_template="Respond directly. {prompt_fragment}",
            modalities=frozenset({"audio"}),
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
        self.instruction_max_tokens: list[int | None] = []
        self.observer_messages: list[str] = []
        self.no_reply_count = 0
        self.resets: list[str] = []

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

    async def instruct_main_agent(
        self,
        directive: str,
        *,
        max_tokens: int | None = None,
    ) -> None:
        self.instructions.append(directive)
        self.instruction_max_tokens.append(max_tokens)

    async def tell_observers(self, text: str) -> None:
        self.observer_messages.append(text)

    async def no_reply(self) -> None:
        self.no_reply_count += 1

    async def reset_agent_contexts(self, observer_memory: str) -> None:
        self.resets.append(observer_memory)


def make_policy(**kwargs) -> tuple[CoalaPolicy, FakePort]:
    episodic = kwargs.pop("episodic_store", InMemoryEpisodicStore())
    port = kwargs.pop("port", None) or FakePort()
    strategy_store = kwargs.pop("strategy_store", STRATEGIES)

    policy = CoalaPolicy(
        strategy_store=strategy_store,
        episodic_store=episodic,
        **kwargs,
    )
    return policy, port


def make_utterance(
    speaker_id=1,
    name="Ada",
    content="hello",
    modality="text",
    is_wake_phrase=False,
    always_reply=False,
    wake_phrase_has_question=False,
    judge_wake_question=False,
    timestamp_ms=0,
) -> Utterance:
    return Utterance(
        speaker_id=speaker_id,
        speaker_name=name,
        content=content,
        timestamp_ms=timestamp_ms,
        modality=modality,
        is_wake_phrase=is_wake_phrase,
        always_reply=always_reply,
        wake_phrase_has_question=wake_phrase_has_question,
        judge_wake_question=judge_wake_question,
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


async def test_a_wake_phrase_gets_an_acknowledgement_not_the_observers():
    # "transcribe" mode only: conversation.py already opened the listening
    # window itself before this is ever called (that state lives there,
    # not in CoalaPolicy) -- this just confirms a forced acknowledgement
    # directive fires instead of a normal judged reply. Described, not
    # scripted: the main agent is asked to acknowledge in its own words,
    # not forced to say an exact fixed phrase.
    policy, port = make_policy()
    await policy.start(port)

    consult = await policy.on_utterance(
        make_utterance(content="hey assistant", modality="audio", is_wake_phrase=True),
    )

    assert consult is False
    assert len(port.instructions) == 1
    assert "acknowledge" in port.instructions[0].lower()
    assert "your own words" in port.instructions[0].lower()


async def test_a_wake_phrase_with_a_question_in_the_same_breath_just_gets_answered():
    # "wake" mode only (judge_wake_question=False): "hey mulca, could you
    # define the UNG diagram" -- not just the bare trigger word, but still
    # answered immediately with no observer judgement, since "wake" judges
    # only later, engaged turns, never the one that starts the session.
    # Found live: explicitly asking for an acknowledgement here too
    # (alongside the answer) produced a stilted "I heard you, ..." preamble
    # before every single answer -- a real question just gets answered
    # directly, no acknowledgement needed at all, unlike the bare
    # trigger-word-alone case (which still gets one, since there's nothing
    # yet to answer).
    policy, port = make_policy()
    await policy.start(port)

    consult = await policy.on_utterance(
        make_utterance(
            content="hey mulca, could you define the UNG diagram",
            modality="audio",
            is_wake_phrase=True,
            wake_phrase_has_question=True,
        ),
    )

    assert consult is False
    assert len(port.instructions) == 1
    assert "no need to acknowledge" in port.instructions[0].lower()
    assert "answer" in port.instructions[0].lower()
    assert "answer" in port.instructions[0].lower()


async def test_a_transcribe_wake_question_is_judged_so_it_can_get_a_web_search():
    # "transcribe"/"toggle" (judge_wake_question=True): found live,
    # "MULCA, could you look up the latest videos by X?" used to take the
    # immediate-answer shortcut above with no observer judgement at all, so
    # needs_web_search could never be set for a question asked in the same
    # breath as the wake phrase -- MULCA just answered from what it already
    # knew ("I can't look that up"). It must now reach the observer like
    # any other addressed turn.
    policy, port = make_policy(
        strategy_store=STRATEGIES_WITH_ALWAYS_REPLY,
        web_searcher=FakeWebSearcher(),
    )
    await policy.start(port)

    consult = await policy.on_utterance(
        make_utterance(
            content="MULCA, could you look up the latest videos by Dark Dark?",
            modality="audio",
            is_wake_phrase=True,
            wake_phrase_has_question=True,
            judge_wake_question=True,
        ),
    )

    assert consult is True
    assert port.instructions == []  # nothing forced yet -- awaiting judgement

    # Even if the observer fails to produce any decision at all (the same
    # live failure mode already seen for a later follow-up question), the
    # "toggle"/"transcribe" guarantee still forces a reply.
    await policy.on_intervention_signal(object(), None)

    assert len(port.instructions) == 1
    assert "Respond directly" in port.instructions[0]


async def test_the_directive_names_who_the_reply_is_for():
    # Found live: not told who a reply was for, the main agent hedged about
    # who was speaking, though the platform labels every turn.
    policy, port = make_policy()
    await policy.start(port)

    await policy.on_utterance(
        make_utterance(content="hey assistant", modality="audio", is_wake_phrase=True),
    )

    assert "Replying to: Ada (audio)" in port.instructions[0]


async def test_an_ordinary_utterance_asks_the_observers():
    policy, port = make_policy()
    await policy.start(port)

    consult = await policy.on_utterance(make_utterance())

    assert consult is True
    assert port.instructions == []


async def test_the_directive_is_grounded_in_the_latest_utterances_own_timestamp():
    # "what are upcoming deadlines" needs an actual date to be "upcoming"
    # relative to -- sourced from the most recent utterance's own
    # timestamp_ms (already tracked, already in wm.history), not a fresh
    # system clock read, so it matches whenever this turn actually
    # happened rather than whenever the prompt happens to be rendered.
    policy, port = make_policy()
    await policy.start(port)

    # 2026-03-08T12:00:00 UTC.
    await policy.on_utterance(make_utterance(timestamp_ms=1772971200000))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="they asked about deadlines",
        ),
    )

    assert len(port.instructions) == 1
    assert "2026-03-08" in port.instructions[0]


async def test_the_date_is_only_re_announced_when_the_calendar_day_changes():
    # Repeating the same date on every single reply over a session that
    # can run for hours is pure waste once it was already established
    # earlier in the connection -- only worth repeating once it's actually
    # a new day.
    policy, port = make_policy()
    await policy.start(port)

    for strategy_id, fragment in [
        ("respond_when_addressed", "first"),
        ("respond_when_addressed", "second same day"),
    ]:
        await policy.on_utterance(make_utterance(timestamp_ms=1772971200000))
        await policy.on_intervention_signal(
            object(),
            InterventionSignal(strategy_id=strategy_id, prompt_fragment=fragment),
        )

    assert "2026-03-08" in port.instructions[0]
    assert "2026-03-08" not in port.instructions[1]

    # The next day.
    await policy.on_utterance(make_utterance(timestamp_ms=1773057600000))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="third, a new day",
        ),
    )

    assert len(port.instructions) == 3
    assert "2026-03-09" in port.instructions[2]


async def test_the_directive_carries_the_live_voice_roster_not_just_who_spoke():
    # "who is in the chat" was being answered from the speaker-tagged
    # transcript alone (only people actually heard from), missing anyone
    # present in the voice channel but silent so far -- the real roster
    # (port.voice_participants()) is a separate, already-tracked thing.
    policy, port = make_policy()
    await policy.start(port)
    port._voice_participants = {1: "Ada", 7: "Mats", 9: "Grace"}

    await policy.on_utterance(make_utterance())
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="they asked who's here",
        ),
    )

    assert len(port.instructions) == 1
    assert "Ada" in port.instructions[0]
    assert "Mats" in port.instructions[0]
    assert "Grace" in port.instructions[0]


async def test_an_empty_voice_roster_adds_nothing_to_the_directive():
    # A text-only link (nobody in a voice channel at all) shouldn't carry
    # an empty "Currently in the voice channel:" line.
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
    assert "Currently in the voice channel" not in port.instructions[0]


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
    # So a client can stop showing "a reply is coming" UI (e.g. a typing
    # indicator) immediately instead of waiting out its own timeout.
    assert port.no_reply_count == 1


async def test_no_reply_is_not_sent_when_a_strategy_fires():
    policy, port = make_policy()
    await policy.start(port)
    await policy.on_utterance(make_utterance())

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    assert port.no_reply_count == 0


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


async def test_always_reply_fires_when_the_model_chooses_it_inside_an_open_window():
    # Still legitimate to test directly against CoalaPolicy/on_intervention_
    # signal: the guarantee that an ambient conversation's own observer
    # never sees always_reply as an option at all lives one layer up, in
    # which StrategyStore gets built into that conversation's own
    # InterventionAgentOpenAI (OpenAIAgentFactory.build_auxiliary) -- see
    # test_openai_agent_factory.py for that. This only confirms the
    # forced-reply path itself still honors a model's own direct selection
    # when the utterance says it may.
    policy, port = make_policy(strategy_store=STRATEGIES_WITH_ALWAYS_REPLY)
    await policy.start(port)

    await policy.on_utterance(make_utterance(modality="audio", always_reply=True))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="always_reply",
            prompt_fragment="they asked a direct question",
        ),
    )

    assert len(port.instructions) == 1
    assert "Respond directly." in port.instructions[0]


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


OVERVIEW_CONTENT = {
    "narrative": "the group discussed the deadline",
    "decisions": [
        {
            "decision": "deadline is Fri 24 Oct",
            "decided_by": ["Ada"],
            "rationale": "the course schedule",
            "date": "14 Oct",
            "changed_on": "",
        }
    ],
    "action_points": [
        {"owner": "Grace", "task": "send the forecast link", "status": "open", "raised_on": "14 Oct"}
    ],
    "conclusions": [],
    "knowledge": [],
    "contributions": [{"name": "Ada", "items": ["proposed the deadline"]}],
    "decision_making": [],
    "process_notes": [],
    "mulca": [],
}


async def save_overview(store, conversation_id):
    await store.save_summary(
        conversation_id,
        MemorySummary(kind="overview", day="2026-10-14", content=OVERVIEW_CONTENT),
    )


async def test_provide_summary_always_includes_the_memory_even_once_sent():
    # The memory goes with a session's first reply only (the live session
    # keeps it) -- but a recap is exactly when it's worth sending again.
    strategies = StrategyStore(
        {
            "respond_when_addressed": Strategy(
                id="respond_when_addressed",
                trigger_description="",
                prompt_template="Answer. {prompt_fragment}",
            ),
            "provide_summary": Strategy(
                id="provide_summary",
                trigger_description="",
                prompt_template="Summarize. {prompt_fragment}",
            ),
        }
    )
    episodic = InMemoryEpisodicStore()
    policy, port = make_policy(strategy_store=strategies, episodic_store=episodic)
    await save_overview(episodic, port.conversation_id)
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="hi"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )
    await policy.on_assistant_output("hello", interrupted=False)
    assert "the group discussed the deadline" in port.instructions[0]

    await policy.on_utterance(make_utterance(content="can you summarize?"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="provide_summary", prompt_fragment=""),
    )

    assert "the group discussed the deadline" in port.instructions[1]
    assert "Ada: proposed the deadline" in port.instructions[1]
    assert "[Grace] send the forecast link (raised 14 Oct)" in port.instructions[1]
    assert "14 Oct: deadline is Fri 24 Oct" in port.instructions[1]


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


async def test_the_verbatim_history_is_a_rolling_recent_window():
    policy, port = make_policy(recent_window=3)
    await policy.start(port)

    for i in range(5):
        await policy.on_utterance(make_utterance(content=f"message {i}"))
        await policy.on_intervention_signal(object(), None)

    assert [u.content for u in policy._wm.history] == [
        "message 2",
        "message 3",
        "message 4",
    ]


async def test_participation_is_measured_over_its_own_window():
    policy, port = make_policy(participation_window=2)
    await policy.start(port)

    for name in ("Ada", "Ada", "Ada", "Grace", "Grace"):
        await policy.on_utterance(make_utterance(name=name, speaker_id=hash(name)))
        await policy.on_intervention_signal(object(), None)

    # Only the last two turns, both Grace's.
    assert policy._wm.trigger_variables["participant_message_counts"] == {"Grace": 2}


async def test_a_dm_from_a_recognized_participant_reads_the_group_memory():
    stores: dict[str, tuple] = {}

    def store_factory(conversation_id):
        return stores.setdefault(
            conversation_id,
            (InMemoryEpisodicStore(), InMemorySnapshotStore()),
        )

    group_store, _ = store_factory("instance:team-alpha")
    await save_overview(group_store, "instance:team-alpha")

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

    memory = policy.memory_text()
    assert "the group discussed the deadline" in memory
    assert "Ada: proposed the deadline" in memory
    # Read-only: nothing written to the group.
    assert [s.kind for s in await group_store.load_summaries("instance:team-alpha")] == [
        "overview"
    ]


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


async def test_a_dm_gets_no_summaries_of_its_own_just_a_recent_window():
    summarizer = FakeDaySummarizer()
    episodic = InMemoryEpisodicStore()
    policy, port = make_policy(
        episodic_store=episodic,
        day_summarizer=summarizer,
        port=FakePort(conversation_id="dm:42", is_private=True),
        recent_window=2,
    )
    await policy.start(port)

    for i in range(4):
        await policy.on_utterance(make_utterance(content=f"message {i}"))
        await policy.on_intervention_signal(object(), None)
    await policy.on_context_full()
    await policy.wait_for_memory()

    assert summarizer.calls == []
    assert await episodic.load_summaries("dm:42") == []
    assert len(policy._wm.history) == 2


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


async def test_the_verbatim_tail_is_not_resent_on_a_later_triggered_reply():
    """
    Found live: the recent-messages tail was going out on every single
    triggered reply, not just the first after a (re)connect -- pure
    redundancy once the live session has natively seen those turns itself.
    """

    policy, port = make_policy()
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="the number is 6"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )
    await policy.on_assistant_output("ok", interrupted=False)

    await policy.on_utterance(make_utterance(content="the number is 7"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    assert "the number is 6" in port.instructions[0]
    assert "Recent messages in this conversation" not in port.instructions[1]


async def test_a_groups_memory_is_sent_once_then_again_after_a_context_reset():
    """
    The live session keeps the memory once it has it, so it isn't resent
    with every reply -- until its context is renewed (it was full), which
    starts it over from the memory.
    """

    episodic = InMemoryEpisodicStore()
    policy, port = make_policy(episodic_store=episodic)
    await save_overview(episodic, port.conversation_id)
    await policy.start(port)

    async def reply(text):
        await policy.on_utterance(make_utterance(content=text))
        await policy.on_intervention_signal(
            object(),
            InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
        )
        await policy.on_assistant_output("ok", interrupted=False)

    await reply("first message")
    await reply("second message")
    assert "the group discussed the deadline" in port.instructions[0]
    assert "the group discussed the deadline" not in port.instructions[1]

    await policy.on_context_full()
    await policy.wait_for_memory()
    await reply("third message")

    assert "the group discussed the deadline" in port.resets[-1]
    assert "the group discussed the deadline" in port.instructions[2]
    # Today, verbatim, MULCA's own replies included.
    assert "Ada (text): second message" in port.instructions[2]
    assert "MULCA (text): ok" in port.instructions[2] or "(text): ok" in port.instructions[2]


def make_dm_policy():
    stores: dict[str, tuple] = {}

    def store_factory(conversation_id):
        return stores.setdefault(
            conversation_id,
            (InMemoryEpisodicStore(), InMemorySnapshotStore()),
        )

    return store_factory, FakePort(
        conversation_id="dm:1",
        is_private=True,
        linked_conversation_id="instance:team-alpha",
    )


async def test_a_dms_linked_summary_is_not_resent_within_the_hour():
    """
    Found live: this used to go out on every single triggered DM reply,
    unconditionally -- real, recurring cost for an active back-and-forth
    DM, not a one-time thing the way a group's own summary is. Now gated
    to DM_FULL_CONTEXT_INTERVAL_SECONDS, same as the group case just with
    a real interval instead of "forever" -- see the next test for the
    other side of that gate.
    """

    store_factory, port = make_dm_policy()
    _, group_snapshot_store = store_factory("instance:team-alpha")
    await group_snapshot_store.save(
        "instance:team-alpha",
        MemorySnapshot(history_summary="The team discussed the deadline."),
    )

    policy = CoalaPolicy(strategy_store=STRATEGIES, store_factory=store_factory)
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
    assert "The team discussed the deadline." not in port.instructions[1]


async def test_a_dms_linked_summary_is_resent_after_the_interval_elapses(monkeypatch):
    import muca_server.cognition.policy as policy_module

    store_factory, port = make_dm_policy()
    _, group_snapshot_store = store_factory("instance:team-alpha")
    await group_snapshot_store.save(
        "instance:team-alpha",
        MemorySnapshot(history_summary="The team discussed the deadline."),
    )

    policy = CoalaPolicy(strategy_store=STRATEGIES, store_factory=store_factory)
    await policy.start(port)

    clock = [1_000_000.0]
    monkeypatch.setattr(policy_module.time, "time", lambda: clock[0])

    await policy.on_utterance(make_utterance(content="first message"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )
    await policy.on_assistant_output("ok", interrupted=False)

    clock[0] += policy_module.DM_FULL_CONTEXT_INTERVAL_SECONDS + 1

    await policy.on_utterance(make_utterance(content="second message"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    assert "The team discussed the deadline." in port.instructions[0]
    assert "The team discussed the deadline." in port.instructions[1]


async def test_a_dms_full_context_gate_survives_a_reconnect():
    """
    Found live: CoalaPolicy is rebuilt fresh on every (re)connect
    (session_components_factory.policy_factory), so without persisting
    full_context_sent_at, a DM that merely reconnected -- not actually an
    hour later -- would get the full-context resend again immediately,
    keyed on "this connection" rather than durably on this one
    participant's own conversation_id.
    """

    store_factory, port = make_dm_policy()
    _, group_snapshot_store = store_factory("instance:team-alpha")
    await group_snapshot_store.save(
        "instance:team-alpha",
        MemorySnapshot(history_summary="The team discussed the deadline."),
    )

    first = CoalaPolicy(strategy_store=STRATEGIES, store_factory=store_factory)
    await first.start(port)
    await first.on_utterance(make_utterance(content="first message"))
    await first.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )
    await first.on_assistant_output("ok", interrupted=False)
    await first.close()

    # A fresh CoalaPolicy, same DM conversation_id -- standing in for a
    # reconnect seconds later, not an hour later.
    second_port = FakePort(
        conversation_id=port.conversation_id,
        is_private=True,
        linked_conversation_id="instance:team-alpha",
    )
    second = CoalaPolicy(strategy_store=STRATEGIES, store_factory=store_factory)
    await second.start(second_port)
    await second.on_utterance(make_utterance(content="second message"))
    await second.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    assert "The team discussed the deadline." in port.instructions[0]
    assert "The team discussed the deadline." not in second_port.instructions[0]


async def test_a_groups_snapshot_saves_after_every_turn():
    # The bug this covers: "Mats says X in the group, then immediately DMs
    # about X" failing because nothing had been saved yet.
    snapshot_store = InMemorySnapshotStore()
    policy, port = make_policy(snapshot_store=snapshot_store)
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

    # Nothing logged yet today: the group's saved recent window stands in.
    assert "Mats (text): the number is 6" in port.instructions[0]


async def test_a_dm_prompt_includes_the_linked_groups_day_so_far():
    stores: dict[str, tuple] = {}

    def store_factory(conversation_id):
        return stores.setdefault(
            conversation_id,
            (InMemoryEpisodicStore(), InMemorySnapshotStore()),
        )

    group_store, _ = store_factory("instance:team-alpha")
    await group_store.log(
        EpisodicRecord(
            timestamp=time.time(),
            conversation_id="instance:team-alpha",
            intervention_path="none",
            speaker="Mats",
            content="the number is 6",
        )
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
    await policy.on_utterance(make_utterance(content="what is the number?"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    assert "Mats (text): the number is 6" in port.instructions[0]


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


async def test_observer_catchup_text_carries_the_overview_and_today():
    episodic = InMemoryEpisodicStore()
    policy, port = make_policy(episodic_store=episodic)
    await save_overview(episodic, port.conversation_id)
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="let's also fix the budget"))

    catchup = policy.observer_catchup_text()

    assert "the group discussed the deadline" in catchup
    assert "Ada: proposed the deadline" in catchup
    assert "[Grace] send the forecast link" in catchup
    assert "let's also fix the budget" in catchup


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


class FakeWebSearcher:
    def __init__(self, result: str = "Rome is warm in October.") -> None:
        self.queries: list[str] = []
        self._result = result

    async def search(self, query: str) -> str:
        self.queries.append(query)
        return self._result


async def test_a_text_question_needing_web_search_gets_it():
    searcher = FakeWebSearcher()
    policy, port = make_policy(web_searcher=searcher)
    await policy.start(port)

    await policy.on_utterance(make_utterance(modality="text"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="asked about the weather",
            needs_web_search=True,
            web_search_query="weather in Rome today",
        ),
    )

    assert searcher.queries == ["weather in Rome today"]
    assert "Rome is warm in October." in port.instructions[-1]
    assert "one or two short sentences" not in port.instructions[-1].lower()


async def test_a_voice_question_needing_web_search_gets_an_ack_then_the_findings():
    # Voice: "let me check that" (its own spoken turn) -> search -> the
    # real answer (a second spoken turn) -- not one reply with a silent
    # multi-second gap in it. on_intervention_signal only returns once
    # both stages are sent, because the interim ack's own completion
    # doesn't resolve the turn (see test_the_interim_ack_alone_does_not_
    # resolve_the_turn below for that half in isolation).
    searcher = FakeWebSearcher()
    policy, port = make_policy(web_searcher=searcher, interim_ack_timeout_seconds=0.05)
    await policy.start(port)

    await policy.on_utterance(make_utterance(modality="audio"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="asked about the weather",
            needs_web_search=True,
            web_search_query="weather in Rome today",
        ),
    )

    assert searcher.queries == ["weather in Rome today"]
    assert len(port.instructions) == 2
    assert "Rome is warm in October." not in port.instructions[0]
    assert "Rome is warm in October." in port.instructions[1]
    # Found live: without a hard cap, the ack's own wording ("a word or
    # two") wasn't reliably obeyed -- it rambled into a full, sometimes
    # fabricated, answer before the real search had even run. The real
    # findings reply is deliberately uncapped.
    assert port.instruction_max_tokens[0] == INTERIM_ACK_MAX_TOKENS
    assert port.instruction_max_tokens[1] is None
    # Asked for extra brevity, voice only -- spoken findings are harder to
    # follow by ear than the same text would be to skim.
    assert "one or two short sentences" in port.instructions[1].lower()


async def test_the_interim_ack_alone_does_not_resolve_the_turn():
    # If the ack's own completion arrives (conversation.py calls
    # on_assistant_output for every completed reply, including this
    # interim one), the turn must stay open: the real findings reply for
    # this same turn is still in flight.
    policy, port = make_policy(
        web_searcher=FakeWebSearcher(),
        interim_ack_timeout_seconds=5,
    )
    await policy.start(port)
    await policy.on_utterance(make_utterance(modality="audio"))

    signal_task = asyncio.create_task(
        policy.on_intervention_signal(
            object(),
            InterventionSignal(
                strategy_id="respond_when_addressed",
                prompt_fragment="asked about the weather",
                needs_web_search=True,
                web_search_query="weather in Rome today",
            ),
        )
    )
    # Let on_intervention_signal run up to its await on the ack.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await policy.on_assistant_output("Let me check that.", interrupted=False)

    waiter = asyncio.create_task(policy.wait_for_turn_to_finish())
    await asyncio.sleep(0)
    assert not waiter.done()  # the real findings reply hasn't landed yet

    await asyncio.wait_for(signal_task, timeout=1)
    await policy.on_assistant_output("Rome is warm in October.", interrupted=False)
    await asyncio.wait_for(waiter, timeout=1)


async def test_needs_web_search_without_a_configured_searcher_is_a_no_op():
    policy, port = make_policy()  # no web_searcher given
    await policy.start(port)

    await policy.on_utterance(make_utterance(modality="text"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="asked about the weather",
            needs_web_search=True,
            web_search_query="weather in Rome today",
        ),
    )

    assert len(port.instructions) == 1  # didn't crash, still replied


async def test_needs_web_search_with_an_empty_query_is_a_no_op():
    # Found live: needs_web_search=True arrived with no web_search_query
    # at all often enough to be worth its own guard -- the model set one
    # flag and not the other.
    searcher = FakeWebSearcher()
    policy, port = make_policy(web_searcher=searcher)
    await policy.start(port)

    await policy.on_utterance(make_utterance(modality="text"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="asked about the weather",
            needs_web_search=True,
            web_search_query="",
        ),
    )

    assert searcher.queries == []
    assert len(port.instructions) == 1  # didn't crash, still replied


async def test_flag_offensive_language_splices_an_overlay_onto_a_different_primary_reply():
    policy, port = make_policy(strategy_store=STRATEGIES_WITH_RIDERS)
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="MULCA, you're useless"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="",
            flag_offensive_language=True,
        ),
    )

    assert "Answer them." in port.instructions[0]
    assert "Also briefly note the offense." in port.instructions[0]


async def test_nudge_participation_splices_an_overlay_onto_a_different_primary_reply():
    policy, port = make_policy(strategy_store=STRATEGIES_WITH_RIDERS)
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="MULCA, what's next?"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="",
            nudge_participation=True,
        ),
    )

    assert "Answer them." in port.instructions[0]
    assert "Also briefly invite the quiet one." in port.instructions[0]


async def test_a_rider_flag_is_a_no_op_when_it_matches_the_primary_strategy_itself():
    # The observer is told to select the strategy's own id instead of
    # flagging it as a rider on itself -- guard against a double-splice if
    # it does both anyway.
    policy, port = make_policy(strategy_store=STRATEGIES_WITH_RIDERS)
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="that's an offensive remark"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="pleasant_conversation",
            prompt_fragment="",
            flag_offensive_language=True,
        ),
    )

    assert port.instructions[0].count("the offense") == 1


async def test_a_rider_in_cooldown_is_vetoed_but_the_primary_reply_still_goes_out():
    policy, port = make_policy(strategy_store=STRATEGIES_WITH_RIDERS)
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="first"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="participation_equality_text",
            prompt_fragment="",
        ),
    )
    await policy.on_assistant_output("ok", interrupted=False)

    await policy.on_utterance(make_utterance(content="MULCA, help"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="respond_when_addressed",
            prompt_fragment="",
            nudge_participation=True,
        ),
    )

    assert "Answer them." in port.instructions[1]
    assert "invite the quiet one" not in port.instructions[1]


async def test_a_session_event_is_logged_apart_from_memory():
    episodic = InMemoryEpisodicStore()
    policy, port = make_policy(episodic_store=episodic)
    await policy.start(port)

    await policy.on_session_event(
        "wake_ended",
        "silence",
        data={"reason": "silence", "duration_s": 42},
    )

    [event] = episodic.session_events
    assert event["event"] == "wake_ended"
    assert event["detail"] == "silence"
    assert event["data"] == {"reason": "silence", "duration_s": 42}
    assert episodic._records.get(port.conversation_id, []) == []


async def test_a_group_session_after_a_restart_gets_the_summary_again():
    # Found live: the group's snapshot still held when a *previous*
    # connection had been sent the summary, so a restarted session --
    # which has never seen it -- was never sent it at all.
    snapshot_store = InMemorySnapshotStore()
    port = FakePort(conversation_id="instance:team-alpha")
    await snapshot_store.save(
        port.conversation_id,
        MemorySnapshot(
            history_summary="The team agreed on Friday's deadline.",
            full_context_sent_at=1000.0,
        ),
    )
    policy, _ = make_policy(snapshot_store=snapshot_store, port=port)
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="what did we agree?"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="respond_when_addressed", prompt_fragment=""),
    )

    assert "The team agreed on Friday's deadline." in port.instructions[0]


# ----------------------------------------------------------------------
# Long-term memory by day (docs/MEMORY_DESIGN.md)
# ----------------------------------------------------------------------


class FakeDaySummarizer:
    def __init__(self, *, fail=False):
        self.calls: list[tuple] = []
        self.fail = fail

    async def summarize_day(self, *, day, log, final):
        self.calls.append(("day", day, final, log))
        if self.fail:
            raise RuntimeError("summary failed")
        return {"narrative": f"summary of {day}", "decisions": [], "action_points": []}

    async def build_overview(self, *, days):
        self.calls.append(("overview", [day for day, _ in days]))
        if self.fail:
            raise RuntimeError("summary failed")
        return {"narrative": "overview of " + ", ".join(day for day, _ in days)}


def logged(conversation_id, when, content, *, speaker="Ada", reply=None):
    return EpisodicRecord(
        timestamp=when,
        conversation_id=conversation_id,
        intervention_path="strategy" if reply else "none",
        speaker=speaker,
        content=content,
        assistant_output=reply,
    )


DAY = 24 * 3600


async def test_past_days_are_backfilled_and_the_overview_built_from_them():
    episodic = InMemoryEpisodicStore()
    summarizer = FakeDaySummarizer()
    policy, port = make_policy(episodic_store=episodic, day_summarizer=summarizer)
    now = time.time()
    for when, text in ((now - 3 * DAY, "kickoff"), (now - DAY, "prototype"), (now, "today")):
        await episodic.log(logged(port.conversation_id, when, text, reply="noted"))

    await policy.start(port)
    await policy.wait_for_memory()

    days = [c for c in summarizer.calls if c[0] == "day"]
    assert len(days) == 2  # not today
    assert all(final for _, _, final, _ in days)
    assert "Ada (text): kickoff" in days[0][3]
    assert "MULCA (text): noted" in days[0][3]  # MULCA's part included
    overview = [c for c in summarizer.calls if c[0] == "overview"]
    assert overview == [("overview", [days[0][1], days[1][1]])]
    assert "overview of" in policy.memory_text()
    assert "Ada (text): today" in policy.memory_text()  # today, verbatim
    assert port.resets  # observers renewed with the new memory

    # A restart doesn't redo any of it.
    again = FakeDaySummarizer()
    policy2, port2 = make_policy(episodic_store=episodic, day_summarizer=again, port=port)
    await policy2.start(port2)
    await policy2.wait_for_memory()
    assert again.calls == []


async def test_a_failed_summary_keeps_whats_there_and_is_retried_next_time():
    episodic = InMemoryEpisodicStore()
    policy, port = make_policy(episodic_store=episodic, day_summarizer=FakeDaySummarizer(fail=True))
    await episodic.log(logged(port.conversation_id, time.time() - DAY, "yesterday"))

    await policy.start(port)
    await policy.wait_for_memory()

    assert await episodic.load_summaries(port.conversation_id) == []
    assert port.resets == []

    retry = FakeDaySummarizer()
    policy2, port2 = make_policy(episodic_store=episodic, day_summarizer=retry, port=port)
    await policy2.start(port2)
    await policy2.wait_for_memory()
    assert [c[0] for c in retry.calls] == ["day", "overview"]


async def test_a_new_day_starts_today_over_and_rolls_yesterday_into_memory():
    episodic = InMemoryEpisodicStore()
    summarizer = FakeDaySummarizer()
    policy, port = make_policy(episodic_store=episodic, day_summarizer=summarizer)
    await policy.start(port)
    await policy.wait_for_memory()
    await episodic.log(logged(port.conversation_id, time.time() - DAY, "late last night"))
    policy._today_day = "2000-01-01"  # the session has been open since "yesterday"
    policy._today_lines = ["Ada (text): late last night"]

    await policy.on_utterance(make_utterance(content="good morning"))
    await policy.wait_for_memory()

    assert policy._today_lines[-1] == "Ada (text): good morning"
    assert "late last night" not in "\n".join(policy._today_lines)
    assert [c[0] for c in summarizer.calls] == ["day", "overview"]


async def test_a_full_context_summarizes_today_when_it_no_longer_fits():
    episodic = InMemoryEpisodicStore()
    summarizer = FakeDaySummarizer()
    policy, port = make_policy(
        episodic_store=episodic,
        day_summarizer=summarizer,
        today_verbatim_tokens=10,
    )
    await policy.start(port)
    await policy.wait_for_memory()
    for i in range(6):
        await policy.on_utterance(make_utterance(content=f"a fairly long message number {i}"))
        await policy.on_intervention_signal(object(), None)

    await policy.on_context_full()
    await policy.wait_for_memory()

    assert summarizer.calls[-1][0] == "day" and summarizer.calls[-1][2] is False
    memory = policy.memory_text()
    assert "so far, summarized" in memory
    assert "Most recent messages, verbatim" in memory
    assert "a fairly long message number 5" in memory
    assert port.resets and "so far, summarized" in port.resets[-1]


async def test_a_fresh_session_gets_all_of_today_verbatim():
    episodic = InMemoryEpisodicStore()
    port = FakePort()
    now = time.time()
    for i in range(3):
        await episodic.log(logged(port.conversation_id, now - 60 + i, f"earlier {i}"))
    policy, _ = make_policy(episodic_store=episodic, port=port)

    await policy.start(port)

    memory = policy.memory_text()
    assert "so far, verbatim" in memory
    assert all(f"Ada (text): earlier {i}" in memory for i in range(3))


async def test_the_old_running_summary_shows_until_the_first_overview():
    episodic = InMemoryEpisodicStore()
    snapshot_store = InMemorySnapshotStore()
    policy, port = make_policy(episodic_store=episodic, snapshot_store=snapshot_store)
    await snapshot_store.save(
        port.conversation_id,
        MemorySnapshot(history_summary="the old running summary"),
    )
    await policy.start(port)
    assert "the old running summary" in policy.memory_text()

    await save_overview(episodic, port.conversation_id)
    policy2, port2 = make_policy(episodic_store=episodic, snapshot_store=snapshot_store, port=port)
    await policy2.start(port2)
    assert "the old running summary" not in policy2.memory_text()
    # Still saved untouched, for an older server version.
    await policy2.close()
    assert (await snapshot_store.load(port.conversation_id)).history_summary == "the old running summary"


async def test_provide_summary_retrieves_the_day_summaries():
    strategies = StrategyStore(
        {
            "provide_summary": Strategy(
                id="provide_summary",
                trigger_description="",
                prompt_template="Summarize. {prompt_fragment}",
            ),
        }
    )
    episodic = InMemoryEpisodicStore()
    policy, port = make_policy(strategy_store=strategies, episodic_store=episodic)
    await episodic.save_summary(
        port.conversation_id,
        MemorySummary(kind="day", day="2026-10-13", content={"narrative": "Monday's work"}),
    )
    await policy.start(port)

    await policy.on_utterance(make_utterance(content="what did we do before today?"))
    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="provide_summary", prompt_fragment=""),
    )

    assert "Day summary, Tuesday 13 October 2026" in port.instructions[0]
    assert "Monday's work" in port.instructions[0]


async def summary_policy_with_days():
    """A policy whose memory has an overview and two earlier days."""

    from datetime import date, timedelta

    from muca_server.cognition.memory.day_memory import day_of

    today = date.fromisoformat(day_of(time.time()))
    yesterday = (today - timedelta(days=1)).isoformat()
    earlier = (today - timedelta(days=3)).isoformat()
    strategies = StrategyStore(
        {
            "provide_summary": Strategy(
                id="provide_summary",
                trigger_description="",
                prompt_template="Summarize. {prompt_fragment}",
            ),
        }
    )
    episodic = InMemoryEpisodicStore()
    policy, port = make_policy(strategy_store=strategies, episodic_store=episodic)
    for day, text in ((yesterday, "yesterday's work"), (earlier, "older work")):
        await episodic.save_summary(
            port.conversation_id,
            MemorySummary(kind="day", day=day, content={"narrative": text}),
        )
    await save_overview(episodic, port.conversation_id)
    await policy.start(port)
    await policy.on_utterance(make_utterance(content="today's question"))
    return policy, port


async def test_a_summary_of_one_period_gets_only_that_period():
    policy, port = await summary_policy_with_days()

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="provide_summary", prompt_fragment="", history_period="yesterday"
        ),
    )

    sent = port.instructions[0]
    assert "yesterday's work" in sent
    assert "The question is about" in sent
    assert "older work" not in sent
    assert "the group discussed the deadline" not in sent  # the overview
    assert "today's question" not in sent


async def test_a_summary_of_today_gets_today_and_no_earlier_days():
    policy, port = await summary_policy_with_days()

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(
            strategy_id="provide_summary", prompt_fragment="", history_period="today"
        ),
    )

    sent = port.instructions[0]
    assert "today's question" in sent
    assert "yesterday's work" not in sent
    assert "older work" not in sent
    assert "the group discussed the deadline" not in sent


async def test_a_summary_of_everything_still_gets_everything():
    policy, port = await summary_policy_with_days()

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="provide_summary", prompt_fragment=""),
    )

    sent = port.instructions[0]
    for text in ("yesterday's work", "older work", "the group discussed the deadline", "today's question"):
        assert text in sent


async def test_the_brief_leaves_the_content_to_mulca():
    # Found live: MULCA followed briefs that were wrong about what it could
    # see. The heading above every brief gives it the final say.
    policy, port = await summary_policy_with_days()

    await policy.on_intervention_signal(
        object(),
        InterventionSignal(strategy_id="provide_summary", prompt_fragment="only the header"),
    )

    sent = port.instructions[0]
    assert "go by what you see" in sent
    assert "attached files, documents, web search results" in sent
