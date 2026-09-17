"""
The cognition pipeline: when to speak, what to say, and what to remember.

Ported from MULCA's Orchestrator, split across the two hooks a conversation
calls: on_utterance runs while a message arrives, on_intervention_signal runs
once an observer has judged whether to act.
"""

from __future__ import annotations

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

        self._port: ConversationPort | None = None
        self._wm: WorkingMemory | None = None

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
        Mirrors a linked group's summary directly into this DM's own
        history_summary/participant_roles_summary -- reusing exactly the
        fields (and _render_prompt's existing summary_block) a group already
        uses for its own memory, rather than a separate DM-only concept.
        This DM's own raw conversation still reaches the main/intervention
        agents the normal way (Conversation forwards each utterance to their
        live sessions directly) -- this only supplies what a DM's session
        cannot otherwise see at all: the group's memory, since it lives in a
        different session entirely.

        Re-read on every message, not cached: the token cost of including
        this is the same either way, since the main LLM rebuilds its full
        prompt fresh every turn regardless (BLUEPRINT.md's "stateless main
        LLM") -- caching would only reduce DB reads, at the cost of
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
        if wm is None or not text:
            return

        wm.append_history(
            Utterance(
                speaker_id=self._assistant_id,
                speaker_name=self._assistant_name,
                content=text,
                timestamp_ms=int(time.time() * 1000),
            )
        )

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

        latest = wm.history[-1] if wm.history else None
        message_count = wm.total_message_count

        if not port.is_private and self._episodic_store is not None:
            await self._episodic_store.log(
                EpisodicRecord(
                    timestamp=time.time(),
                    conversation_id=port.conversation_id,
                    intervention_path=path,
                    speaker=latest.label() if latest else "",
                    content=latest.content if latest else "",
                    trigger_variables=dict(wm.trigger_variables),
                    strategy_id=strategy_id,
                    prompt_fragment=prompt_fragment,
                )
            )

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

    async def _run_management_cycle(
        self,
        wm: WorkingMemory,
        port: ConversationPort,
    ) -> None:
        assert self._summarizer is not None

        history_summary, roles_summary = (
            await self._summarizer.summarize(
                transcript=wm.transcript(),
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
            f"{retrieved_block}"
            f"{directive_label}\n"
            f"{directive or 'Respond naturally as the ongoing conversation warrants.'}"
        )
