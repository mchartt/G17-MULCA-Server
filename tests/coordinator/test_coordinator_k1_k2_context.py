"""
K1: the intervention agent sees the saved plan and decisions (project memory).
K2: CS7 v2, a possible dependency the group confirms, with ways forward.

No network, model faked. The realistic multi-user conversations for the live
run are in docs/coordinator/CS_CORPUS.md; here we check the contract.
"""

from __future__ import annotations

from datetime import date

import pytest

from muca_server.cognition import InterventionSignal, Utterance
from muca_server.cognition.coordination.models import (
    BlockerCertainty,
    BlockerPayload,
    Charter,
    Decision,
    Milestone,
    ProjectState,
    ProposalKind,
    Task,
    TaskStatus,
)
from muca_server.cognition.coordination.proactive import _dropped
from muca_server.cognition.coordination.proactive_writer import ChatLine, ProactiveDraft, ProactiveWriter
from muca_server.cognition.coordination.project_memory import MAX_TASKS, RESEND_EVERY, project_memory
from muca_server.cognition.coordination.proposals import ProposalService
from muca_server.cognition.coordination.writer import ProposalWriter

from .conftest import FakeChatClient
from .m3_fixtures import ROSTER, context
from .test_coordinator_m3_proactive import FakeWriter, coordinator_policy, make_bridge
from .test_coordinator_m4 import seed

TODAY = date(2026, 11, 19)  # a Thursday
CS7 = "cs7_blockers_dependencies"


# ----------------------------------------------------------------------
# K1 · project_memory(): built in code from what the group saved
# ----------------------------------------------------------------------


def scenario_b_state() -> ProjectState:
    """Scenario B: Sara tests on Friday, Marco's API is due Saturday."""
    return ProjectState(
        charter=Charter(goal="A booking app tested with five users", confirmed_by=1, confirmed_at=0),
        tasks=[
            Task(task_id="test", title="Usability test", owner_id=2, due_date=date(2026, 11, 20)),
            Task(task_id="api", title="Booking API", owner_id=3, due_date=date(2026, 11, 21), status="doing"),
            Task(task_id="old", title="Wireframes", owner_id=1, status="done"),
        ],
        milestones=[Milestone(milestone_id="m", title="Demo", due_date=date(2026, 12, 2))],
        decisions=[
            Decision(decision_id="d1", question="App navigation", choice="Tabs",
                     reason="Five sections fit; testers found the side menu hidden", decided_by=1, decided_at=10),
        ],
    )


def test_memory_lists_open_tasks_owners_dates_milestones_and_decisions():
    text = project_memory(scenario_b_state(), ROSTER, TODAY)
    assert text.startswith("Project memory")
    assert "“Usability test” | Bruno | due Fri 20 Nov | todo" in text
    assert "“Booking API” | Chiara | due Sat 21 Nov | doing" in text
    assert "Wireframes" not in text  # done work is not context for a blocker
    assert "“Demo” on Wed 2 Dec" in text
    assert "App navigation → chose “Tabs” because Five sections fit" in text
    assert "Goal: A booking app" in text


def test_memory_shows_recorded_dependencies_with_their_state():
    state = scenario_b_state()
    state.tasks[0].depends_on.append("api")
    assert "waits on “Booking API” (doing, due Sat 21 Nov)" in project_memory(state, ROSTER, TODAY)


def test_nothing_saved_means_no_memory():
    assert project_memory(ProjectState(), ROSTER, TODAY) == ""


def test_memory_is_capped_and_keeps_blocked_and_urgent_first():
    tasks = [Task(task_id=f"t{i}", title=f"Task {i}", due_date=date(2026, 12, 1 + i % 20)) for i in range(30)]
    tasks.append(Task(task_id="b", title="Stuck one", status="blocked"))
    text = project_memory(ProjectState(tasks=tasks), ROSTER, TODAY)
    task_lines = [line for line in text.splitlines() if line.startswith("- “")]
    assert len(task_lines) == MAX_TASKS and task_lines[0].startswith("- “Stuck one”")
    assert f"and {31 - MAX_TASKS} more" in text


# ----------------------------------------------------------------------
# K1 · the bridge sends it when it changes; the policy appends it
# ----------------------------------------------------------------------


async def save_scenario(store):
    await seed(
        store,
        dict(task_id="test", title="Usability test", owner_id=2, due_date=date(2026, 11, 20)),
        dict(task_id="api", title="Booking API", owner_id=3, due_date=date(2026, 11, 21)),
    )


