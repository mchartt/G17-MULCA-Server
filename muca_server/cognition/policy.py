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
from datetime import date, datetime, timezone
from collections.abc import Callable
from uuid import UUID

from .forced_intervention import check_forced_intervention
from .memory.in_memory import InMemoryEpisodicStore, InMemorySnapshotStore
from .memory.ports import Embedder, EpisodicStore, SnapshotStore
from .memory.records import EpisodicRecord, MemorySnapshot, StoredUtterance
from .ports import (
    ConversationPort,
    InterventionSignal,
    Summarizer,
    Utterance,
    WebSearcher,
)
from .query_extraction import extract_query
from .strategies import StrategyStore
from .trigger_variables import compute_trigger_variables
from .working_memory import WorkingMemory

logger = logging.getLogger(__name__)

RETRIEVAL_LIMIT = 50
# Sized for the actual deployment this project targets: 10 teams x 6 tasks x
# ~30 min sessions each, one shared conversation_id per team across all of
# them (nothing resets between tasks). A rough ~2-5 messages/minute estimate
# for an active group discussion puts one session at ~60-150 turns; 24
# keeps several minutes of recent back-and-forth fully verbatim (not relying
# on the compressed summary for anything just said) without resending a huge
# block on every single triggered reply -- that per-turn resend, not the
# summarization call itself, is the bigger cost driver, since replies fire
# far more often than management cycles do. The old default of 8 was a demo
# value, not tuned for this; raising it also needed working_memory.py's
# transcript() to stop independently re-capping at a smaller, unrelated 20.
MANAGEMENT_CYCLE_INTERVAL = 24
TURN_TIMEOUT_SECONDS = 30.0

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
        summarizer: Summarizer | None = None,
        embedder: Embedder | None = None,
        web_searcher: WebSearcher | None = None,
        management_cycle_interval: int = MANAGEMENT_CYCLE_INTERVAL,
        retrieval_limit: int = RETRIEVAL_LIMIT,
        similarity_retrieval_limit: int = SIMILARITY_RETRIEVAL_LIMIT,
        turn_timeout_seconds: float = TURN_TIMEOUT_SECONDS,
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
        self._summarizer = summarizer
        self._embedder = embedder
        self._management_cycle_interval = management_cycle_interval
        self._retrieval_limit = retrieval_limit
        self._similarity_retrieval_limit = similarity_retrieval_limit
        self._turn_timeout_seconds = turn_timeout_seconds

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

        # Whether this session's main agent has already been sent the
        # Gates summary_block/linked_tail_block in _render_prompt -- the
        # "foreign" content (a group's own summary, or a DM's mirrored read
        # of its linked group) the live session has no other way to learn,
        # since it isn't present in the session's own native turn-by-turn
        # memory. For a group this is sent once, ever, since the live
        # session already has everything its own summary covers (it was
        # there when it was said) -- only checked for None, never reset.
        # For a DM it's genuinely foreign (a different conversation this
        # session was never part of) and re-checked against
        # DM_FULL_CONTEXT_INTERVAL_SECONDS instead of only-once. None at
        # construction and never reset mid-session on purpose: a reconnect
        # builds a fresh CoalaPolicy via policy_factory, so a new session
        # starts this at None again without any explicit reset.
        self._full_context_sent_at: float | None = None

        # Gates recent_block -- this conversation's OWN recent turns,
        # verbatim. Pure redundancy once the live session has natively seen
        # them (which happens as they're said, same as any other turn), so
        # sent once per connection only, to cover the one case the live
        # session genuinely doesn't have it: freshly reconnected, wm.history
        # restored from a snapshot the new session was never actually live
        # for. Not time-gated like the DM case above -- whether the live
        # session has this natively is purely a function of connection
        # lifetime, not wall-clock time elapsed.
        self._own_tail_sent = False

        # Gates current_time_block -- re-announced only when the calendar
        # date actually changes (by the clock of each turn's own
        # timestamp_ms), not every single turn. Not persisted across a
        # reconnect, same reasoning as _own_tail_sent above: a reconnect is
        # a brand new main-agent session with no memory of a date
        # announced in a previous one.
        self._last_date_announced: date | None = None

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
            if snapshot is not None:
                self._full_context_sent_at = snapshot.full_context_sent_at
            if snapshot is not None and not port.is_private:
                wm.history_summary = snapshot.history_summary
                wm.participant_roles_summary = (
                    snapshot.participant_roles_summary
                )
                wm.total_message_count = snapshot.total_message_count
                wm.history = [
                    stored.restore()
                    for stored in snapshot.history_tail
                ]
                wm.recent_actions = dict(snapshot.recent_actions)
                wm.recent_action_at = dict(snapshot.recent_action_at)

        self._wm = wm

    async def close(self) -> None:
        # A turn whose reply never arrived is still worth its log entry.
        await self._flush_pending_episodic_record()
        await self._save_snapshot()

    async def _save_snapshot(self) -> None:
        """
        Also called from the management cycle, not just close() -- a clean
        shutdown is not the only way a process's memory can end. Saving here
        too means a crash loses at most the messages since the last
        management cycle, not everything since the last clean shutdown.
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

    async def on_utterance(self, utterance: Utterance) -> bool:
        """
        Somebody said something, in text or (once transcribed) in speech.

        Conversation calls this once per whole turn, one turn at a time, and
        awaits wait_for_turn_to_finish() before handing over the next.
        """

        wm = self._wm
        port = self._port
        assert wm is not None and port is not None

        wm.append_history(utterance)
        wm.increment_message_count()

        await self._apply_linked_group_context(wm, port)

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
        text_vars = compute_trigger_variables(
            [u for u in wm.history if u.modality == "text"],
            assistant_id=self._assistant_id,
            participants=participants,
        )
        voice_vars = compute_trigger_variables(
            [u for u in wm.history if u.modality == "audio"],
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
        await port.tell_observers(
            "Current trigger variables: "
            f"{json.dumps(wm.trigger_variables)}.{cooldown_note}{modality_note}"
        )

        # A decision -- forced or observer-judged -- is now pending for
        # this exact utterance. Cleared again once that decision's whole
        # cycle (including the assistant's reply, if any) resolves.
        self._pending_utterance = utterance
        self._turn_done.clear()

        if utterance.is_wake_phrase:
            # conversation.py already opened the listening window itself
            # (that state lives there, not here) -- this is the forced
            # directive, same shape as a forced intervention. Two cases:
            # the trigger word alone ("assistant?") gets a brief
            # acknowledgement and nothing else, since there's nothing yet
            # to answer; a real question in the same breath ("hey mulca,
            # could you define the UNG diagram") just gets answered
            # directly -- found live, explicitly asking for an
            # acknowledgement here too ("Briefly acknowledge... then
            # answer") produced a stilted "I heard you, ..." preamble
            # before every answer. A real question needs no acknowledgement
            # at all; just answering it already shows it was heard.
            directive = (
                "Directly and helpfully answer what they just asked -- no "
                "need to acknowledge or confirm you heard them first, just "
                "answer."
                if utterance.wake_phrase_has_question
                else "Briefly acknowledge in your own words that you're "
                "listening and ready (a word or two, not a fixed phrase). "
                "Nothing else -- they haven't said anything to respond to yet."
            )
            wm.set_forced_context(directive)
            await port.instruct_main_agent(
                self._render_prompt(
                    wm,
                    directive=directive,
                    port=port,
                )
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
        Mirrors a linked group's summary AND raw tail directly into this
        DM's own WM -- reusing exactly the fields (and _render_prompt's
        existing rendering) a group already uses for its own memory, rather
        than a separate DM-only concept. This DM's own raw conversation
        still reaches the main/intervention agents the normal way
        (Conversation forwards each utterance to their live sessions
        directly) -- this only supplies what a DM's session cannot
        otherwise see at all: the group's memory, since it lives in a
        different session entirely.

        Both the summary and the tail matter here, not just the summary:
        the group's snapshot only gets saved every management_cycle_interval
        messages (or on a clean group shutdown), so something said in the
        group very recently may not be reflected in history_summary yet at
        all. The tail is what covers that gap -- it is exactly the same raw
        messages the group's own next management cycle will summarize, just
        not compressed yet.

        Re-read on every message, not cached: _render_prompt sends the
        summary and recent tail with every triggered turn anyway, so caching
        would only save DB reads, at the cost of staleness, which matters
        for "ask individual questions whenever you want" over a
        long-running deployment. This never writes back to the linked
        conversation either way.

        A DM from someone not recognized as belonging to any instance simply
        has no linked_conversation_id, so this does nothing for them --
        no memory beyond their own live session, same as always.
        """

        if not (
            port.is_private
            and port.linked_conversation_id is not None
            and self._store_factory is not None
        ):
            return

        _, linked_snapshot_store = self._store_factory(
            port.linked_conversation_id,
        )
        linked_snapshot = await linked_snapshot_store.load(
            port.linked_conversation_id,
        )
        if linked_snapshot is not None:
            wm.history_summary = linked_snapshot.history_summary
            wm.participant_roles_summary = (
                linked_snapshot.participant_roles_summary
            )
            wm.linked_history_tail = [
                stored.restore() for stored in linked_snapshot.history_tail
            ]

    async def on_intervention_signal(
        self,
        agent_guid: UUID,
        signal: InterventionSignal | None,
    ) -> None:
        wm = self._wm
        port = self._port
        assert wm is not None and port is not None

        strategy_id = signal.strategy_id if signal else None
        prompt_fragment = signal.prompt_fragment if signal else None
        needs_history = bool(signal and signal.needs_history)
        needs_web_search = bool(signal and signal.needs_web_search)
        web_search_query = signal.web_search_query if signal else ""

        logger.info(
            "%s intervention decision: strategy_id=%r prompt_fragment=%r "
            "needs_history=%s needs_web_search=%s web_search_query=%r",
            wm.conversation_id,
            strategy_id,
            prompt_fragment,
            needs_history,
            needs_web_search,
            web_search_query,
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

        # "toggle"/"transcribe" guarantee: not a request the model might
        # ignore, same as the vetoes above -- "do nothing" is not a real
        # outcome for an utterance spoken during an explicitly opened
        # listening window, whether the model itself chose nothing or a
        # veto just cleared what it did choose.
        if (
            strategy_id is None
            and self._pending_utterance is not None
            and self._pending_utterance.always_reply
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
            await self._finish_turn(path=path, strategy_id=None)
            return

        if strategy_id == "remember":
            query = (
                extract_query(self._pending_utterance.content)
                if self._pending_utterance is not None
                else ""
            )
            await self._retrieve_similar(wm, port, query)
        elif needs_history or strategy_id == "provide_summary":
            await self._retrieve(wm, port)

        # Text only, for this first cut -- a voice reply saying "let me
        # look that up" before the real answer, then speaking it once this
        # resolves, is a two-stage conversational flow this doesn't
        # attempt yet; a voice utterance just gets a normal reply instead,
        # exactly as if needs_web_search had never been set. Independent
        # of the branch above: a question can need both history and a
        # live lookup at once.
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
            elif modality != "text":
                logger.info(
                    "%s needs_web_search set for a %s turn -- skipping, "
                    "voice isn't wired in yet",
                    wm.conversation_id,
                    modality,
                )
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

        await port.instruct_main_agent(
            self._render_prompt(wm, directive=directive, port=port),
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
        The whole picture for an observer that was never live for any of
        this -- both the compressed summary of everything before the
        current tail, and the tail itself verbatim. Found live: this used
        to send only the raw tail, silently dropping everything a
        management cycle had already compressed away -- a reconnecting
        observer had zero knowledge of anything further back than its
        last ~management_cycle_interval messages, unlike the main agent,
        which always gets both.
        """

        if self._wm is None:
            return ""

        wm = self._wm
        if not wm.history and not wm.history_summary:
            return ""

        summary_part = (
            "Summary of the conversation so far:\n"
            f"{wm.history_summary}\n\n"
            "Participants and roles observed so far:\n"
            f"{wm.participant_roles_summary or '(none yet)'}\n\n"
            if wm.history_summary
            else ""
        )
        tail_part = (
            f"Recent messages, verbatim:\n{wm.transcript()}"
            if wm.history
            else ""
        )

        return (
            "Catching you up on this conversation's history, which you "
            f"were not connected for:\n{summary_part}{tail_part}"
        )

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
        driving cooldowns/the management cycle shouldn't advance for a turn
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

        if (
            self._pending_episodic_record is not None
            and self._episodic_store is not None
        ):
            await self._episodic_store.log(self._pending_episodic_record)
        self._pending_episodic_record = None

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
            # group's own retrieval/management cycle has no path to a DM's
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

        # Every turn, not just at a management cycle: otherwise a group's
        # snapshot (and therefore anything a linked DM could mirror) simply
        # doesn't exist at all until message management_cycle_interval, no
        # matter how recently something relevant was actually said.
        await self._save_snapshot()

        wm.clear_turn_output()

        if message_count % self._management_cycle_interval == 0:
            if port.is_private:
                # No summarization for a DM (its history_summary instead
                # mirrors a linked group's, when it has one --
                # _apply_linked_group_context) -- just bound how much raw
                # history this DM's own WM holds, no LLM call involved.
                wm.trim_history(tail_size=self._management_cycle_interval)
            elif self._summarizer is not None:
                await self._run_management_cycle(wm, port)

        if path == "none":
            # Nothing triggered, so nothing to wait for -- resolves this
            # turn immediately instead of leaving it "in progress" until
            # wait_for_turn_to_finish()'s timeout for no reason.
            self._pending_utterance = None
            self._turn_done.set()

    async def _run_management_cycle(
        self,
        wm: WorkingMemory,
        port: ConversationPort,
    ) -> None:
        assert self._summarizer is not None

        history_summary, roles_summary = (
            await self._summarizer.summarize(
                transcript=wm.transcript(exclude_assistant=True),
                previous_summary=wm.history_summary,
                previous_roles=wm.participant_roles_summary,
            )
        )
        wm.apply_management_cycle(
            history_summary,
            roles_summary,
            tail_size=self._management_cycle_interval,
        )
        await self._save_snapshot()

        if self._episodic_store is not None:
            await self._episodic_store.log(
                EpisodicRecord(
                    timestamp=time.time(),
                    conversation_id=port.conversation_id,
                    intervention_path="management_cycle",
                    trigger_variables=dict(wm.trigger_variables),
                    history_summary=history_summary,
                    participant_roles_summary=roles_summary,
                )
            )

    def _render_prompt(
        self,
        wm: WorkingMemory,
        *,
        directive: str,
        port: ConversationPort,
    ) -> str:
        """
        Assemble this turn's payload for the main agent -- everything
        *except* persona/task, which MainAgentOpenAI sends once at connect
        (session.update replaces rather than merges instructions, so
        resending them here every turn would just be waste, not safety).
        Pushed as a conversation item, not a session-instructions
        overwrite -- see MainAgentOpenAI._trigger_response.
        """

        # A DM mirrors a *different* conversation's summary
        # (_apply_linked_group_context), which this session was never part
        # of and has no other way to learn about -- genuinely foreign
        # content, re-checked against an interval rather than sent once. A
        # group's own summary is different: once sent, the live session
        # already has everything it covers, so it's only sent once, ever
        # -- every turn after that would just be re-telling the model
        # something it was already there for.
        now = time.time()
        if port.is_private:
            needs_full_context = (
                self._full_context_sent_at is None
                or now - self._full_context_sent_at
                > DM_FULL_CONTEXT_INTERVAL_SECONDS
            )
        else:
            needs_full_context = self._full_context_sent_at is None
        if needs_full_context:
            self._full_context_sent_at = now

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
                wm.history[-1].timestamp_ms / 1000, tz=timezone.utc
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
            f"Today's date: {current_date:%A, %Y-%m-%d} (UTC)\n\n"
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

        summary_block = (
            (
                "Summary of the conversation so far:\n"
                f"{wm.history_summary or '(none yet)'}\n\n"
                "Participants and roles observed so far:\n"
                f"{wm.participant_roles_summary or '(none yet)'}\n\n"
            )
            if needs_full_context
            else ""
        )

        # General, not DM-specific: covers what the live session doesn't --
        # e.g. after a restart, wm.history is restored from a snapshot but a
        # freshly-connected session has never actually seen it. Sent once
        # per connection only (not time-gated, unlike summary_block's DM
        # case above): whether the live session has this natively is purely
        # a function of connection lifetime, not wall-clock time elapsed,
        # so re-sending it mid-session on every triggered reply was pure
        # waste -- found live, this was going out on every single triggered
        # reply, not just after a reconnect.
        send_own_tail = not self._own_tail_sent and bool(wm.history)
        if send_own_tail:
            self._own_tail_sent = True

        recent_block = (
            f"Recent messages in this conversation, verbatim:\n{wm.transcript()}\n\n"
            if send_own_tail
            else ""
        )

        # Only ever non-empty for a DM from a recognized participant
        # (CoalaPolicy._apply_linked_group_context) -- the group's own raw
        # tail, covering what its own next management cycle would summarize
        # but hasn't yet, since that group's snapshot may be more current
        # than history_summary above (saved every turn, not just every
        # management_cycle_interval messages). Gated the same as
        # summary_block above: it's the same "foreign content" category,
        # from the same linked group, so it goes stale/gets refreshed on
        # the same schedule.
        linked_tail_block = (
            "Recent messages in the linked group conversation, verbatim:\n"
            + "\n".join(
                f"{utterance.label()}: {utterance.content}"
                for utterance in wm.linked_history_tail
            )
            + "\n\n"
            if needs_full_context and wm.linked_history_tail
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

        directive_label = (
            "High-level guidance from a lightweight judgment step -- this "
            "is the rationale and rough target, not literal wording. Use "
            "your own judgment for exact phrasing, tone, and delivery:"
            if wm.latest_strategy_id is not None
            else "Current directive:"
        )

        return (
            f"{current_time_block}"
            f"{voice_roster_block}"
            f"{summary_block}"
            f"{recent_block}"
            f"{linked_tail_block}"
            f"{retrieved_block}"
            f"{web_search_block}"
            f"{directive_label}\n"
            f"{directive or 'Respond naturally as the ongoing conversation warrants.'}"
        )
