"""CoordinationStore: tables, members, proposals, atomic resolve, shared file."""

from __future__ import annotations

import asyncio

import pytest

from muca_server.cognition.coordination.models import (
    Charter,
    MemberProfile,
    Milestone,
    Proposal,
    ProposalKind,
    ProposalStatus,
)
from muca_server.cognition.coordination.store import CoordinationStore
from muca_server.cognition.memory.records import EpisodicRecord
from muca_server.cognition.memory.sqlite import SqliteEpisodicStore


def a_proposal(proposal_id: str = "p1") -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        kind=ProposalKind.CHARTER,
        payload={"goal": "g", "deliverables": [], "milestones": []},
        rationale="Stepping in because test.",
        created_at=100.0,
        expires_at=200.0,
    )


async def test_an_empty_store_has_an_empty_state(store):
    state = await store.load_state()
    assert state.charter is None
    assert state.members == state.milestones == state.tasks == state.decisions == []


async def test_a_member_profile_is_saved_and_updated(store):
    await store.upsert_member(MemberProfile(member_id=1, display_name="Ada", skills=["react"]))
    await store.upsert_member(
        MemberProfile(member_id=1, display_name="Ada L.", discipline="CS", weekly_hours=6)
    )
    (member,) = (await store.load_state()).members
    assert member.display_name == "Ada L."
    assert member.discipline == "CS"
    assert member.skills == []
    assert member.weekly_hours == 6


async def test_a_proposal_round_trips(store):
    await store.add_proposal(a_proposal())
    loaded = await store.get_proposal("p1")
    assert loaded == a_proposal()
    assert await store.get_proposal("missing") is None


async def test_resolve_applies_once_and_only_while_pending(store):
    await store.add_proposal(a_proposal())
    calls = []

    async def apply(db):
        calls.append(db)

    assert await store.resolve_proposal(
        "p1", ProposalStatus.APPLIED, member_id=1, now=150.0, apply=apply
    )
    # A second click finds it no longer pending: nothing happens.
    assert not await store.resolve_proposal(
        "p1", ProposalStatus.REJECTED, member_id=2, now=151.0, apply=apply
    )
    assert len(calls) == 1
    proposal = await store.get_proposal("p1")
    assert (proposal.status, proposal.resolved_by, proposal.resolved_at) == (
        ProposalStatus.APPLIED,
        1,
        150.0,
    )


async def test_two_simultaneous_clicks_apply_exactly_once(store):
    await store.add_proposal(a_proposal())
    results = await asyncio.gather(
        *(
            store.resolve_proposal("p1", ProposalStatus.APPLIED, member_id=m, now=150.0)
            for m in (1, 2, 3)
        )
    )
    assert sorted(results) == [False, False, True]


async def test_a_failing_change_leaves_the_proposal_pending(store):
    await store.add_proposal(a_proposal())

    async def broken(db):
        await CoordinationStore.write_charter(
            db, Charter(goal="half", confirmed_by=1, confirmed_at=1.0), []
        )
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        await store.resolve_proposal(
            "p1", ProposalStatus.APPLIED, member_id=1, now=150.0, apply=broken
        )
    assert (await store.get_proposal("p1")).status is ProposalStatus.PENDING
    assert (await store.load_state()).charter is None  # nothing half-written


async def test_write_charter_replaces_charter_and_milestones(store):
    await store.add_proposal(a_proposal("p1"))
    await store.add_proposal(a_proposal("p2"))

    def writer(goal, titles):
        async def apply(db):
            await CoordinationStore.write_charter(
                db,
                Charter(goal=goal, deliverables=["Report"], confirmed_by=1, confirmed_at=5.0),
                [
                    Milestone(milestone_id=f"m{i}", title=t, due_date="2026-12-02")
                    for i, t in enumerate(titles)
                ],
            )

        return apply

    await store.resolve_proposal("p1", ProposalStatus.APPLIED, member_id=1, now=1.0, apply=writer("old", ["A", "B"]))
    await store.resolve_proposal("p2", ProposalStatus.APPLIED, member_id=1, now=2.0, apply=writer("new", ["C"]))
    state = await store.load_state()
    assert state.charter.goal == "new"
    assert state.charter.deliverables == ["Report"]
    assert [m.title for m in state.milestones] == ["C"]
    assert state.milestones[0].due_date.isoformat() == "2026-12-02"


async def test_tasks_dependencies_and_decisions_are_read_back(store):
    await store.load_state()  # creates the tables
    import aiosqlite

    async with aiosqlite.connect(store._db_path) as db:
        await db.execute("INSERT INTO coord_task (task_id, title, owner_id, status) VALUES ('t1','Mockup',1,'doing')")
        await db.execute("INSERT INTO coord_task (task_id, title) VALUES ('t2','Tests')")
        await db.execute("INSERT INTO coord_task_dependency VALUES ('t2','t1')")
        await db.execute(
            "INSERT INTO coord_decision VALUES ('d1','Framework?','React','team knows it',2,9.0,'proposal:p1')"
        )
        await db.commit()
    state = await store.load_state()
    assert [(t.task_id, t.owner_id, t.status.value, t.depends_on) for t in state.tasks] == [
        ("t1", 1, "doing", []),
        ("t2", None, "todo", ["t1"]),
    ]
    assert state.decisions[0].choice == "React"
    assert state.decisions[0].source == "proposal:p1"


async def test_shares_the_conversation_file_with_the_episodic_store(tmp_path):
    """Assumption A4: our tables in the lab's SQLite file disturb nothing."""
    path = tmp_path / "channel_42.db"
    episodic = SqliteEpisodicStore(path)
    coordination = CoordinationStore(path)
    await coordination.upsert_member(MemberProfile(member_id=1, display_name="Ada"))
    await episodic.log(
        EpisodicRecord(
            timestamp=1.0,
            conversation_id="channel:42",
            intervention_path="none",
            speaker="Ada",
            content="hello",
        )
    )
    assert (await coordination.load_state()).members[0].display_name == "Ada"
    rows = await episodic.recall_recent("channel:42", limit=5)
    assert [r.content for r in rows] == ["hello"]
