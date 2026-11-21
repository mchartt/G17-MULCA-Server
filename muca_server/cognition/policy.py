"""
The cognition pipeline: when to speak, what to say, and what to remember.

Ported from MULCA's Orchestrator, split across the two hooks a conversation
calls: on_utterance runs while a message arrives, on_intervention_signal runs
once an observer has judged whether to act.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import date, datetime
from collections.abc import Callable
from uuid import UUID

from .forced_intervention import check_forced_intervention
from .memory.in_memory import InMemoryEpisodicStore, InMemorySnapshotStore
from .memory.ports import Embedder, EpisodicStore, SnapshotStore
from .memory.day_memory import (
    DAY_TZ,
    day_bounds,
    day_heading,
    day_log_lines,
    day_of,
    estimate_tokens,
    period_label,
    render_summary,
    resolve_period,
)
from .memory.records import (
    EpisodicRecord,
    MemorySnapshot,
    MemorySummary,
    StoredUtterance,
)
from .ports import (
    ConversationPort,
    DaySummarizer,
    InterventionSignal,
    Utterance,
    WebSearcher,
)
from .query_extraction import extract_query
from .strategies import StrategyStore
from .trigger_variables import compute_trigger_variables
from .working_memory import WorkingMemory

logger = logging.getLogger(__name__)

RETRIEVAL_LIMIT = 50
# Long-term memory (docs/MEMORY_DESIGN.md) replaced the management cycle,
# which also bounded these two: the last messages kept verbatim, and the
# ones participation is measured over, per modality.
RECENT_WINDOW = 150
PARTICIPATION_WINDOW = 50
# Today goes to the agents verbatim up to this many tokens; past it, as
# "today so far" plus the recent window. Leaves room under the agents'
# ~24k-token sessions for the overview, the persona and the live turns.
TODAY_VERBATIM_TOKENS = 12_000
TURN_TIMEOUT_SECONDS = 30.0

# How long a voice web search's interim "let me check that" is given to
# actually finish playing before moving on regardless -- a safety net, not
# a real limit: a short acknowledgement should finish in well under this,
# and if it somehow doesn't, proceeding anyway (rather than hanging) is
# safer than leaving the turn stuck.
INTERIM_ACK_TIMEOUT_SECONDS = 10.0

# Hard cap on the interim ack's own response -- found live, the directive's
# wording alone ("a word or two, nothing else") wasn't reliably obeyed: it
# sometimes rambled into a full, fabricated answer before any search had
# even run (wrong numbers that then contradicted the real, searched answer
# moments later). Generous enough for a short natural phrase in audio
# (which costs far more tokens per word than text), tight enough to cut off
# well before a second, repeated acknowledgement or a premature answer.
INTERIM_ACK_MAX_TOKENS = 60

# "remember" retrieves a few specific, relevant facts, not a wide recent
# window like provide_summary's RETRIEVAL_LIMIT -- a handful of the most
# similar turns is more useful here than fifty chronological ones.
SIMILARITY_RETRIEVAL_LIMIT = 8

# How often a DM's mirrored read of its linked group (summary + raw tail,
# both foreign content the DM's own live session was never part of) gets
# re-sent. Found live: this used to go out on every single triggered DM
# reply, unconditionally -- real, recurring cost for an active DM, not a
# one-time thing the way a group's own summary is. An hour is a judgment
# call, not measured: frequent enough that the linked group's state can't
# have drifted far, infrequent enough that an active back-and-forth DM
# isn't resending it every reply.
DM_FULL_CONTEXT_INTERVAL_SECONDS = 3600.0


class CoalaPolicy:
    """
    One conversation's cognition: working memory, strategies, and memory.

    A conversation drives turn taking and holds the agents; this decides
    whether the assistant should speak and renders what it should be told.
    """

    def __init__(
        self,
        *,
        strategy_store: StrategyStore,
        assistant_id: int = 0,
        assistant_name: str = "",
        episodic_store: EpisodicStore | None = None,
        snapshot_store: SnapshotStore | None = None,
        store_factory: Callable[
            [str], tuple[EpisodicStore, SnapshotStore]
        ]
        | None = None,
        day_summarizer: DaySummarizer | None = None,
        embedder: Embedder | None = None,
        web_searcher: WebSearcher | None = None,
        recent_window: int = RECENT_WINDOW,
        participation_window: int = PARTICIPATION_WINDOW,
        today_verbatim_tokens: int = TODAY_VERBATIM_TOKENS,
        retrieval_limit: int = RETRIEVAL_LIMIT,
        similarity_retrieval_limit: int = SIMILARITY_RETRIEVAL_LIMIT,
        turn_timeout_seconds: float = TURN_TIMEOUT_SECONDS,
        interim_ack_timeout_seconds: float = INTERIM_ACK_TIMEOUT_SECONDS,
    ) -> None:
        self._strategies = strategy_store
        self._assistant_id = assistant_id
        self._assistant_name = assistant_name
        self._episodic_store = episodic_store
        self._snapshot_store = snapshot_store
        # None is a legitimate, supported configuration (no search
        # provider wired in at all), not just an unset default -- a signal
        # asking for it then is treated the same as the provider failing,
        # not an error.
        self._web_searcher = web_searcher
        # Deferred store selection: which DB backs this conversation can
        # depend on its conversation_id (e.g. one SQLite file per Discord
        # channel), which isn't known until start() -- policy_factory builds
        # this object before the connect handshake ever runs. Takes
        # precedence over episodic_store/snapshot_store above when given.
        self._store_factory = store_factory
        self._day_summarizer = day_summarizer
        self._embedder = embedder
        self._recent_window = recent_window
        self._participation_window = participation_window
        self._today_verbatim_tokens = today_verbatim_tokens
        self._retrieval_limit = retrieval_limit
        self._similarity_retrieval_limit = similarity_retrieval_limit
        self._turn_timeout_seconds = turn_timeout_seconds
        self._interim_ack_timeout_seconds = interim_ack_timeout_seconds

        self._port: ConversationPort | None = None
        self._wm: WorkingMemory | None = None

        # Turn serialization ("queued, not interleaved"), restored here
        # after being silently lost when the intervention agent moved to
        # an event-driven Realtime design -- its decision now arrives via
        # a callback with no reference to which
        # utterance it was judging, so a second utterance arriving before
        # the first's decision resolved would corrupt attribution (found via
        # a real duplicate/missing episodic-log entry, not hypothetically).
        # Set whenever no turn is in progress; Conversation awaits it via
        # wait_for_turn_to_finish() before processing the next utterance.
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        # The utterance the in-progress turn's eventual decision is about --
        # captured once, not re-derived from wm.history[-1] later, which is
        # exactly what could have moved on by the time a decision arrives.
        self._pending_utterance: Utterance | None = None
        # Deferred until on_assistant_output supplies the actual reply text,
        # so the log captures what MULCA said, not just what triggered it --
        # nothing ever wrote this before, at all.
        self._pending_episodic_record: EpisodicRecord | None = None

        # Set only for the span between a voice web-search's interim
        # acknowledgment ("let me check that") and the real follow-up
        # reply it stands in for -- on_assistant_output must not resolve
        # the turn (or log an episodic record) for the interim one, only
        # for the real one that follows; otherwise the next utterance
        # could be let through, and judged, while the search and the real
        # reply are still in flight for this one.
        self._interim_ack_done: asyncio.Event | None = None

        # A DM only: when its main agent was last sent the linked group's
        # memory -- a different conversation, which moves on without it, so
        # re-sent once DM_FULL_CONTEXT_INTERVAL_SECONDS have passed.
        # Persisted (see _save_snapshot) so a reconnect doesn't resend it.
        self._full_context_sent_at: float | None = None

        # Long-term memory (docs/MEMORY_DESIGN.md), as last loaded: for a
        # group its own, for a DM its linked group's (read-only).
        self._overview: MemorySummary | None = None
        self._days: list[MemorySummary] = []
        self._today: MemorySummary | None = None
        # Today's log, verbatim, one line per turn -- loaded at start, then
        # appended to live. A group's only; a DM's own turns are in its
        # live session already.
        self._today_day: str | None = None
        self._today_lines: list[str] = []
        # Whether the main agent's live session has the memory above: sent
        # with its first reply, and again after any context reset.
        self._memory_sent = False
        # Rollover/backfill and "today so far" run in the background: a
        # summary takes seconds to a minute, and never holds up a turn.
        self._memory_task: asyncio.Task[None] | None = None

        # Gates current_time_block -- re-announced only when the calendar
        # date actually changes (by the clock of each turn's own
        # timestamp_ms), not every single turn. Not persisted across a
        # reconnect: a reconnect is a brand new main-agent session with no
        # memory of a date announced in a previous one.
        self._last_date_announced: date | None = None
        # The strategy the latest turn resolved with (None: nothing fired,
        # or forced). Read-only for the conversation's wake session.
        self._last_turn_strategy: str | None = None
        # Group Coordinator (cognition/coordination/proactive.py): set only
        # when the coordinator is on, by its wiring. None = exactly as before.
        self._proactive = None

    def set_proactive_bridge(self, bridge) -> None:
        """Group Coordinator: its strategies answer with a card, not a reply."""
        self._proactive = bridge

    async def _project_note(self, wm: WorkingMemory) -> str:
        """
        Group Coordinator only: the saved plan and decisions, so the observer
        can judge a dependency or a reopened decision it cannot see in the
        chat (cognition/coordination/project_memory.py). "" when the
        coordinator is off, when nothing changed, or on any error: the turn
        update is then exactly the lab's.
        """
        note_for = getattr(self._proactive, "observer_note", None)
        if note_for is None:
            return ""
        try:
            note = await note_for(wm.total_message_count)
        except Exception:
            logger.exception("%s project memory for the observer failed", wm.conversation_id)
            return ""
        return f"\n{note}" if note else ""

    async def start(self, port: ConversationPort) -> None:
        self._port = port

        if self._store_factory is not None:
            if getattr(port, "is_ephemeral", False):
                # No conversation id from the client: nothing to continue
                # later, so nothing is written to disk for it either. Still
                # gets an embedder, so "remember" works the same as it
                # would for a persisted conversation, just not across a
                # restart.
                self._episodic_store = InMemoryEpisodicStore(
                    embedder=self._embedder,
                )
                self._snapshot_store = InMemorySnapshotStore()
            else:
                self._episodic_store, self._snapshot_store = (
                    self._store_factory(port.conversation_id)
                )

        wm = WorkingMemory(
            conversation_id=port.conversation_id,
            assistant_id=self._assistant_id,
        )

        if self._snapshot_store is not None:
            snapshot = await self._snapshot_store.load(
                port.conversation_id,
            )
            # A DM's conversation_id is one specific participant's, never
            # shared -- full_context_sent_at carries no conversation
            # content at all (just a timestamp), so restoring only this
            # one field is safe even though DMs otherwise aren't written
            # to long-term memory. Without this, the "once per hour" gate
            # would silently reset on every reconnect, since a fresh
            # CoalaPolicy is built each time (session_components_factory).
            # DMs only. Found live: restored for a group too, a fresh
            # session after any restart counted the summary as already
            # sent -- by a previous connection -- and never got it.
            if snapshot is not None and port.is_private:
                self._full_context_sent_at = snapshot.full_context_sent_at
            if snapshot is not None and not port.is_private:
                wm.history_summary = snapshot.history_summary
                wm.participant_roles_summary = (
                    snapshot.participant_roles_summary
                )
                wm.action_points = snapshot.action_points
                wm.total_message_count = snapshot.total_message_count
                wm.history = [
                    stored.restore()
                    for stored in snapshot.history_tail
                ][-self._recent_window:]
                wm.recent_actions = dict(snapshot.recent_actions)
                wm.recent_action_at = dict(snapshot.recent_action_at)

        self._wm = wm

        if port.is_private:
            await self._apply_linked_group_context(wm, port)
        elif self._episodic_store is not None:
            self._today_day = day_of(time.time())
            await self._load_memory()
            self._start_memory_task(self._roll_over())

    async def close(self) -> None:
        if self._memory_task is not None:
            self._memory_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._memory_task
        # A turn whose reply never arrived is still worth its log entry.
        await self._flush_pending_episodic_record()
        await self._save_snapshot()

    async def _save_snapshot(self) -> None:
        """
        Called after every turn, not just close() -- a clean shutdown is
        not the only way a process's memory can end.
        """

        if (
            self._wm is None
            or self._snapshot_store is None
            or self._port is None
        ):
            return

        if self._port.is_private:
            # DMs are never written to long-term memory otherwise (no
            # history/summary persisted) -- full_context_sent_at is the
            # one exception, since it carries no conversation content at
            # all, just a timestamp, and keeps the "once per hour" gate
            # keyed on this one participant's own conversation_id across
            # a reconnect instead of resetting every time.
            await self._snapshot_store.save(
                self._port.conversation_id,
                MemorySnapshot(full_context_sent_at=self._full_context_sent_at),
            )
            return

        await self._snapshot_store.save(
            self._port.conversation_id,
            MemorySnapshot(
                history_summary=self._wm.history_summary,
                participant_roles_summary=(
                    self._wm.participant_roles_summary
                ),
                action_points=self._wm.action_points,
                history_tail=[
                    StoredUtterance.of(utterance)
                    for utterance in self._wm.history
                ],
                total_message_count=self._wm.total_message_count,
                updated_at=time.time(),
                recent_actions=dict(self._wm.recent_actions),
                recent_action_at=dict(self._wm.recent_action_at),
                full_context_sent_at=self._full_context_sent_at,
            ),
        )

    def last_turn_strategy(self) -> str | None:
        return self._last_turn_strategy

    async def on_utterance(self, utterance: Utterance) -> bool:
        """
        Somebody said something, in text or (once transcribed) in speech.

        Conversation calls this once per whole turn, one turn at a time, and
        awaits wait_for_turn_to_finish() before handing over the next.
        """

        wm = self._wm
        port = self._port
        assert wm is not None and port is not None
        self._last_turn_strategy = None

        wm.append_history(utterance)
        wm.trim_history(tail_size=self._recent_window)
        wm.increment_message_count()

        if port.is_private:
            await self._apply_linked_group_context(wm, port)
        else:
            day = day_of(time.time())
            if day != self._today_day:
                # The first activity of a new day: yesterday gets its day
                # summary, the overview is rebuilt (in the background).
                self._today_day = day
                self._today_lines = []
                self._today = None
                self._start_memory_task(self._roll_over())
            self._today_lines.append(self._log_line(utterance.label(), utterance.modality, utterance.content))

        # Text and voice have entirely separate rosters and separate turn
        # counts -- someone present in the voice call who never types has
        # no text participation to measure, and vice versa -- so each
        # modality's gini_index/participant_message_counts is computed from
        # only that modality's own history and roster, never the other's.
        # Voice's variables are the same names, "voice_"-prefixed, merged
        # into one dict so every strategy's catalog-declared trigger
        # variables (whichever modality they're for) are present on the
        # one wm.trigger_variables every strategy shares.
        participants = port.participants()
        voice_participants = port.voice_participants()
        window = self._participation_window
        text_vars = compute_trigger_variables(
            [u for u in wm.history if u.modality == "text"][-window:],
            assistant_id=self._assistant_id,
            participants=participants,
        )
        voice_vars = compute_trigger_variables(
            [u for u in wm.history if u.modality == "audio"][-window:],
            assistant_id=self._assistant_id,
            participants=voice_participants,
        )
        wm.set_trigger_variables(
            {
                **text_vars,
                **{f"voice_{key}": value for key, value in voice_vars.items()},
            }
        )
        logger.info(
            "%s participants=%s gini_index=%s participant_message_counts=%s "
            "voice_participants=%s voice_gini_index=%s "
            "voice_participant_message_counts=%s",
            wm.conversation_id,
            participants,
            wm.trigger_variables["gini_index"],
            wm.trigger_variables["participant_message_counts"],
            voice_participants,
            wm.trigger_variables["voice_gini_index"],
            wm.trigger_variables["voice_participant_message_counts"],
        )

        # The observer only ever sees raw utterance text otherwise (routed
        # straight to its session by Conversation) -- a numeric-threshold
        # strategy like participation_equality_text has no way to judge
        # against a threshold it is never actually told.
        cooling_down = [
            strategy.id
            for strategy in self._strategies.all()
            if wm.strategy_in_cooldown(strategy)
        ]
        cooldown_note = (
            f" Currently in cooldown, do not select: {cooling_down}."
            if cooling_down
            else ""
        )
        # This is only a steer for the observer; on_intervention_signal is
        # what actually enforces Strategy.modalities, since the observer's
        # choice is a request, not a guarantee.
        allowed_for_modality = sorted(
            strategy.id
            for strategy in self._strategies.all()
            if utterance.modality in strategy.modalities
        )
        all_ids = sorted(strategy.id for strategy in self._strategies.all())
        modality_note = (
            f" This turn was {'spoken' if utterance.modality == 'audio' else 'typed'}: "
            f"only {allowed_for_modality} may be selected."
            if allowed_for_modality != all_ids
            else ""
        )
        # Found live: asked (in an open listening window) to "look up the
        # current price of gas," the observer produced no tool call at
        # all -- it judged respond_when_addressed's strict address
        # criteria, found no name/mention, and (never having been told a
        # reply is already guaranteed here regardless) concluded nothing
        # should fire, silently dropping needs_web_search along with it.
        # always_reply's own trigger_description says to fire it anyway,
        # but only if the observer gets that far in its own reasoning --
        # spelling the guarantee out here, at the point every strategy
        # catalog entry is weighed, removes the need to infer it.
        window_note = (
            " You are inside an open listening window (the user pressed "
            "to talk, or said your wake phrase): a reply is already "
            "guaranteed for this turn no matter what you decide, so "
            "'nothing applies' is not a real option -- if no more "
            "specific strategy fits, select always_reply, and still set "
            "needs_history/needs_web_search whenever they apply, exactly "
            "as you would for any other strategy."
            if utterance.always_reply or utterance.judge_wake_question
            else ""
        )
        # "wake_v2": a spoken turn already came with its turn facts
        # (conversation.py); no voice strategy reads these variables.
        if utterance.wake_v2 and utterance.modality == "audio":
            notes = f"{cooldown_note}{modality_note}{window_note}".strip()
            if notes:
                await port.tell_observers(notes)
        else:
            await port.tell_observers(
                "Current trigger variables: "
                f"{json.dumps(wm.trigger_variables)}.{cooldown_note}"
                f"{modality_note}{window_note}"
                f"{await self._project_note(wm)}"
            )

        # A decision -- forced or observer-judged -- is now pending for
        # this exact utterance. Cleared again once that decision's whole
        # cycle (including the assistant's reply, if any) resolves.
        self._pending_utterance = utterance
        self._turn_done.clear()

        if utterance.is_wake_phrase and utterance.judge_wake_question:
            # "transcribe"/"toggle" only (see judge_wake_question's own
            # comment on Utterance). Found live: "MULCA, could you look up
            # the latest videos by [X]?" -- a real question riding along
            # with the wake phrase itself -- used to always take the
            # immediate-answer shortcut below, which never consults the
            # observer at all, so needs_web_search could never be set for
            # it; MULCA just answered from what it already knew. The
            # question almost always names MULCA literally (it's spoken in
            # the same breath as the wake phrase), so respond_when_addressed
            # should fire normally here -- consult the observer like any
            # other addressed turn, so it gets the chance to flag
            # needs_web_search/needs_history. The "toggle"/"transcribe"
            # guarantee below still forces a reply regardless, exactly as
            # it would for a follow-up question later in the same window.
            return True

        if utterance.is_wake_phrase:
            # Either the trigger word alone ("assistant?", nothing to
            # answer yet), or a question riding along with it in "wake"
            # mode specifically -- "wake" judges only later, engaged turns,
            # never the one that starts the session (confirmed by
            # test_the_wake_phrase_engages_without_the_judge_and_announces_
            # once), so this stays the immediate, unjudged shortcut for
            # both of "wake"'s own cases. conversation.py already opened
            # the listening window/engaged session itself (that state lives
            # there, not here) -- this is the forced directive, same shape
            # as a forced intervention.
            # "wake": a bare one said again in an open session gets a
            # single word. Found in the 15-28 Oct exports: students said it
            # again and again, and each time got a sentence back.
            one_word = utterance.wake_restart
            if utterance.wake_phrase_has_question:
                directive = (
                    "Directly and helpfully answer what they just asked -- no "
                    "need to acknowledge or confirm you heard them first, just "
                    "answer."
                )
            elif one_word:
                directive = (
                    "Say one word that tells them you're listening (e.g. "
                    '"Listening"), in the language they spoke. Nothing else.'
                )
            else:
                directive = (
                    "Briefly acknowledge in your own words that you're "
                    "listening and ready (a word or two, not a fixed phrase). "
                    "Nothing else -- they haven't said anything to respond to yet."
                )
            wm.set_forced_context(directive)
            await port.instruct_main_agent(
                self._render_prompt(
                    wm,
                    directive=directive,
                    port=port,
                ),
                max_tokens=INTERIM_ACK_MAX_TOKENS if one_word else None,
            )
            await self._finish_turn(path="forced", strategy_id=None)
            return False

        forced = check_forced_intervention(utterance)
        if forced is None:
            return True

        wm.set_forced_context(forced)
        await port.instruct_main_agent(
            self._render_prompt(
                wm,
                directive=f"Forced intervention: {forced}",
                port=port,
            )
        )
        await self._finish_turn(path="forced", strategy_id=None)

        return False

    async def _apply_linked_group_context(
        self,
        wm: WorkingMemory,
        port: ConversationPort,
    ) -> None:
        """
        A DM's memory is its linked group's (docs/MEMORY_DESIGN.md): the
        group's overview and today, read-only -- what a DM's own session
        cannot otherwise see, since it lives in a different session. This
        DM's own turns reach its agents live, as always.

        Re-read on every DM message, not cached, so a question asked in a DM
        sees what the group said a moment ago. Never written back.

        A DM from someone not recognized as belonging to any instance has no
        linked_conversation_id, so this does nothing: no memory beyond their
        own live session, same as always.
        """

        if not (
            port.is_private
            and port.linked_conversation_id is not None
            and self._store_factory is not None
        ):
            return

        linked_store, linked_snapshot_store = self._store_factory(
            port.linked_conversation_id,
        )
        linked_snapshot = await linked_snapshot_store.load(
            port.linked_conversation_id,
        )
        if linked_snapshot is not None:
            # Only shown while the group has no overview yet.
            wm.history_summary = linked_snapshot.history_summary
        self._today_day = day_of(time.time())
        await self._load_memory(
            store=linked_store,
            conversation_id=port.linked_conversation_id,
        )
        if not self._today_lines and linked_snapshot is not None:
            # Nothing logged yet today: the group's saved recent window,
            # which is written every turn ("Mats says X in the group, then
            # immediately DMs about X").
            self._today_lines = [
                self._log_line(u.label(), u.modality, u.content)
                for u in (stored.restore() for stored in linked_snapshot.history_tail)
            ]

    async def on_intervention_signal(
        self,
        agent_guid: UUID,
        signal: InterventionSignal | None,
    ) -> None:
        wm = self._wm
        port = self._port
        assert wm is not None and port is not None

        # An empty strategy_id is a chosen silence (no_intervention).
        strategy_id = (signal.strategy_id or None) if signal else None
        prompt_fragment = signal.prompt_fragment if signal else None
        silence_reason = signal.silence_reason if signal else ""
        needs_history = bool(signal and signal.needs_history)
        history_period = signal.history_period if signal else "all"
        needs_web_search = bool(signal and signal.needs_web_search)
        web_search_query = signal.web_search_query if signal else ""
        flag_offensive_language = bool(signal and signal.flag_offensive_language)
        nudge_participation = bool(signal and signal.nudge_participation)

        logger.info(
            "%s intervention decision: strategy_id=%r prompt_fragment=%r "
            "needs_history=%s history_period=%r needs_web_search=%s "
            "web_search_query=%r "
            "flag_offensive_language=%s nudge_participation=%s",
            wm.conversation_id,
            strategy_id,
            prompt_fragment,
            needs_history,
            history_period,
            needs_web_search,
            web_search_query,
            flag_offensive_language,
            nudge_participation,
        )

        # Hard vetoes: not a request the model might ignore, the guarantee.
        # Both share one strategy lookup; either can clear strategy_id, in
        # which case the other finds nothing left to veto.
        strategy = (
            self._strategies.get(strategy_id)
            if strategy_id is not None
            else None
        )

        if strategy is not None and wm.strategy_in_cooldown(strategy):
            logger.info(
                "%s vetoed %r: still in cooldown",
                wm.conversation_id,
                strategy_id,
            )
            strategy_id = None
            prompt_fragment = None
            needs_history = False
            strategy = None

        # Same guarantee, for modality: a strategy whose config doesn't
        # list this turn's modality may never fire for it (e.g.
        # participation_equality_text for a spoken turn, or
        # participation_equality_voice for a typed one). See the
        # modality_note in on_utterance for why.
        if (
            strategy is not None
            and self._pending_utterance is not None
            and self._pending_utterance.modality not in strategy.modalities
        ):
            logger.info(
                "%s vetoed %r: not allowed for a %s turn",
                wm.conversation_id,
                strategy_id,
                self._pending_utterance.modality,
            )
            strategy_id = None
            prompt_fragment = None
            needs_history = False

        # Group Coordinator hook (M3). Its strategies (CS5, CS2, CS3) become
        # a card drafted in the background, never a free-text reply; its own
        # deterministic rules may veto them. Anything else: NOT_MINE, and the
        # turn goes on exactly as before. Skipped entirely when off.
        if strategy_id is not None and self._proactive is not None:
            verdict = await self._proactive.offer(
                strategy_id,
                history=list(wm.history),
                message_count=wm.total_message_count,
                assistant_id=self._assistant_id,
                reason=prompt_fragment or "",
            )
            if verdict != "not_mine":
                if verdict == "taken":
                    wm.record_action(strategy_id, wm.total_message_count)
                await self._finish_turn(
                    path="none",
                    strategy_id=None,
                    prompt_fragment=f"coordinator {verdict}: {strategy_id}",
                )
                return

        # "toggle"/"transcribe" guarantee: not a request the model might
        # ignore, same as the vetoes above -- "do nothing" is not a real
        # outcome for an utterance spoken during an explicitly opened
        # listening window, whether the model itself chose nothing or a
        # veto just cleared what it did choose. judge_wake_question is
        # included here too: that utterance's own always_reply is always
        # False (conversation.py snapshots it before the wake phrase opens
        # the window), but a question asked in the same breath as the wake
        # phrase gets exactly the same guarantee a later follow-up question
        # would, in the "transcribe"/"toggle" modes where it's judged at
        # all (see judge_wake_question's own comment on Utterance).
        if (
            strategy_id is None
            and self._pending_utterance is not None
            and (
                self._pending_utterance.always_reply
                or self._pending_utterance.judge_wake_question
            )
            and self._strategies.get("always_reply") is not None
        ):
            strategy_id = "always_reply"
            prompt_fragment = prompt_fragment or ""
            needs_history = False

        path = "none"
        if strategy_id is not None:
            wm.set_intervention_result(strategy_id, prompt_fragment)
            wm.record_action(strategy_id, wm.total_message_count)
            path = "strategy"
        else:
            wm.set_intervention_result(None, None)

        if path == "none":
            await self._finish_turn(
                path=path,
                strategy_id=None,
                prompt_fragment=silence_reason or None,
            )
            return

        if strategy_id == "remember":
            query = (
                extract_query(self._pending_utterance.content)
                if self._pending_utterance is not None
                else ""
            )
            await self._retrieve_similar(wm, port, query)
        elif needs_history or strategy_id == "provide_summary":
            wm.history_period = resolve_period(
                history_period,
                self._today_day or day_of(time.time()),
                [day.day for day in self._days],
            )
            await self._retrieve(wm, port)

        # Set only when a voice turn's web search actually ran -- gates the
        # extra brevity instruction appended to the directive below, voice
        # only, so a text reply's own web-search answer is unaffected.
        searched_for_voice = False

        # Independent of the branch above: a question can need both
        # history and a live lookup at once.
        if needs_web_search:
            modality = (
                self._pending_utterance.modality
                if self._pending_utterance is not None
                else None
            )
            if not web_search_query:
                # Found live: needs_web_search=True arrived with an empty
                # web_search_query often enough to be worth its own log
                # line -- the model set one flag and not the other,
                # silently producing no search at all otherwise, with
                # nothing in the logs to explain why.
                logger.warning(
                    "%s needs_web_search was set with no web_search_query "
                    "-- skipping, nothing to search for",
                    wm.conversation_id,
                )
            elif self._web_searcher is None:
                logger.info(
                    "%s needs_web_search set but no web searcher is "
                    "configured -- skipping",
                    wm.conversation_id,
                )
            elif modality == "audio":
                # Voice: "let me check that" -> narrow search -> the real
                # answer, as two separate spoken turns, not one reply with
                # a silent multi-second gap in it. on_assistant_output
                # won't resolve the turn for this interim one (see
                # _interim_ack_done) -- the real resolution is the normal
                # _finish_turn call below, once this falls through to it.
                ack_done = asyncio.Event()
                self._interim_ack_done = ack_done
                await port.instruct_main_agent(
                    self._render_prompt(
                        wm,
                        directive=(
                            "Briefly acknowledge, in your own words, that "
                            "you're looking this up right now (e.g. "
                            '"Let me check that") -- ONE short sentence, '
                            "not a fixed phrase, and absolutely nothing "
                            "else: no numbers, names, facts, or findings "
                            "of any kind, and do not say you've already "
                            "found or looked anything up -- you have not, "
                            "no search has run yet. Say the one sentence "
                            "and stop; do not say it twice or rephrase it "
                            "a second way."
                        ),
                        port=port,
                    ),
                    max_tokens=INTERIM_ACK_MAX_TOKENS,
                )
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        ack_done.wait(),
                        timeout=self._interim_ack_timeout_seconds,
                    )
                self._interim_ack_done = None
                await self._retrieve_web_search(wm, web_search_query)
                searched_for_voice = True
            else:
                await self._retrieve_web_search(wm, web_search_query)

        strategy = self._strategies.get(strategy_id)
        directive = (
            strategy.prompt_template.format(
                prompt_fragment=prompt_fragment or "",
            )
            if strategy is not None
            else prompt_fragment or ""
        )
        if searched_for_voice:
            # Spoken, not read -- a long recitation of search findings is
            # much harder to follow by ear than the same text would be to
            # skim. Appended only here, after the real findings directive
            # is otherwise fully built, so a text reply's own web-search
            # answer (which has no such length pressure) is untouched.
            directive += (
                " Report the findings in one or two short sentences, the "
                "single most relevant fact or number only -- this is "
                "spoken aloud, not read, so skip caveats, source lists, "
                "and secondary details unless they directly asked for them."
            )

        # Independent riders: a short overlay from a *different* strategy,
        # spliced onto this turn's directive rather than replacing it (see
        # InterventionSignal.flag_offensive_language/nudge_participation).
        # Each still goes through that strategy's own cooldown/modality
        # veto, same guarantee as the primary pick above, and firing one
        # starts its cooldown exactly as a standalone firing would.
        rider_modality = (
            self._pending_utterance.modality
            if self._pending_utterance is not None
            else None
        )
        for flagged, rider_id in (
            (flag_offensive_language, "pleasant_conversation"),
            (nudge_participation, "participation_equality_text"),
        ):
            if not flagged or rider_id == strategy_id:
                continue

            rider = self._strategies.get(rider_id)
            if rider is None or not rider.overlay_template:
                continue

            if wm.strategy_in_cooldown(rider):
                logger.info(
                    "%s rider %r vetoed: still in cooldown",
                    wm.conversation_id,
                    rider_id,
                )
                continue

            if rider_modality is not None and rider_modality not in rider.modalities:
                logger.info(
                    "%s rider %r vetoed: not allowed for a %s turn",
                    wm.conversation_id,
                    rider_id,
                    rider_modality,
                )
                continue

            directive += " " + rider.overlay_template.strip()
            wm.record_action(rider_id, wm.total_message_count)

        await port.instruct_main_agent(
            self._render_prompt(
                wm,
                directive=directive,
                port=port,
                force_summary=strategy_id == "provide_summary",
            ),
        )

        await self._finish_turn(
            path=path,
            strategy_id=strategy_id,
            prompt_fragment=prompt_fragment,
        )

    async def on_assistant_output(
        self,
        text: str,
        *,
        interrupted: bool,
    ) -> None:
        wm = self._wm
        # The one place this fires for both modalities (conversation.py
        # calls it uniformly whether the reply streamed as text or spoken
        # audio) -- found live, investigating what actually triggered a
        # reply had no way to see what MULCA actually said, only the
        # directive sent to it and the strategy decision, never its own
        # output.
        logger.info(
            "%s MULCA said (interrupted=%s): %r",
            wm.conversation_id if wm is not None else "?",
            interrupted,
            text,
        )
        if wm is not None and text:
            wm.append_history(
                Utterance(
                    speaker_id=self._assistant_id,
                    speaker_name=self._assistant_name,
                    content=text,
                    timestamp_ms=int(time.time() * 1000),
                )
            )
            wm.trim_history(tail_size=self._recent_window)
            if self._port is not None and not self._port.is_private:
                modality = (
                    self._pending_utterance.modality
                    if self._pending_utterance is not None
                    else "text"
                )
                self._today_lines.append(
                    self._log_line(self._assistant_name or "MULCA", modality, text)
                )

        if self._interim_ack_done is not None:
            # This was the interim "let me check that" standing in for a
            # voice web search's real reply -- already logged to history
            # above for conversational continuity, but the turn itself
            # isn't done: the real reply (the one that actually answers
            # the question) is still coming.
            self._interim_ack_done.set()
            self._interim_ack_done = None
            return

        # Resolves regardless of whether text was empty: a turn that
        # triggered a reply but got nothing back (interrupted, or the model
        # produced no output) still needs its pending record flushed and the
        # turn marked done, or wait_for_turn_to_finish() would hang until
        # its timeout for no reason.
        await self._resolve_pending_turn(assistant_output=text or None)

    async def wait_for_turn_to_finish(self) -> None:
        """
        Blocks until the turn started by the most recent on_utterance() call
        has fully resolved -- Conversation awaits this before letting the
        next utterance through, restoring the "queued, not interleaved"
        guarantee for text (audio's own self-interrupt reflex is a
        deliberate, separate exception -- this does not apply there).
        """

        try:
            await asyncio.wait_for(
                self._turn_done.wait(),
                timeout=self._turn_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Turn for %s did not resolve within %.0fs -- forcing it "
                "to finish so the conversation does not hang forever.",
                self._wm.conversation_id if self._wm else "?",
                self._turn_timeout_seconds,
            )
            await self._flush_pending_episodic_record()
            self._pending_utterance = None
            self._turn_done.set()

    def observer_catchup_text(self) -> str:
        """
        The memory for an observer that was never live for any of it --
        after a restart, a new observer mid-conversation, or a context
        reset. Empty if there's nothing to catch up on.
        """

        memory = self.memory_text()
        if not memory:
            return ""
        return (
            "Catching you up on this conversation's memory, which you were "
            f"not connected for:\n\n{memory}"
        )

    def memory_text(
        self,
        *,
        overview: bool = True,
        today: bool = True,
    ) -> str:
        """
        What both agents know beyond their live session (docs/
        MEMORY_DESIGN.md): the overview, then today -- verbatim if it fits,
        otherwise "today so far" plus the most recent messages verbatim.
        A question about one period leaves out what isn't in it.
        """

        wm = self._wm
        if wm is None:
            return ""

        parts = []
        if overview and self._overview is not None:
            parts.append(
                render_summary(
                    self._overview.content,
                    heading=f"Project overview (as of {day_heading(self._overview.day)}):",
                )
            )
        elif overview and wm.history_summary:
            # Until the first overview exists (e.g. while past days are
            # still being summarized after this memory was introduced).
            parts.append(f"Summary of the conversation so far:\n{wm.history_summary}")

        if not today:
            return "\n\n".join(parts)

        heading = day_heading(self._today_day) if self._today_day else "Today"
        today_text = "\n".join(self._today_lines)
        if self._today_lines and estimate_tokens(today_text) <= self._today_verbatim_tokens:
            parts.append(f"Today ({heading}) so far, verbatim:\n{today_text}")
        elif self._today_lines:
            if self._today is not None:
                parts.append(
                    render_summary(
                        self._today.content,
                        heading=f"Today ({heading}) so far, summarized:",
                    )
                )
            recent = wm.transcript() if not self._is_private() else ""
            if not recent:
                # A DM: the group's latest lines instead of the DM's own.
                recent = "\n".join(self._today_lines[-self._recent_window:])
            parts.append(f"Most recent messages, verbatim:\n{recent}")
        elif wm.history and not self._is_private():
            parts.append(f"Recent messages, verbatim:\n{wm.transcript()}")

        return "\n\n".join(parts)

    def _is_private(self) -> bool:
        return self._port is not None and self._port.is_private

    @staticmethod
    def _log_line(speaker: str, modality: str, content: str) -> str:
        channel = "voice" if modality == "audio" else "text"
        return f"{speaker} ({channel}): {content}"

    async def _load_memory(
        self,
        *,
        store: EpisodicStore | None = None,
        conversation_id: str | None = None,
        today_log: bool = True,
    ) -> None:
        """
        Summaries, and (today_log) today's log, from the store. A running
        session reloads only the summaries: its today has lines the store
        doesn't have yet (a turn still waiting for its reply).
        """

        store = store or self._episodic_store
        conversation_id = conversation_id or (
            self._port.conversation_id if self._port else ""
        )
        if store is None or self._today_day is None:
            return

        summaries = await store.load_summaries(conversation_id)
        overviews = [s for s in summaries if s.kind == "overview"]
        self._overview = overviews[-1] if overviews else None
        self._days = [s for s in summaries if s.kind == "day"]
        todays = [
            s for s in summaries if s.kind == "today" and s.day == self._today_day
        ]
        self._today = todays[-1] if todays else None
        if not today_log:
            return

        start, end = day_bounds(self._today_day)
        self._today_lines = day_log_lines(
            await store.records_between(conversation_id, start, end),
            assistant_name=self._assistant_name or "MULCA",
            call_ends=await store.call_ends_between(conversation_id, start, end),
        )

    def _start_memory_task(self, coroutine) -> None:
        if self._day_summarizer is None or self._episodic_store is None:
            coroutine.close()
            return
        if self._memory_task is not None and not self._memory_task.done():
            # One at a time; the running one already reads the latest log.
            coroutine.close()
            return
        self._memory_task = asyncio.create_task(coroutine)

    async def wait_for_memory(self) -> None:
        """Until a running rollover or "today so far" finishes (tests)."""

        if self._memory_task is not None:
            with suppress(asyncio.CancelledError):
                await self._memory_task

    async def _roll_over(self) -> None:
        """
        Every earlier day with something said and no day summary gets one
        (backfill, the first time), then the overview is rebuilt from all of
        them. Then "today so far", if today has outgrown the verbatim limit.
        Each summary is saved as soon as it exists, so a failure keeps the
        rest and the next trigger retries what's missing.
        """

        store = self._episodic_store
        port = self._port
        if store is None or port is None or self._day_summarizer is None:
            return
        conversation_id = port.conversation_id
        today = self._today_day or day_of(time.time())

        try:
            have = {s.day for s in self._days}
            spoken = {day_of(ts) for ts in await store.spoken_timestamps(conversation_id)}
            missing = sorted(day for day in spoken if day < today and day not in have)

            for day in missing:
                start, end = day_bounds(day)
                records = await store.records_between(conversation_id, start, end)
                lines = day_log_lines(
                    records,
                    assistant_name=self._assistant_name or "MULCA",
                    call_ends=await store.call_ends_between(conversation_id, start, end),
                )
                if not lines:
                    continue
                content = await self._day_summarizer.summarize_day(
                    day=day,
                    log="\n".join(lines),
                    final=True,
                )
                await store.save_summary(
                    conversation_id,
                    MemorySummary(
                        kind="day",
                        day=day,
                        content=content,
                        through_ts=records[-1].timestamp,
                        created_at=time.time(),
                    ),
                )
                logger.info("%s day summary written for %s", conversation_id, day)

            summaries = await store.load_summaries(conversation_id)
            days = [s for s in summaries if s.kind == "day"]
            has_overview = any(s.kind == "overview" for s in summaries)
            if days and (missing or not has_overview):
                overview = await self._day_summarizer.build_overview(
                    days=[(s.day, s.content) for s in days],
                )
                await store.save_summary(
                    conversation_id,
                    MemorySummary(
                        kind="overview",
                        day=today,
                        content=overview,
                        created_at=time.time(),
                    ),
                )
                logger.info("%s overview rebuilt from %d days", conversation_id, len(days))

            if any(s.kind == "today" and s.day != today for s in summaries):
                # Replaced by those days' own summaries.
                await store.delete_summaries(conversation_id, "today")

            await self._load_memory(today_log=False)
            if self._today_too_long():
                await self._refresh_today()
        except Exception:
            logger.warning(
                "%s memory rollover failed; retried at the next trigger",
                conversation_id,
                exc_info=True,
            )
            return

        await self._renew_agent_memory()

    def _today_too_long(self) -> bool:
        return (
            estimate_tokens("\n".join(self._today_lines))
            > self._today_verbatim_tokens
        )

    async def _refresh_today(self) -> None:
        """"Today so far", always regenerated from today's full log."""

        store, port = self._episodic_store, self._port
        assert store is not None and port is not None and self._day_summarizer is not None
        today = self._today_day or day_of(time.time())
        start, end = day_bounds(today)
        records = await store.records_between(port.conversation_id, start, end)
        lines = day_log_lines(
            records,
            assistant_name=self._assistant_name or "MULCA",
            call_ends=await store.call_ends_between(port.conversation_id, start, end),
        )
        if not lines:
            return
        content = await self._day_summarizer.summarize_day(
            day=today,
            log="\n".join(lines),
            final=False,
        )
        self._today = MemorySummary(
            kind="today",
            day=today,
            content=content,
            through_ts=records[-1].timestamp,
            created_at=time.time(),
        )
        await store.save_summary(port.conversation_id, self._today)
        logger.info("%s today so far summarized", port.conversation_id)

    async def on_context_full(self) -> None:
        """
        An agent's live session is full: instead of dropping its oldest
        half with nothing in its place, give both agents a fresh session
        with the memory -- summarizing today first if it no longer fits
        verbatim. In the background; never holds up a turn.
        """

        self._start_memory_task(self._renew_after_context_full())
        if self._day_summarizer is None or self._episodic_store is None:
            # Nothing to summarize with: still reset, with what there is.
            await self._renew_agent_memory()

    async def _renew_after_context_full(self) -> None:
        try:
            if not self._is_private() and self._today_too_long():
                await self._refresh_today()
        except Exception:
            logger.warning(
                "%s today so far failed; resetting with what there is",
                self._port.conversation_id if self._port else "?",
                exc_info=True,
            )
        await self._renew_agent_memory()

    async def _renew_agent_memory(self) -> None:
        """Both agents start over from the memory, between turns."""

        if self._port is None:
            return
        self._memory_sent = False
        self._full_context_sent_at = None
        await self._port.reset_agent_contexts(self.observer_catchup_text())

    async def on_untranscribed_turn(
        self,
        speaker_id: int,
        speaker_name: str,
        timestamp_ms: int,
    ) -> None:
        """
        A voice turn ended with no transcript (noise, provider failure).
        Logs the bare fact that something happened, same file a DM's audit
        record goes to -- but doesn't touch WM: there's no content to add
        to conversational history or judge, and wm.total_message_count
        driving cooldowns shouldn't advance for a turn
        with nothing in it.
        """

        if self._episodic_store is None:
            return

        await self._episodic_store.log(
            EpisodicRecord(
                timestamp=timestamp_ms / 1000,
                conversation_id=self._wm.conversation_id if self._wm else "",
                intervention_path="untranscribed",
                speaker=speaker_name or f"User {speaker_id}",
                modality="audio",
            )
        )

    async def on_session_event(
        self,
        event: str,
        detail: str = "",
        *,
        speaker: str = "",
        data: dict | None = None,
    ) -> None:
        if self._episodic_store is None or self._wm is None:
            return

        await self._episodic_store.log_session_event(
            self._wm.conversation_id,
            event,
            timestamp=time.time(),
            speaker=speaker,
            detail=detail,
            data=data,
        )

    async def _resolve_pending_turn(
        self,
        *,
        assistant_output: str | None,
    ) -> None:
        if self._pending_episodic_record is not None:
            record = self._pending_episodic_record.model_copy(
                update={"assistant_output": assistant_output},
            )
            self._pending_episodic_record = None
            if self._episodic_store is not None:
                await self._episodic_store.log(record)

        self._pending_utterance = None
        self._turn_done.set()

    async def _flush_pending_episodic_record(self) -> None:
        """
        Logs a still-pending record as-is (no assistant_output) -- for a
        turn whose reply never arrived, so it is not silently lost the next
        time a record is about to be written.
        """

        # Taken before the await, not after. Found live (group 16, 14 Oct): a
        # reply arriving during the log write still saw the record pending,
        # logged its own copy with the reply, and the turn appeared twice.
        record = self._pending_episodic_record
        self._pending_episodic_record = None
        if record is not None and self._episodic_store is not None:
            await self._episodic_store.log(record)

    # ------------------------------------------------------------------

    async def _retrieve(
        self,
        wm: WorkingMemory,
        port: ConversationPort,
    ) -> None:
        store, conversation_id = self._retrieval_source(port)
        if store is None:
            wm.set_retrieved_context("(no prior conversation found)")
            return

        # A question about one period gets only that period's days. Found
        # live (15 Oct): "what did we talk about today" was handed every
        # earlier day too, and the recap told the whole story instead.
        if wm.history_period is not None:
            first, last = wm.history_period
            today = self._today_day or day_of(time.time())
            days = [day for day in self._days if first <= day.day <= last]
            parts = [
                f"The question is about {period_label(first, last)}: "
                "answer only about that period."
            ]
            parts += [
                render_summary(day.content, heading=f"Day summary, {day_heading(day.day)}:")
                for day in days
            ]
            if not days and not first <= today <= last:
                parts.append("(nothing was recorded in that period)")
            wm.set_retrieved_context("\n\n".join(parts))
            return

        # The days before today, as summarized -- today itself is already
        # in the agent's memory. The recent log only until any day has a
        # summary.
        if self._days:
            wm.set_retrieved_context(
                "\n\n".join(
                    render_summary(day.content, heading=f"Day summary, {day_heading(day.day)}:")
                    for day in self._days
                )
            )
            return

        records = await store.recall_recent(
            conversation_id,
            self._retrieval_limit,
        )

        lines = [
            f"{record.speaker}: {record.content}"
            + (
                f"\n{self._assistant_name or 'Assistant'}: {record.assistant_output}"
                if record.assistant_output
                else ""
            )
            for record in records
            if record.content
        ]

        wm.set_retrieved_context(
            "\n".join(lines) or "(no prior conversation found)",
        )

    async def _retrieve_web_search(
        self,
        wm: WorkingMemory,
        query: str,
    ) -> None:
        """
        Live lookup for current information this conversation has no other
        way to know -- separate from _retrieve/_retrieve_similar above,
        which both only ever look backward into this same conversation's
        own memory. self._web_searcher is already confirmed non-None by
        the caller.
        """

        assert self._web_searcher is not None
        result = await self._web_searcher.search(query)
        wm.set_web_search_context(result)

    async def _retrieve_similar(
        self,
        wm: WorkingMemory,
        port: ConversationPort,
        query: str,
    ) -> None:
        """
        "remember"'s own retrieval: a few specific facts ranked by
        relevance to `query`, not a chronological window -- see
        _retrieve above for that. `query` is the extracted topic
        (query_extraction.extract_query), not the whole triggering
        message, so similarity search isn't diluted by its own wrapper
        phrasing ("tell me something about").
        """

        store, conversation_id = self._retrieval_source(port)
        if store is None:
            wm.set_retrieved_context("(no prior conversation found)")
            return

        # A failed or misconfigured search is reported to the main agent
        # as "couldn't check", not left to crash the whole turn -- the
        # intervention agent's event loop has no other handling for an
        # exception raised this deep, so the turn would otherwise just
        # silently never get a reply at all.
        try:
            records = await store.search_similar(
                conversation_id,
                query,
                limit=self._similarity_retrieval_limit,
            )
        except Exception:
            logger.warning(
                "%s: search_similar failed for 'remember'",
                wm.conversation_id,
                exc_info=True,
            )
            wm.set_retrieved_context(
                "(couldn't search memory right now -- say so, don't guess)",
            )
            return

        lines = [
            f"{record.speaker}: {record.content}"
            for record in records
            if record.content
        ]

        wm.set_retrieved_context(
            "\n".join(lines)
            or f"(nothing found related to {query!r})",
        )

    def _retrieval_source(
        self,
        port: ConversationPort,
    ) -> tuple[EpisodicStore | None, str]:
        """
        Where a history lookup reads from. A DM's own log is written now
        (an audit record), but retrieval deliberately never reads it --
        intentionally kept to "what the group said", not "what this person
        told MULCA privately". A DM's retrieval always redirects to its
        linked group's log instead (read-only, as with
        _apply_linked_group_context), or nowhere if it has none.
        """

        if not port.is_private:
            return self._episodic_store, port.conversation_id

        linked = port.linked_conversation_id
        if linked is None or self._store_factory is None:
            return None, ""

        linked_store, _ = self._store_factory(linked)
        return linked_store, linked

    async def _finish_turn(
        self,
        *,
        path: str,
        strategy_id: str | None,
        prompt_fragment: str | None = None,
    ) -> None:
        wm = self._wm
        port = self._port
        assert wm is not None and port is not None
        self._last_turn_strategy = strategy_id

        # Not wm.history[-1]: by the time this runs, a second utterance
        # could already have been appended to history if something upstream
        # ever stops awaiting wait_for_turn_to_finish() again -- this is
        # what actually ties the log entry to the utterance that caused it.
        utterance = self._pending_utterance
        message_count = wm.total_message_count

        if self._episodic_store is not None:
            # DMs are logged too (their own separate file, via
            # store_factory) -- an audit record of what was said, same as a
            # group. This is deliberately NOT a memory/retrieval source: a
            # DM's own retrieval always redirects to its linked group's log
            # instead (_retrieval_source), never to this file, and a
            # group's own retrieval/memory has no path to a DM's
            # file at all (one SQLite file per conversation_id, never
            # shared). So this file exists for review, not recall.
            #
            # A record still sitting here means a previous turn's reply
            # never arrived to attach itself to -- flush it plain rather
            # than silently lose it when the line below replaces it.
            await self._flush_pending_episodic_record()

            record = EpisodicRecord(
                timestamp=time.time(),
                conversation_id=port.conversation_id,
                intervention_path=path,
                speaker=utterance.label() if utterance else "",
                content=utterance.content if utterance else "",
                modality=utterance.modality if utterance else "text",
                trigger_variables=dict(wm.trigger_variables),
                strategy_id=strategy_id,
                prompt_fragment=prompt_fragment,
            )

            if path == "none":
                # Nothing was triggered, so there is no reply to wait for.
                await self._episodic_store.log(record)
            else:
                # Written once on_assistant_output supplies the actual
                # reply text, so the log captures what MULCA said, not
                # just what triggered it -- this never happened before.
                self._pending_episodic_record = record

        # Every turn: a restart picks up the recent window and cooldowns
        # from here.
        await self._save_snapshot()

        wm.clear_turn_output()

        if path == "none":
            # Nothing triggered, so nothing to wait for -- resolves this
            # turn immediately instead of leaving it "in progress" until
            # wait_for_turn_to_finish()'s timeout for no reason.
            self._pending_utterance = None
            self._turn_done.set()
            await port.no_reply()

    def _render_prompt(
        self,
        wm: WorkingMemory,
        *,
        directive: str,
        port: ConversationPort,
        force_summary: bool = False,
    ) -> str:
        """
        Assemble this turn's payload for the main agent -- everything
        *except* persona/task, which MainAgentOpenAI sends once at connect
        (session.update replaces rather than merges instructions, so
        resending them here every turn would just be waste, not safety).
        Pushed as a conversation item, not a session-instructions
        overwrite -- see MainAgentOpenAI._trigger_response.

        force_summary: provide_summary's own case -- a recap is exactly
        when the memory is worth re-sending, even if the live session has
        it already.
        """

        # The memory goes with the first reply of a session, and again after
        # a context reset (_renew_agent_memory). A DM's memory is a
        # different conversation's, which moves on without it -- re-sent
        # once an hour instead of once.
        now = time.time()
        if port.is_private:
            needs_memory = (
                self._full_context_sent_at is None
                or now - self._full_context_sent_at
                > DM_FULL_CONTEXT_INTERVAL_SECONDS
            )
            if needs_memory:
                self._full_context_sent_at = now
        else:
            needs_memory = not self._memory_sent
            self._memory_sent = True

        # Grounds "upcoming"/"next week"/etc. in an actual date -- the main
        # agent otherwise has no sense of the current date at all. Sourced
        # from the most recent utterance's own timestamp_ms (already
        # tracked on every Utterance, already in wm.history) rather than a
        # fresh system clock read: this is data the conversation already
        # has, not a new dependency. Re-announced only when the calendar
        # date actually changes, not every turn -- a session can run for
        # hours, and repeating the same date on every single reply was pure
        # waste once it was already established earlier in the connection.
        current_date = (
            datetime.fromtimestamp(
                wm.history[-1].timestamp_ms / 1000, tz=DAY_TZ
            ).date()
            if wm.history
            else None
        )
        send_date = (
            current_date is not None
            and current_date != self._last_date_announced
        )
        if send_date:
            self._last_date_announced = current_date

        current_time_block = (
            f"Today's date: {current_date:%A, %Y-%m-%d}\n\n"
            if send_date
            else ""
        )

        # The actual live roster (who is in the voice call right now,
        # whether or not they've said anything), not an inference from who
        # has spoken -- found live, "who is in the chat" was being
        # answered from the speaker-tagged transcript alone (only the
        # people heard from), missing anyone present but silent. Every
        # turn, not gated: the roster can change (join/leave) between any
        # two turns, same reasoning as current_time_block above.
        voice_roster = port.voice_participants()
        voice_roster_block = (
            f"Currently in the voice channel: {', '.join(voice_roster.values())}\n\n"
            if voice_roster
            else ""
        )

        if wm.history_period is not None:
            first, last = wm.history_period
            today = self._today_day or day_of(time.time())
            memory_parts = {"overview": False, "today": first <= today <= last}
        else:
            memory_parts = {}
        memory = (
            self.memory_text(**memory_parts)
            if needs_memory or force_summary
            else ""
        )
        memory_block = (
            f"What you remember of this conversation:\n{memory}\n\n"
            if memory
            else ""
        )

        retrieved_block = (
            f"Retrieved conversation history:\n{wm.retrieved_context}\n\n"
            if wm.retrieved_context
            else ""
        )

        web_search_block = (
            f"Web search results:\n{wm.web_search_context}\n\n"
            if wm.web_search_context
            else ""
        )

        # Found live (15-19 Oct, four times): MULCA followed briefs that
        # were wrong about what it could see -- a pasted analysis "not
        # included", "only a header" of a 20,000-character file, the wrong
        # text for "do that" -- since this used to leave it only phrasing,
        # tone and delivery.
        directive_label = (
            "Guidance from a lightweight judgment step: it decided that you "
            "should reply and roughly how, but it doesn't see everything "
            "you see -- attached files, documents, web search results, "
            "retrieved history, the full conversation. Where it conflicts "
            "with what you can see, go by what you see. Phrasing, tone, "
            "delivery and content are your call:"
            if wm.latest_strategy_id is not None
            else "Current directive:"
        )

        # Found live: without being told who a reply is for, the main
        # agent hedged about who was speaking -- the platform knows, it
        # isn't guessing from voices.
        pending = self._pending_utterance
        speaker_block = (
            f"Replying to: {pending.label()} ({pending.modality})\n\n"
            if pending is not None
            else ""
        )

        return (
            f"{current_time_block}"
            f"{voice_roster_block}"
            f"{memory_block}"
            f"{retrieved_block}"
            f"{web_search_block}"
            f"{speaker_block}"
            f"{directive_label}\n"
            f"{directive or 'Respond naturally as the ongoing conversation warrants.'}"
        )