async def test_bridge_sends_the_memory_once_then_on_change_or_after_a_while(store):
    await save_scenario(store)
    bridge, _, _ = make_bridge(store, FakeWriter())
    first = await bridge.observer_note(1)
    assert "Usability test" in first
    assert await bridge.observer_note(2) == ""  # unchanged: the observer already has it
    await seed(store, dict(task_id="slides", title="Slides", owner_id=None, due_date=None))
    assert "Slides" in await bridge.observer_note(3)  # changed: sent again
    assert await bridge.observer_note(4) == ""
    assert "Slides" in await bridge.observer_note(3 + RESEND_EVERY)  # not lost when context is trimmed


async def test_bridge_with_an_empty_plan_says_nothing(store):
    bridge, _, _ = make_bridge(store, FakeWriter())
    assert await bridge.observer_note(1) == ""


async def test_bridge_database_error_is_silence_not_a_crash():
    class Broken:
        async def load_state(self):
            raise OSError("disk")

    bridge, _, _ = make_bridge(Broken(), FakeWriter())
    assert await bridge.observer_note(1) == ""


class NoteBridge:
    def __init__(self, note):
        self.note = note
        self.counts = []

    async def offer(self, strategy_id, **kwargs):
        raise AssertionError("not judged in these tests")

    async def observer_note(self, message_count):
        self.counts.append(message_count)
        if isinstance(self.note, Exception):
            raise self.note
        return self.note


def utter(text):
    return Utterance(speaker_id=1, speaker_name="Ada", content=text, timestamp_ms=0)


async def test_policy_appends_the_memory_to_the_observers_turn_update():
    policy, port = coordinator_policy()
    bridge = NoteBridge("Project memory (…). Open tasks: …")
    policy.set_proactive_bridge(bridge)
    await policy.start(port)
    await policy.on_utterance(utter("I'll run the tests on Friday"))
    (update,) = port.observer_messages
    assert update.startswith("Current trigger variables: ")
    assert update.endswith("\nProject memory (…). Open tasks: …")
    assert bridge.counts == [1]


@pytest.mark.parametrize("bridge", [None, NoteBridge(""), NoteBridge(RuntimeError("boom"))])
async def test_policy_turn_update_is_the_labs_when_off_unchanged_or_failing(bridge):
    policy, port = coordinator_policy()
    reference, reference_port = coordinator_policy()
    if bridge is not None:
        policy.set_proactive_bridge(bridge)
    for p, pt in ((policy, port), (reference, reference_port)):
        await p.start(pt)
        await p.on_utterance(utter("hello"))
    assert port.observer_messages == reference_port.observer_messages
    assert "Project memory" not in port.observer_messages[0]


# ----------------------------------------------------------------------
# K2 · CS7 v2 drafts
# ----------------------------------------------------------------------

CHAT_B = [
    ChatLine("Bruno", "I booked five testers for Friday at 10", 1),
    ChatLine("Chiara", "booking flow still crashes on submit, fixing it", 2),
    ChatLine("Ada", "nice, the icons are done", 3),
]
TASKS_B = [
    Task(task_id="test", title="Usability test", owner_id=2, due_date=date(2026, 11, 20)),
    Task(task_id="api", title="Booking API", owner_id=3, due_date=date(2026, 11, 21), status="doing"),
]
POSSIBLE = {
    "certainty": "possible",
    "task_id": "test", "title": "Usability test",
    "depends_on_task_id": "api", "depends_on_title": "", "helper_id": 3,
    "question": "Is the booking API needed for Friday's test, or could Bruno use mock data?",
    "ways_forward": ["Test with mock data", "Chiara ships a minimal booking flow first", ""],
    "topic": "Test waits on booking API", "evidence": [1, 2],
    "rationale": "Stepping in because Friday's test may need the booking API, due Saturday.",
}


async def draft_cs7(*answers, tasks=TASKS_B):
    writer = ProactiveWriter(ProposalWriter(FakeChatClient(*answers)))
    return await writer.draft(CS7, chat=CHAT_B, members=ROSTER, profiles=[], tasks=tasks,
                              silenced_topics=[], today=TODAY)


