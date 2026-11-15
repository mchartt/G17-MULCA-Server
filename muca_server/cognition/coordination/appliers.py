"""
Appliers: the change each kind of proposal makes once a member clicks.

An applier is a small async function that receives the open database
connection and writes the change inside CoordinationStore.resolve_proposal's
transaction (store.py). If it raises StaleProposal the whole transaction is
rolled back: the state moved on since the card was made, so the card must
not apply (proposals.py then closes it as expired).

One builder per kind (Strategy-like table in proposals.py chooses it):

    charter_applier      CHARTER     replace charter + milestones
    task_applier         TASK_OWNER  give an existing task its owner, or add the task
                         DELEGATION  add the task with the member who took it
    decision_applier     DECISION    write the chosen option to the decision log
    snooze_applier       any proactive card, "Not now"
    plan_applier         PLAN        add every task of the /planning list
    checkin_applier      CHECKIN     change one task (date, owner) or drop it
    blocker_applier      BLOCKER     mark a task blocked, record what it waits on
"""

from __future__ import annotations

from .models import (
    BlockerPayload,
    Charter,
    CharterPayload,
    CheckinOption,
    Decision,
    DecisionPayload,
    Milestone,
    PlanPayload,
    TaskAssignmentPayload,
    TaskStatus,
)
from .plan_writes import (
    add_decision,
    add_dependency,
    add_snooze,
    add_task,
    assign_task,
    delete_task,
    task_owner,
    update_task,
)
from .store import Applier, CoordinationStore


class StaleProposal(Exception):
    """Raised inside the transaction: the state moved on since the card was made."""


def charter_applier(
    payload: CharterPayload,
    *,
    member_id: int,
    now: float,
    created_at: float,
    new_id,
) -> Applier:
    """
    Charter + milestones replaced. Refused (StaleProposal) if another charter
    was confirmed after this card was made: an old card must never overwrite
    a newer charter.
    """
    charter = Charter(
        goal=payload.goal,
        deliverables=payload.deliverables,
        confirmed_by=member_id,
        confirmed_at=now,
    )
    milestones = [
        Milestone(milestone_id=new_id(), title=m.title, due_date=m.due_date)
        for m in payload.milestones
    ]

    async def apply(db) -> None:
        confirmed_at = await CoordinationStore.charter_confirmed_at(db)
        if confirmed_at is not None and confirmed_at > created_at:
            raise StaleProposal("A newer charter was confirmed after this card was made.")
        await CoordinationStore.write_charter(db, charter, milestones)

    return apply


def task_applier(payload: TaskAssignmentPayload, *, owner_id: int, new_task_id: str) -> Applier:
    """
    Existing task (CS5 on a task of the plan): it must still exist and still
    have no owner, checked inside the transaction (someone may have taken it
    on another channel meanwhile). New task: added with its owner and date.
    """

    async def apply(db) -> None:
        if payload.task_id is None:
            await add_task(
                db,
                task_id=new_task_id,
                title=payload.title,
                owner_id=owner_id,
                due_date=payload.due_date,
            )
            return
        exists, current_owner = await task_owner(db, payload.task_id)
        if not exists:
            raise StaleProposal("This task is no longer in the plan.")
        if current_owner is not None:
            raise StaleProposal("This task already has an owner.")
        await assign_task(db, task_id=payload.task_id, owner_id=owner_id, due_date=payload.due_date)

    return apply


def decision_applier(
    payload: DecisionPayload,
    *,
    option: int,
    member_id: int,
    now: float,
    decision_id: str,
    proposal_id: str,
) -> Applier:
    """
    The chosen option goes to the decision log. The reason is the argument
    the chat gave for it (copied on the card, visible before the click), and
    the source points back at this card, so "why did we decide X?" (M4) can
    answer with where the decision came from.
    """
    chosen = payload.options[option]
    decision = Decision(
        decision_id=decision_id,
        question=payload.question,
        choice=chosen.label,
        reason=chosen.argument,
        decided_by=member_id,
        decided_at=now,
        source=f"proposal:{proposal_id}",
    )

    async def apply(db) -> None:
        await add_decision(db, decision)

    return apply


def snooze_applier(*, strategy_id: str, topic: str, until: float) -> Applier:
    """"Not now": that strategy stays silent on that topic until `until`."""

    async def apply(db) -> None:
        await add_snooze(db, strategy_id=strategy_id, topic=topic, until=until)

    return apply


# ----------------------------------------------------------------------
# M4
# ----------------------------------------------------------------------


def plan_applier(payload: PlanPayload, *, new_id) -> Applier:
    """/planning confirmed: every task of the list enters the plan."""

    async def apply(db) -> None:
        for item in payload.items:
            await add_task(db, task_id=new_id(), title=item.title, owner_id=item.owner_id, due_date=item.due_date)

    return apply


def checkin_applier(option: CheckinOption) -> Applier:
    """The check-in option the group chose: change one task, or drop it."""

    async def apply(db) -> None:
        exists, _ = await task_owner(db, option.task_id)
        if not exists:
            raise StaleProposal("This task is no longer in the plan.")
        if option.drop:
            await delete_task(db, option.task_id)
            return
        await update_task(db, task_id=option.task_id, due_date=option.new_due_date, owner_id=option.new_owner_id)

    return apply


def blocker_applier(payload: BlockerPayload, *, new_task_id: str) -> Applier:
    """
    CS7 confirmed: the task is marked blocked (added if the plan did not have
    it) and, if the card names one, the task it waits on is recorded.
    """

    async def apply(db) -> None:
        task_id = payload.task_id
        if task_id is None:
            task_id = new_task_id
            await add_task(db, task_id=task_id, title=payload.title, owner_id=None, due_date=None)
        else:
            exists, _ = await task_owner(db, task_id)
            if not exists:
                raise StaleProposal("This task is no longer in the plan.")
        await update_task(db, task_id=task_id, status=TaskStatus.BLOCKED)
        if payload.depends_on_task_id:
            exists, _ = await task_owner(db, payload.depends_on_task_id)
            if exists:
                await add_dependency(db, task_id=task_id, depends_on=payload.depends_on_task_id)

    return apply
