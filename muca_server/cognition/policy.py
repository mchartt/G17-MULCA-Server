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
from collections.abc import Callable
from uuid import UUID

from .forced_intervention import check_forced_intervention
from .memory.in_memory import InMemoryEpisodicStore, InMemorySnapshotStore
from .memory.ports import EpisodicStore, SnapshotStore
from .memory.records import EpisodicRecord, MemorySnapshot, StoredUtterance
from .ports import ConversationPort, InterventionSignal, Summarizer, Utterance
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

# A spoken turn may only ever get a strategy it actually solicited --
# being addressed directly, or asking for a summary -- never an
# unsolicited one like participation_equality or keep_on_topic. Talking
# over a live conversation uninvited is far more intrusive than an
# unsolicited reply reads in text, where it's easy to scan past.
VOICE_ALLOWED_STRATEGY_IDS = frozenset({"respond_when_addressed", "provide_summary"})


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
        management_cycle_interval: int = MANAGEMENT_CYCLE_INTERVAL,
        retrieval_limit: int = RETRIEVAL_LIMIT,
        turn_timeout_seconds: float = TURN_TIMEOUT_SECONDS,
    ) -> None:
        self._strategies = strategy_store
        self._assistant_id = assistant_id
        self._assistant_name = assistant_name
        self._episodic_store = episodic_store
        self._snapshot_store = snapshot_store
        # Deferred store selection: which DB backs this conversation can
        # depend on its conversation_id (e.g. one SQLite file per Discord
        # channel), which isn't known until start() -- policy_factory builds
        # this object before the connect handshake ever runs. Takes
        # precedence over episodic_store/snapshot_store above when given.
        self._store_factory = store_factory
        self._summarizer = summarizer
        self._management_cycle_interval = management_cycle_interval
        self._retrieval_limit = retrieval_limit
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
        # group's own history_summary/participant_roles_summary once since
        # connecting. Group-only: once sent, the live session already has
        # everything that summary covers (it was there when it was said),
        # so resending it every turn is pure waste, not safety -- unlike a
        # DM's linked read of a group it was never part of, which has no
        # other way to get that content and must keep refreshing live.
        # False at construction and never reset mid-session on purpose: a
        # reconnect builds a fresh CoalaPolicy via policy_factory, so a new
        # session starts this at False again without any explicit reset.
        self._group_summary_sent = False

    async def start(self, port: ConversationPort) -> None:
        self._port = port

        if self._store_factory is not None:
            if getattr(port, "is_ephemeral", False):
                # No conversation id from the client: nothing to continue
                # later, so nothing is written to disk for it either.
                self._episodic_store = InMemoryEpisodicStore()
                self._snapshot_store = InMemorySnapshotStore()
            else:
                self._episodic_store, self._snapshot_store = (
                    self._store_factory(port.conversation_id)
                )

        wm = WorkingMemory(
            conversation_id=port.conversation_id,
            assistant_id=self._assistant_id,
        )

        if self._snapshot_store is not None and not port.is_private:
            snapshot = await self._snapshot_store.load(
                port.conversation_id,
            )
            if snapshot is not None:
                wm.history_summary = snapshot.history_summary
                wm.participant_roles_summary = (
                    snapshot.participant_roles_summary
                )
                wm.total_message_count = snapshot.total_message_count
                wm.history = [
                    stored.restore()
                    for stored in snapshot.history_tail
                ]

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
            or self._port.is_private
        ):
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

        participants = port.participants()
        wm.set_trigger_variables(
            compute_trigger_variables(
                wm.history,
                assistant_id=self._assistant_id,
                participants=participants,
            )
        )
        logger.info(
            "%s participants=%s gini_index=%s participant_message_counts=%s",
            wm.conversation_id,
            participants,
            wm.trigger_variables["gini_index"],
            wm.trigger_variables["participant_message_counts"],
        )

        # The observer only ever sees raw utterance text otherwise (routed
        # straight to its session by Conversation) -- a numeric-threshold
        # strategy like participation_equality has no way to judge against a
        # threshold it is never actually told.
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
        # what actually enforces VOICE_ALLOWED_STRATEGY_IDS, since the
        # observer's choice is a request, not a guarantee.
        voice_note = (
            " This turn was spoken, not typed: only "
            f"{sorted(VOICE_ALLOWED_STRATEGY_IDS)} may be selected, "
            "nothing unsolicited."
            if utterance.modality == "audio"
            else ""
        )
        await port.tell_observers(
            "Current trigger variables: "
            f"{json.dumps(wm.trigger_variables)}.{cooldown_note}{voice_note}"
        )

        # A decision -- forced or observer-judged -- is now pending for
        # this exact utterance. Cleared again once that decision's whole
        # cycle (including the assistant's reply, if any) resolves.
        self._pending_utterance = utterance
        self._turn_done.clear()

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

        logger.info(
            "%s intervention decision: strategy_id=%r prompt_fragment=%r "
            "needs_history=%s needs_web_search=%s",
            wm.conversation_id,
            strategy_id,
            prompt_fragment,
            needs_history,
            needs_web_search,
        )

        # Hard veto: not a request the model might ignore, the guarantee.
        if strategy_id is not None:
            strategy = self._strategies.get(strategy_id)
            if strategy is not None and wm.strategy_in_cooldown(strategy):
                logger.info(
                    "%s vetoed %r: still in cooldown",
                    wm.conversation_id,
                    strategy_id,
                )
                strategy_id = None
                prompt_fragment = None
                needs_history = False

        # Hard veto, same guarantee as cooldown: a spoken turn may only
        # ever get a response it actually solicited -- being addressed
        # directly, or asking for a summary -- never an unsolicited one
        # (e.g. participation_equality, keep_on_topic). See the voice_note
        # in on_utterance for why.
        if (
            strategy_id is not None
            and strategy_id not in VOICE_ALLOWED_STRATEGY_IDS
            and self._pending_utterance is not None
            and self._pending_utterance.modality == "audio"
        ):
            logger.info(
                "%s vetoed %r: not allowed for a spoken turn",
                wm.conversation_id,
                strategy_id,
            )
            strategy_id = None
            prompt_fragment = None
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

        if needs_history or strategy_id == "provide_summary":
            await self._retrieve(wm, port)

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
        if self._wm is None or not self._wm.history:
            return ""

        return (
            "Catching you up on this conversation's history, which you "
            "were not connected for:\n" + self._wm.transcript()
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

        # A DM always needs this live -- it's mirroring a *different*
        # conversation's summary (_apply_linked_group_context), which this
        # session was never part of and has no other way to learn about. A
        # group's own summary is different: once sent, the live session
        # already has everything it covers, so it's only sent once, right
        # after connecting -- every turn after that would just be re-telling
        # the model something it was already there for.
        include_summary = port.is_private or not self._group_summary_sent
        if include_summary and not port.is_private:
            self._group_summary_sent = True

        summary_block = (
            (
                "Summary of the conversation so far:\n"
                f"{wm.history_summary or '(none yet)'}\n\n"
                "Participants and roles observed so far:\n"
                f"{wm.participant_roles_summary or '(none yet)'}\n\n"
            )
            if include_summary
            else ""
        )

        # General, not DM-specific: covers what the live session doesn't --
        # e.g. after a restart, wm.history is restored from a snapshot but a
        # freshly-connected session has never actually seen it. For an
        # uninterrupted session this duplicates what the model already has
        # live, which is harmless, just slightly redundant.
        recent_block = (
            f"Recent messages in this conversation, verbatim:\n{wm.transcript()}\n\n"
            if wm.history
            else ""
        )

        # Only ever non-empty for a DM from a recognized participant
        # (CoalaPolicy._apply_linked_group_context) -- the group's own raw
        # tail, covering what its own next management cycle would summarize
        # but hasn't yet, since that group's snapshot may be more current
        # than history_summary above (saved every turn, not just every
        # management_cycle_interval messages).
        linked_tail_block = (
            "Recent messages in the linked group conversation, verbatim:\n"
            + "\n".join(
                f"{utterance.label()}: {utterance.content}"
                for utterance in wm.linked_history_tail
            )
            + "\n\n"
            if wm.linked_history_tail
            else ""
        )

        retrieved_block = (
            f"Retrieved conversation history:\n{wm.retrieved_context}\n\n"
            if wm.retrieved_context
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
            f"{summary_block}"
            f"{recent_block}"
            f"{linked_tail_block}"
            f"{retrieved_block}"
            f"{directive_label}\n"
            f"{directive or 'Respond naturally as the ongoing conversation warrants.'}"
        )
