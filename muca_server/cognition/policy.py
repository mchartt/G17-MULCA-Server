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
from .memory.ports import EpisodicStore, SnapshotStore
from .memory.records import EpisodicRecord, MemorySnapshot, StoredUtterance
from .ports import ConversationPort, InterventionSignal, Summarizer, Utterance
from .strategies import StrategyStore
from .trigger_variables import compute_trigger_variables
from .working_memory import WorkingMemory

logger = logging.getLogger(__name__)

RETRIEVAL_LIMIT = 50
MANAGEMENT_CYCLE_INTERVAL = 8
TURN_TIMEOUT_SECONDS = 30.0


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
        main_persona: str,
        main_task: str,
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
        self._main_persona = main_persona
        self._main_task = main_task
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

    async def start(self, port: ConversationPort) -> None:
        self._port = port

        if self._store_factory is not None:
            self._episodic_store, self._snapshot_store = (
                self._store_factory(port.conversation_id)
            )

        wm = WorkingMemory(
            conversation_id=port.conversation_id,
            assistant_id=self._assistant_id,
            main_persona=self._main_persona,
            main_task_knowledge=self._main_task,
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
        # TODO: remove once live-tested (mirrors the old MULCA prototype's
        # orchestrator.py debug print of roster + trigger_variables).
        print(
            f"[MUCA debug] {wm.conversation_id} participants={participants} "
            f"gini_index={wm.trigger_variables['gini_index']} "
            f"participant_message_counts={wm.trigger_variables['participant_message_counts']}"
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
        await port.tell_observers(
            "Current trigger variables: "
            f"{json.dumps(wm.trigger_variables)}.{cooldown_note}"
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

        Re-read on every message, not cached: the token cost of including
        this is the same either way, since the main LLM rebuilds its full
        prompt fresh every turn regardless (the "stateless main LLM"
        design) -- caching would only reduce DB reads, at the cost of
        staleness, which matters for "ask individual questions whenever you
        want" over a long-running deployment. This never writes back to the
        linked conversation either way.

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

        # print(), not logger.debug(): nothing in this project configures a
        # logging level yet, so debug/info calls are otherwise invisible.
        print(
            f"[MUCA debug] {wm.conversation_id} intervention decision: "
            f"strategy_id={strategy_id!r} prompt_fragment={prompt_fragment!r} "
            f"needs_history={needs_history} needs_web_search={needs_web_search}"
        )

        # Hard veto: not a request the model might ignore, the guarantee.
        if strategy_id is not None:
            strategy = self._strategies.get(strategy_id)
            if strategy is not None and wm.strategy_in_cooldown(strategy):
                print(
                    f"[MUCA debug] {wm.conversation_id} vetoed "
                    f"{strategy_id!r}: still in cooldown"
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

        if (
            needs_history or strategy_id == "provide_summary"
        ) and self._episodic_store is not None:
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
            self._render_prompt(wm, directive=directive),
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
        assert self._episodic_store is not None

        records = await self._episodic_store.recall_recent(
            port.conversation_id,
            self._retrieval_limit,
        )

        lines = [
            f"{record.speaker}: {record.content}"
            + (
                f"\nMULCA: {record.assistant_output}"
                if record.assistant_output
                else ""
            )
            for record in records
            if record.content
        ]

        wm.set_retrieved_context(
            "\n".join(lines) or "(no prior conversation found)",
        )

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

        if not port.is_private and self._episodic_store is not None:
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
    ) -> str:
        """
        Assemble the main agent's full instructions for this turn.

        Ported from MULCA's `MainLLM` prompt assembly, moved here because it
        needs working memory the agent itself does not hold.
        """

        summary_block = (
            "Summary of the conversation so far:\n"
            f"{wm.history_summary or '(none yet)'}\n\n"
            "Participants and roles observed so far:\n"
            f"{wm.participant_roles_summary or '(none yet)'}\n\n"
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
            f"{wm.main_persona}\n\n"
            f"Task context:\n{wm.main_task_knowledge}\n\n"
            f"{summary_block}"
            f"{recent_block}"
            f"{linked_tail_block}"
            f"{retrieved_block}"
            f"{directive_label}\n"
            f"{directive or 'Respond naturally as the ongoing conversation warrants.'}"
        )
