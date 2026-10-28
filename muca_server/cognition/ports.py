"""
The seam between a conversation and whatever decides what it should say.

Deliberately imports nothing else from the project: the agents layer depends
on these names, and the rest of `cognition` depends on the agents layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Utterance:
    """One complete thing somebody said."""

    speaker_id: int
    speaker_name: str
    content: str
    timestamp_ms: int
    modality: Literal["text", "audio"] = "text"
    # "toggle"/"transcribe" modes only, voice only: set while a listening
    # window is open (see conversation.py's arm_listening/_handle_wake_
    # phrase) -- the user explicitly opened it before speaking, so there is
    # no "was this addressed to me" question the way there is in ambient
    # mode. CoalaPolicy's on_intervention_signal uses this to force a
    # reply, rather than let "do nothing" be a real outcome -- see the
    # always_reply strategy.
    always_reply: bool = False
    # "transcribe" mode only, voice only: a deterministic (not model-judged)
    # phrase match found in conversation.py -- plain string matching, not
    # left to an observer's semantic judgement, which is exactly the kind
    # of unreliable call this mode exists to avoid making continuously.
    # Fires a fixed acknowledgement (CoalaPolicy.on_utterance), not a normal
    # reply -- the content itself is a control phrase, not a question.
    is_wake_phrase: bool = False
    # Only meaningful when is_wake_phrase is True: whether real content
    # rode along with the trigger word in the same breath ("hey mulca,
    # could you define the UNG diagram" -- not just "assistant?" alone).
    wake_phrase_has_question: bool = False
    # Only meaningful when wake_phrase_has_question is True. "transcribe"/
    # "toggle": the question gets judged normally (consult_observers), same
    # as a follow-up later in the window -- found live, the alternative
    # (answer immediately, no judgement at all) meant needs_web_search
    # could never be set for a question asked in the very breath that
    # opens the window, e.g. "MULCA, could you look up the latest X?"
    # right away answered "I can't look things up" with no search
    # attempted. "wake": left False on purpose -- an engaged session's own
    # opening question is still answered immediately with no judgement
    # (confirmed by test_the_wake_phrase_engages_without_the_judge_and_
    # announces_once), consistent with "wake" judging only later, engaged
    # turns, never the one that starts the session.
    judge_wake_question: bool = False
    # A spoken turn in "wake_v2": its observers get the turn facts instead of
    # the trigger variables.
    wake_v2: bool = False
    # "wake_v2": a bare wake phrase said again in an open session -- it gets
    # a one-word ack.
    wake_restart: bool = False

    def label(self) -> str:
        return self.speaker_name or f"User {self.speaker_id}"


@dataclass(frozen=True, slots=True)
class InterventionSignal:
    """An observer's judgement that the assistant should speak now."""

    strategy_id: str
    prompt_fragment: str
    needs_history: bool = False
    # Which part of the history a history question or summary is about:
    # one of day_memory.HISTORY_PERIODS, or a date as YYYY-MM-DD. "all" is
    # the whole history, as before this existed.
    history_period: str = "all"
    needs_web_search: bool = False
    # The observer's own query, not re-derived heuristically from the raw
    # message the way "remember"'s extract_query is -- a web search query
    # ("current weather in Rome") looks nothing like the triggering
    # question's own phrasing ("what should I pack"), so the model that
    # already read the message writes it directly instead. Empty unless
    # needs_web_search is true.
    web_search_query: str = ""

    # Independent riders on top of strategy_id: a short overlay from a
    # *different* strategy, spliced onto this turn's directive rather than
    # replacing it -- e.g. answering a direct question (strategy_id=
    # respond_when_addressed) that also contained offensive language
    # (flag_offensive_language=True). Only meaningful when strategy_id
    # names a different strategy; if nothing else fires, the observer
    # selects the rider's own strategy as strategy_id instead, the
    # existing standalone path. See CoalaPolicy.on_intervention_signal.
    flag_offensive_language: bool = False
    nudge_participation: bool = False
    # "wake_v2": the observer chose silence (no_intervention, strategy_id
    # empty) and said why -- logged with the turn.
    silence_reason: str = ""