async def test_a_possible_dependency_is_drafted_with_its_evidence_and_ways_forward():
    draft = await draft_cs7(POSSIBLE)
    payload = draft.payload
    assert payload.certainty is BlockerCertainty.POSSIBLE
    assert payload.depends_on_title == "Booking API"  # the plan's title, for every channel
    assert payload.ways_forward == ["Test with mock data", "Chiara ships a minimal booking flow first"]
    assert [e.text for e in payload.context.evidence] == [CHAT_B[0].text, CHAT_B[1].text]


async def test_the_writer_sees_recorded_dependencies_in_the_plan():
    client = FakeChatClient(POSSIBLE)
    writer = ProactiveWriter(ProposalWriter(client))
    tasks = [TASKS_B[0].model_copy(update={"depends_on": ["api"]}), TASKS_B[1]]
    await writer.draft(CS7, chat=CHAT_B, members=ROSTER, profiles=[], tasks=tasks, silenced_topics=[], today=TODAY)
    assert "waits on: api" in client.calls[0]["messages"][-1]["content"]


async def test_a_possible_dependency_on_work_not_in_the_plan_names_it():
    answer = dict(POSSIBLE, depends_on_task_id=None, depends_on_title="Booking flow fix")
    draft = await draft_cs7(answer)
    assert draft.payload.depends_on_title == "Booking flow fix"


@pytest.mark.parametrize(
    "bad",
    [
        {"depends_on_task_id": None, "depends_on_title": ""},  # possible, but on what?
        {"depends_on_task_id": "test"},  # waits on itself
        {"ways_forward": ["a", "b", "c", "d"]},  # more than three
        {"ways_forward": ["x" * 200]},  # not short
        {"certainty": "sure"},
    ],
)
async def test_an_ungrounded_cs7_draft_is_refused(bad):
    answer = dict(POSSIBLE, **bad)
    assert await draft_cs7(answer, answer) is None


# CS7-W1 (27/11): the writer can find no dependency after all.
NO_DEPENDENCY_ANSWER = {
    "certainty": "none", "task_id": None, "title": "", "depends_on_task_id": None, "depends_on_title": "",
    "depends_on_owner_id": None, "helper_id": None, "question": "", "ways_forward": [],
    "topic": "", "evidence": [], "rationale": "",
}


async def test_the_writer_may_find_no_dependency_and_then_drafts_nothing_in_one_call():
    client = FakeChatClient(NO_DEPENDENCY_ANSWER, POSSIBLE)  # the second answer must never be asked for
    writer = ProactiveWriter(ProposalWriter(client))
    draft = await writer.draft(CS7, chat=CHAT_B, members=ROSTER, profiles=[], tasks=TASKS_B,
                               silenced_topics=[], today=TODAY)
    assert draft is None
    assert len(client.calls) == 1  # a clean "no card", not a retry
    call = client.calls[0]
    assert call["response_format"]["json_schema"]["schema"]["properties"]["certainty"]["enum"] == ["blocked", "possible", "none"]
    assert 'certainty is "none"' in call["messages"][0]["content"]


async def test_none_is_only_an_exit_for_blocker_cards():
    # Another strategy's schema never offers it, and a card never carries it.
    from muca_server.cognition.coordination.proactive_writer import SCHEMAS

    for kind, schema in SCHEMAS.items():
        if kind is not ProposalKind.BLOCKER:
            assert "certainty" not in schema["schema"]["properties"]
    with pytest.raises(ValueError):
        possible_payload(certainty="none")


async def test_a_fired_cs7_with_no_dependency_posts_nothing_saves_nothing_and_keeps_the_budget(store):
    await save_scenario(store)
    before = await store.load_state()
    writer = ProactiveWriter(ProposalWriter(FakeChatClient(NO_DEPENDENCY_ANSWER)))
    bridge, sent, _ = make_bridge(store, writer)
    history = [Utterance(speaker_id=1, speaker_name="Ada", content="x", timestamp_ms=0)] * 3
    verdict = await bridge.offer(CS7, history=history, message_count=20, assistant_id=0)
    await bridge.wait()
    assert verdict == "taken" and sent == []
    assert await store.open_proposals(0) == []
    assert (await store.load_state()).tasks == before.tasks
    assert await store.mark("proactive") is None  # R3/R4 count only cards posted


async def test_an_old_style_answer_is_a_stated_blocker():
    old = {k: v for k, v in POSSIBLE.items() if k not in ("certainty", "ways_forward", "depends_on_title")}
    assert (await draft_cs7(old)).payload.certainty is BlockerCertainty.BLOCKED


# ----------------------------------------------------------------------
# K2 · confirming, and R7
# ----------------------------------------------------------------------


