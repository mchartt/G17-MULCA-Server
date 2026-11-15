"""
M4: /planning, /checkin (findings in code + options), task status, "why did
we decide X?" with source, CS7 blocker cards. No network, model faked.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from muca_server.cognition.coordination.checkin import findings, owners_report
from muca_server.cognition.coordination.models import (
    BlockerPayload,
    CheckinOption,
    Decision,
    Milestone,
    ProjectState,
    ProposalKind,
    ProposalStatus,
    Task,
    TaskStatus,
)
from muca_server.cognition.coordination.plan_writer import PlanWriter
from muca_server.cognition.coordination.plan_writes import add_decision, add_task
from muca_server.cognition.coordination.proactive_writer import ChatLine, ProactiveWriter
from muca_server.cognition.coordination.proposals import ProposalService
from muca_server.cognition.coordination.router import UNAVAILABLE, CoordinatorRouter
from muca_server.cognition.coordination.writer import ProposalWriter
from muca_server.protocol import (
    CoordinatorNoticeMessage,
    CoordinatorProposalMessage,
    CoordinatorStateMessage,
    parse_incoming_message,
)

from .conftest import FakeChatClient, FakeConversation, drain
from .m3_fixtures import ROSTER, context

TODAY = date(2026, 11, 25)


# ----------------------------------------------------------------------
# checkin.py: findings computed in code
# ----------------------------------------------------------------------


def test_findings_list_late_blocked_soon_ownerless_milestone_and_dependency():
    state = ProjectState(
        tasks=[
            Task(task_id="a", title="Server", owner_id=1, due_date=date(2026, 11, 24), status="doing"),
            Task(task_id="b", title="UI", owner_id=2, due_date=date(2026, 11, 26), status="blocked", depends_on=["c"]),
            Task(task_id="c", title="API", owner_id=3, due_date=date(2026, 11, 30)),
            Task(task_id="d", title="Slides"),
            Task(task_id="e", title="Done thing", owner_id=1, due_date=date(2026, 11, 1), status="done"),
        ],
        milestones=[Milestone(milestone_id="m", title="Demo", due_date=date(2026, 11, 27))],
    )
    texts = [f.text for f in findings(state, ROSTER, TODAY)]
    assert texts[0] == "Late: “Server” (Ada) was due 24 Nov."
    assert texts[1] == "Blocked: “UI” (Bruno)."
    assert "Due soon: “UI” (Bruno) is due 26 Nov." in texts
    assert "No owner: “Slides”." in texts
    assert any(t.startswith("Dependency: “UI” waits on “API”") for t in texts)
    assert any(t.startswith("Milestone “Demo” on 27 Nov with") for t in texts)
    assert not any("Done thing" in t for t in texts)
    assert owners_report(state, ROSTER)[0].startswith("Ada: Server (doing, 24 Nov)")


def test_a_healthy_plan_has_no_findings():
    state = ProjectState(tasks=[Task(task_id="a", title="Server", owner_id=1, due_date=date(2026, 12, 20))])
    assert findings(state, ROSTER, TODAY) == []


def test_a_checkin_option_must_change_something():
    with pytest.raises(ValueError):
        CheckinOption(label="Nothing", task_id="a")
    assert CheckinOption(label="Drop", task_id="a", drop=True).drop


# ----------------------------------------------------------------------
# Router + PlanCommands, model faked
# ----------------------------------------------------------------------


def make_router(store, *answers):
    queue: asyncio.Queue = asyncio.Queue()
    base = ProposalWriter(FakeChatClient(*answers))
    router = CoordinatorRouter(
        FakeConversation(), queue, store_for=lambda _: store, writer=base,
        proposal_ttl_seconds=3600, today=lambda: TODAY, proactive_writer=ProactiveWriter(base),
    )
    return router, queue


def command(name, member=1, **args):
    return parse_incoming_message(
        {"event": "coordinator_command", "command": name, "memberId": member, "args": args, "requestId": "r"}
    )


def click(pid, action, member=2, **extra):
    return parse_incoming_message(
        {"event": "proposal_response", "proposalId": pid, "action": action, "memberId": member, "requestId": "c", **extra}
    )


async def run(router, message):
    await router.route_coordinator(message)
    await router.wait_for_background()


PLAN = {
    "items": [
        {"title": "Server", "owner_id": 1, "reason": "Python skills", "due_date": "2026-11-28"},
        {"title": "Slides", "owner_id": None, "reason": "", "due_date": None},
    ],
    "rationale": "Stepping in because you listed the tasks.",
}


async def test_planning_posts_one_card_and_confirm_adds_every_task(store):
    router, queue = make_router(store, PLAN)
    await run(router, command("planning", text="Server\nSlides"))
    out = await drain(queue)
    (card,) = [m.proposal for m in out if isinstance(m, CoordinatorProposalMessage)]
    assert card["kind"] == "plan" and card["payload"]["items"][0]["reason"] == "Python skills"
    assert (await store.load_state()).tasks == []  # nothing before the click
    await run(router, click(card["proposal_id"], "confirm"))
    tasks = sorted((await store.load_state()).tasks, key=lambda t: t.title)
    assert [(t.title, t.owner_id) for t in tasks] == [("Server", 1), ("Slides", None)]


@pytest.mark.parametrize(
    "bad",
    [
        {"items": [{"title": "X", "owner_id": 99, "reason": "", "due_date": None}]},
        {"items": [{"title": "X", "owner_id": None, "reason": "", "due_date": "2026-01-01"}]},
        {"items": []},
    ],
)
async def test_planning_refuses_unknown_owners_past_dates_and_empty_lists(store, bad):
    router, queue = make_router(store, dict(bad, rationale="r"), dict(bad, rationale="r"))
    await run(router, command("planning", text="X"))
    out = await drain(queue)
    assert not [m for m in out if isinstance(m, CoordinatorProposalMessage)]
    assert out[-1].text == UNAVAILABLE


async def test_planning_without_text_costs_nothing(store):
    router, queue = make_router(store)
    await run(router, command("planning", text="  "))
    assert (await drain(queue))[-1].text.startswith("Write the tasks")


async def seed(store, *tasks):
    db = await store._connect()
    try:
        for task in tasks:
            await add_task(db, **task)
    finally:
        await db.close()


async def test_checkin_on_track_answers_without_the_model(store):
    await seed(store, dict(task_id="a", title="Server", owner_id=1, due_date=date(2026, 12, 20)))
    router, queue = make_router(store)  # no scripted answer: a model call would fail
    await run(router, command("checkin"))
    notice = (await drain(queue))[-1]
    assert notice.ok and "on track" in notice.text and "Ada: Server (todo, 20 Dec)" in notice.text


async def test_checkin_with_risks_posts_findings_and_options_and_choose_applies(store):
    await seed(store, dict(task_id="a", title="Server", owner_id=1, due_date=date(2026, 11, 24)))
    options = {
        "options": [
            {"label": "Move Server to 28 Nov", "task_id": "a", "new_due_date": "2026-11-28", "new_owner_id": None, "drop": False},
            {"label": "Bruno helps: he takes Server", "task_id": "a", "new_due_date": None, "new_owner_id": 2, "drop": False},
        ],
        "rationale": "Stepping in because Server is late.",
    }
    router, queue = make_router(store, options)
    await run(router, command("checkin"))
    (card,) = [m.proposal for m in await drain(queue) if isinstance(m, CoordinatorProposalMessage)]
    assert card["payload"]["findings"] == ["Late: “Server” (Ada) was due 24 Nov."]
    await run(router, click(card["proposal_id"], "choose", option=1))
    (task,) = (await store.load_state()).tasks
    assert task.owner_id == 2 and task.due_date == date(2026, 11, 24)


async def test_checkin_options_must_point_at_real_tasks(store):
    await seed(store, dict(task_id="a", title="Server", owner_id=1, due_date=date(2026, 11, 24)))
    bad = {"options": [{"label": "x", "task_id": "zzz", "new_due_date": "2026-11-28", "new_owner_id": None, "drop": False}], "rationale": "r"}
    router, queue = make_router(store, bad, bad)
    await run(router, command("checkin"))
    assert (await drain(queue))[-1].text == UNAVAILABLE


async def test_a_checkin_option_on_a_task_dropped_meanwhile_is_stale(store):
    await seed(store, dict(task_id="a", title="Server", owner_id=1, due_date=None))
    service = ProposalService(store)
    from muca_server.cognition.coordination.models import CheckinPayload

    payload = CheckinPayload(findings=["x"], options=[CheckinOption(label="Drop", task_id="a", drop=True)])
    one = await service.create(ProposalKind.CHECKIN, payload, rationale="r", origin_channel="web")
    two = await service.create(ProposalKind.CHECKIN, payload, rationale="r", origin_channel="web")
    assert (await service.respond(one.proposal_id, action="choose", option=0, member_id=1, participants=ROSTER)).ok
    late = await service.respond(two.proposal_id, action="choose", option=0, member_id=1, participants=ROSTER)
    assert not late.ok and late.proposal.status is ProposalStatus.EXPIRED


async def test_a_member_updates_a_task_by_id_or_by_number(store):
    await seed(store, dict(task_id="a", title="Server", owner_id=1, due_date=None), dict(task_id="b", title="UI", owner_id=2, due_date=None))
    router, queue = make_router(store)
    await run(router, command("task", task="b", status="doing"))
    await run(router, command("task", task="1", status="DONE"))
    await run(router, command("task", task="9", status="done"))
    await run(router, command("task", task="a", status="finished"))
    notices = [m for m in await drain(queue) if isinstance(m, CoordinatorNoticeMessage)]
    assert [n.ok for n in notices] == [True, True, False, False]
    statuses = {t.task_id: t.status for t in (await store.load_state()).tasks}
    assert statuses == {"a": TaskStatus.DONE, "b": TaskStatus.DOING}


async def record_decision(store):
    db = await store._connect()
    try:
        await add_decision(db, Decision(decision_id="d1", question="Which test?", choice="Think-aloud",
                                        reason="Shows why users get stuck.", decided_by=2, decided_at=1_795_000_000,
                                        source="proposal:abc"))
    finally:
        await db.close()


async def test_why_answers_from_the_log_with_its_source(store):
    await record_decision(store)
    router, queue = make_router(store, {"answer": "You chose think-aloud because it shows why users get stuck.", "decision_ids": ["d1"]})
    await run(router, command("why", question="Why did we pick think-aloud?"))
    notice = (await drain(queue))[-1]
    assert notice.ok
    assert notice.text.splitlines()[1].startswith("Source: “Which test?” → Think-aloud (decided by Bruno on ")
    assert notice.text.endswith("card abc).")


async def test_why_never_cites_a_decision_that_does_not_exist(store):
    await record_decision(store)
    bad = {"answer": "Because of the budget.", "decision_ids": ["made-up"]}
    router, queue = make_router(store, bad, bad)
    await run(router, command("why", question="Why?"))
    assert (await drain(queue))[-1].text == UNAVAILABLE


async def test_why_with_an_empty_log_says_so_without_the_model(store):
    router, queue = make_router(store)
    await run(router, command("why", question="Why X?"))
    assert (await drain(queue))[-1].text == "No decision has been recorded yet."


async def test_one_model_command_at_a_time(store):
    gate = asyncio.Event()

    class Slow(FakeChatClient):
        async def _create(self, **kwargs):
            await gate.wait()
            return await super()._create(**kwargs)

    queue: asyncio.Queue = asyncio.Queue()
    base = ProposalWriter(Slow(PLAN))
    router = CoordinatorRouter(FakeConversation(), queue, store_for=lambda _: store, writer=base,
                               proposal_ttl_seconds=3600, today=lambda: TODAY)
    await router.route_coordinator(command("planning", text="Server"))
    await router.route_coordinator(command("checkin"))
    gate.set()
    await router.wait_for_background()
    texts = [m.text for m in await drain(queue) if isinstance(m, CoordinatorNoticeMessage)]
    assert texts[0].startswith("The coordinator is already working")


async def test_task_status_sends_the_new_state(store):
    await seed(store, dict(task_id="a", title="Server", owner_id=1, due_date=None))
    router, queue = make_router(store)
    await run(router, command("task", task="a", status="blocked"))
    assert any(isinstance(m, CoordinatorStateMessage) for m in await drain(queue))


# ----------------------------------------------------------------------
# CS7 blockers
# ----------------------------------------------------------------------

LINES = [ChatLine("Bruno", "I can't start the UI until the API is ready", 1)]
TASKS = [Task(task_id="ui", title="UI", owner_id=2), Task(task_id="api", title="API", owner_id=3)]
BLOCKER = {
    "task_id": "ui", "title": "UI", "depends_on_task_id": "api", "helper_id": 3,
    "question": "What do you need from the API first?", "topic": "UI waits on API",
    "evidence": [1], "rationale": "Stepping in because the UI waits on the API.",
}


async def blocker_draft(*answers):
    writer = ProactiveWriter(ProposalWriter(FakeChatClient(*answers)))
    return await writer.draft("cs7_blockers_dependencies", chat=LINES, members=ROSTER, profiles=[], tasks=TASKS,
                              silenced_topics=[], today=TODAY)


async def test_cs7_draft_points_at_plan_tasks_and_a_member():
    draft = await blocker_draft(BLOCKER)
    assert draft.kind is ProposalKind.BLOCKER and draft.payload.helper_id == 3


@pytest.mark.parametrize("bad", [{"task_id": "nope"}, {"depends_on_task_id": "nope"}, {"helper_id": 42}])
async def test_cs7_draft_with_unknown_ids_is_refused(bad):
    assert await blocker_draft(dict(BLOCKER, **bad), dict(BLOCKER, **bad)) is None


async def test_confirming_a_blocker_marks_the_task_and_records_the_dependency(store):
    await seed(store, dict(task_id="ui", title="UI", owner_id=2, due_date=None), dict(task_id="api", title="API", owner_id=3, due_date=None))
    service = ProposalService(store)
    payload = BlockerPayload(context=context("cs7_blockers_dependencies", "UI waits on API"), task_id="ui",
                             title="UI", depends_on_task_id="api", helper_id=3)
    card = await service.create(ProposalKind.BLOCKER, payload, rationale="r", origin_channel="group")
    assert (await service.respond(card.proposal_id, action="confirm", member_id=2, participants=ROSTER)).ok
    ui = next(t for t in (await store.load_state()).tasks if t.task_id == "ui")
    assert ui.status is TaskStatus.BLOCKED and ui.depends_on == ["api"]


async def test_a_blocker_outside_the_plan_becomes_a_blocked_task(store):
    service = ProposalService(store)
    payload = BlockerPayload(context=context("cs7_blockers_dependencies", "Survey data"), title="Survey analysis")
    card = await service.create(ProposalKind.BLOCKER, payload, rationale="r", origin_channel="group")
    assert (await service.respond(card.proposal_id, action="confirm", member_id=1, participants=ROSTER)).ok
    (task,) = (await store.load_state()).tasks
    assert (task.title, task.status) == ("Survey analysis", TaskStatus.BLOCKED)
    # Not now works on CS7 cards too (they carry a context).
    again = await service.create(ProposalKind.BLOCKER, payload, rationale="r", origin_channel="group")
    assert (await service.respond(again.proposal_id, action="not_now", member_id=1, participants=ROSTER)).ok


async def test_r7_no_cs7_card_for_a_task_already_blocked(store):
    from muca_server.cognition.coordination.proactive import _dropped
    from muca_server.cognition.coordination.proactive_writer import ProactiveDraft

    payload = BlockerPayload(context=context("cs7_blockers_dependencies", "UI"), task_id="ui", title="UI")
    draft = ProactiveDraft(ProposalKind.BLOCKER, payload, "r")
    assert _dropped(draft, [], [Task(task_id="ui", title="UI", status="blocked")]).startswith("R7")
    assert _dropped(draft, [], [Task(task_id="ui", title="UI")]) == ""