class ConversationPort(Protocol):
    """Interface which defines what cognition can ask of its conversation."""

    @property
    def conversation_id(self) -> str:
        ...

    @property
    def is_private(self) -> bool:
        """A private conversation is never written to long-term memory."""
        ...

    @property
    def linked_conversation_id(self) -> str | None:
        """
        For a DM: the group conversation_id it's allowed one-way read access
        to (its sender is a recognized participant of that instance), or
        None if unrecognized. Meaningless when is_private is False.
        """
        ...

    def participants(self) -> dict[int, str]:
        ...

    def voice_participants(self) -> dict[int, str]:
        """
        Who is actually in the voice call, separately from participants()
        (text channel). Distinct rosters: someone present in text who never
        joins voice, or vice versa, is only ever counted on the side they're
        actually present on.
        """
        ...

    async def instruct_main_agent(
        self,
        directive: str,
        *,
        max_tokens: int | None = None,
    ) -> None:
        """
        Tell the main agent to respond, with guidance on why and roughly
        what. max_tokens hard-caps this one response -- for a directive
        whose own wording ("a word or two") the model doesn't reliably
        keep to on its own (found live: a voice web search's interim
        "let me check that" rambled into a full, sometimes fabricated,
        answer instead).
        """
        ...

    async def tell_observers(self, text: str) -> None:
        ...

    async def reset_agent_contexts(self, observer_memory: str) -> None:
        """
        Clear every agent's live session between turns (long-term memory
        replaces what it held): the observers are re-sent observer_memory
        straight away; the main agent gets its memory with its next reply.
        """
        ...

    async def no_reply(self) -> None:
        """
        This turn's judgment concluded nothing fires -- no reply is coming
        for it. Lets a client stop showing "a reply is coming" UI (e.g.
        Discord's typing indicator) immediately rather than waiting out its
        own timeout for a reply that was never going to arrive.
        """
        ...


class DaySummarizer(Protocol):
    """
    Interface which defines how the log becomes long-term memory
    (docs/MEMORY_DESIGN.md): one summary per day, and an overview built from
    the day summaries -- never from a previous summary.

    Kept separate from CognitionPolicy because it is the one piece of the
    pipeline that has to call a model: an implementation lives beside a
    provider agent, not in cognition itself.
    """

    async def summarize_day(
        self,
        *,
        day: str,
        log: str,
        final: bool,
    ) -> dict:
        """
        One day's log as a summary (the format in docs/MEMORY_DESIGN.md).
        final: the day is over (its lasting summary) rather than "today so
        far", which is replaced later anyway -- an implementation may use a
        stronger model for the former.
        """
        ...

    async def build_overview(self, *, days: list[tuple[str, dict]]) -> dict:
        """The project so far, from every day summary (day, summary), oldest first."""
        ...


class WebSearcher(Protocol):
    """
    Interface which defines looking something up outside this conversation.

    Kept separate from CognitionPolicy for the same reason as Summarizer:
    it is the one piece of this pipeline that has to call out to a real
    search provider, so an implementation lives beside a provider agent,
    not in cognition itself.
    """

    async def search(self, query: str) -> str:
        """A short, directly usable summary of what was found, with
        sources -- not raw results for the caller to parse further."""
        ...


class CognitionPolicy(Protocol):
    """
    Interface which defines whether and what the assistant says.

    A conversation drives turn taking and owns the agents; this decides what
    to do about any of it.
    """

    async def start(self, port: ConversationPort) -> None:
        ...

    async def close(self) -> None:
        ...

    async def on_utterance(self, utterance: Utterance) -> bool:
        """
        Handle somebody speaking.

        Returns whether the observers should now be asked to judge whether to
        intervene. Returning False means this policy already decided.
        """
        ...

    async def on_intervention_signal(
        self,
        agent_guid: UUID,
        signal: InterventionSignal | None,
    ) -> None:
        """`None` means the observer judged that nothing should be said."""
        ...

    async def on_assistant_output(
        self,
        text: str,
        *,
        interrupted: bool,
    ) -> None:
        ...

    async def wait_for_turn_to_finish(self) -> None:
        """
        Blocks until the turn started by the most recent on_utterance() call
        has fully resolved (decided, and if it triggered a reply, until that
        reply's own text is known). Conversation awaits this before letting
        the next utterance through, so a decision can never be attributed to
        the wrong utterance because a second one arrived first.
        """
        ...

    async def on_untranscribed_turn(
        self,
        speaker_id: int,
        speaker_name: str,
        timestamp_ms: int,
    ) -> None:
        """
        A voice turn ended but the provider never produced a transcript for
        it (noise, provider failure). Distinct from a normal on_utterance
        call: there's no content to judge or add to conversational memory,
        only the fact that something happened -- logged for audit
        completeness, not something to act on.
        """
        ...

    def last_turn_strategy(self) -> str | None:
        """
        The strategy the latest turn resolved with: what the intervention
        agent chose, or None when nothing fired (or the reply was forced).
        """
        ...

    async def on_session_event(
        self,
        event: str,
        detail: str = "",
        *,
        speaker: str = "",
        data: dict | None = None,
    ) -> None:
        """
        Something about the session itself happened (a voice call started,
        a wake session ended and why). Logged for analysis only: not a
        turn, nothing to judge or remember.
        """
        ...

    async def on_context_full(self) -> None:
        """
        An agent's live session hit its token limit: renew its memory (see
        docs/MEMORY_DESIGN.md) instead of silently dropping the oldest half.
        """
        ...

    def observer_catchup_text(self) -> str:
        """
        What a freshly-connected observer should be told once, to catch up
        on history already in WM it was never live for -- after a restart
        (WM restored from a snapshot before any observer existed), or any
        time a new observer connects mid-conversation. Empty if there's
        nothing to catch up on. Never the compressed summary -- an observer
        only ever reads raw history/trigger variables, never
        history_summary, so catching it up means the same raw tail, not a
        different kind of context it wouldn't otherwise be given.
        """
        ...