def possible_payload(**changes) -> BlockerPayload:
    fields = dict(context=context(CS7, "Test waits on API"), task_id="test", title="Usability test",
                  depends_on_task_id="api", helper_id=3, certainty="possible")
    fields.update(changes)
    return BlockerPayload(**fields)


async def test_confirming_a_possible_dependency_records_it_without_blocking(store):
    await save_scenario(store)
    service = ProposalService(store)
    card = await service.create(ProposalKind.BLOCKER, possible_payload(), rationale="r", origin_channel="group")
    result = await service.respond(card.proposal_id, action="confirm", member_id=2, participants=ROSTER)
    assert result.ok and "Dependency saved" in result.message
    test = next(t for t in (await store.load_state()).tasks if t.task_id == "test")
    assert test.status is TaskStatus.TODO and test.depends_on == ["api"]


async def test_confirming_a_dependency_on_unplanned_work_adds_that_work_without_owner(store):
    await save_scenario(store)
    service = ProposalService(store)
    payload = possible_payload(depends_on_task_id=None, depends_on_title="Booking flow fix")
    card = await service.create(ProposalKind.BLOCKER, payload, rationale="r", origin_channel="group")
    result = await service.respond(card.proposal_id, action="confirm", member_id=2, participants=ROSTER)
    assert result.ok and "“Booking flow fix”" in result.message
    tasks = {t.title: t for t in (await store.load_state()).tasks}
    assert tasks["Booking flow fix"].owner_id is None  # never assigned behind the group's back
    assert tasks["Usability test"].depends_on == [tasks["Booking flow fix"].task_id]


async def test_rejecting_a_possible_dependency_changes_nothing(store):
    await save_scenario(store)
    before = await store.load_state()
    service = ProposalService(store)
    card = await service.create(ProposalKind.BLOCKER, possible_payload(), rationale="r", origin_channel="group")
    assert (await service.respond(card.proposal_id, action="reject", member_id=2, participants=ROSTER)).ok
    assert (await store.load_state()).tasks == before.tasks


def test_r7_no_card_for_a_dependency_the_plan_already_records():
    draft = ProactiveDraft(ProposalKind.BLOCKER, possible_payload(), "r")
    known = [Task(task_id="test", title="Usability test", depends_on=["api"]), Task(task_id="api", title="API")]
    assert _dropped(draft, [], known) == "R7 the plan already records this dependency"
    assert _dropped(draft, [], [Task(task_id="test", title="Usability test"), Task(task_id="api", title="API")]) == ""


def test_r7_a_possible_dependency_on_a_blocked_task_is_still_useful():
    # Already blocked, but on something else: saying what it waits on helps.
    draft = ProactiveDraft(ProposalKind.BLOCKER, possible_payload(), "r")
    assert _dropped(draft, [], [Task(task_id="test", title="Usability test", status="blocked")]) == ""


def test_a_possible_dependency_must_say_on_what():
    with pytest.raises(ValueError):
        possible_payload(depends_on_task_id=None)


# ----------------------------------------------------------------------
# R9 · no second card while the first one is still open
# ----------------------------------------------------------------------


async def test_r8_no_duplicate_card_while_the_first_waits(store):
    from muca_server.cognition.coordination.proactive import MIN_MESSAGES_BETWEEN, MIN_SECONDS_BETWEEN

    first = ProactiveDraft(ProposalKind.BLOCKER, possible_payload(), "Stepping in because r.")
    same = ProactiveDraft(ProposalKind.BLOCKER, possible_payload(), "Stepping in because r.")
    other = ProactiveDraft(
        ProposalKind.BLOCKER,
        possible_payload(context=context(CS7, "Slides wait on data"), task_id=None, title="Slides",
                         depends_on_task_id=None, depends_on_title="Survey data"),
        "Stepping in because r.",
    )
    bridge, sent, clock = make_bridge(store, FakeWriter(first, same, other))
    history = [Utterance(speaker_id=1, speaker_name="Ada", content="x", timestamp_ms=0)] * 3
    for count in (20, 20 + MIN_MESSAGES_BETWEEN, 20 + 2 * MIN_MESSAGES_BETWEEN):
        await bridge.offer(CS7, history=history, message_count=count, assistant_id=0)
        await bridge.wait()
        clock.now += MIN_SECONDS_BETWEEN + 1
    assert [p.payload["title"] for p in sent] == ["Usability test", "Slides"]
