"""
CS6 progress check-ins (progress.py) and the conversational stability fixes
of 20/11 (R10, casual talk, no false "no access"), offline with a simulated
clock. Scenario ids T1-T16 follow Matteo's brief of 20/11.

These prove the decision logic and the plumbing (what is asked, when, how
often, to whom). They do NOT prove the wording or the model's reading of a
reply: that is the live corpus (scripts/live_conversation_corpus.py).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from muca_server.cognition.coordination.models import ProjectState, Task, TaskStatus
from muca_server.cognition.coordination.progress import (
    ASSESS_HOURS,
    SPACING_SECONDS,
    ProgressMonitor,
    candidates,
    deadline,
    pick,
    quiet,
)
from muca_server.cognition.coordination.proactive import ProactiveBridge, Verdict
from muca_server.protocol import CoordinatorNoticeMessage

from .conftest import FakeConversation, drain
from .test_coordinator_links import WEB, Group, command, open_web_space

ROME = ZoneInfo("Europe/Rome")
BRUNO, MATIAS, MATTEO, CHIARA = 1, 2, 3, 4
MEMBERS = {BRUNO: "Bruno Neri", MATIAS: "Matías Rodríguez", MATTEO: "Matteo Cesandri", CHIARA: "Chiara Bianchi"}
TODAY = date(2026, 11, 24)


def at(day: int, hour: int, minute: int = 0) -> float:
    """A Rome wall-clock time in November 2026, as unix seconds."""
    return datetime(2026, 11, day, hour, minute, tzinfo=ROME).timestamp()


def task(task_id, title, owner=BRUNO, due=date(2026, 11, 25), **extra):
    return Task(task_id=task_id, title=title, owner_id=owner, due_date=due, **extra)


def state(*tasks):
    return ProjectState(tasks=list(tasks))


# ----------------------------------------------------------------------
# The decision, pure (no clock, no I/O)
# ----------------------------------------------------------------------


def test_t1_unknown_status_a_day_before_the_deadline_is_a_candidate():
    found = candidates(state(task("t1", "Dashboard")), [], now=at(24, 14), tz=ROME)
    assert [c.task.task_id for c in found] == ["t1"]
    assert 30 < found[0].hours_left < 40


def test_the_window_opens_36_hours_before_the_end_of_the_due_day_not_earlier():
    end = deadline(date(2026, 11, 25), ROME)
    assert (end.hour, end.minute) == (23, 59)
    before = end.timestamp() - (ASSESS_HOURS + 1) * 3600
    assert candidates(state(task("t1", "Dashboard")), [], now=before, tz=ROME) == []


def test_t2_a_status_changed_recently_needs_no_question():
    recent = task("t1", "Dashboard", touched_at=at(23, 18), progress=80)
    assert candidates(state(recent), [], now=at(24, 14), tz=ROME) == []


def test_t2_but_a_blocked_task_is_still_assessed_even_if_just_updated():
    blocked = task("t1", "Dashboard", touched_at=at(24, 9), status=TaskStatus.BLOCKED)
    assert len(candidates(state(blocked), [], now=at(24, 14), tz=ROME)) == 1


def test_t3_a_done_task_gets_no_question():
    assert candidates(state(task("t1", "Dashboard", status=TaskStatus.DONE)), [], now=at(24, 14), tz=ROME) == []


def test_a_task_with_no_owner_or_no_date_is_not_cs6s():
    tasks = state(task("t1", "Dashboard", owner=None), task("t2", "Slides", due=None))
    assert candidates(tasks, [], now=at(24, 14), tz=ROME) == []


def test_t4_four_tasks_due_together_give_one_question_not_four():
    tasks = state(
        task("t1", "Dashboard", owner=BRUNO),
        task("t2", "Dati RASD", owner=MATIAS),
        task("t3", "Usability test", owner=CHIARA),
        task("t4", "Slides", owner=MATTEO),
    )
    found = candidates(tasks, [], now=at(24, 14), tz=ROME)
    assert len(found) == 4
    assert pick(found, [], now=at(24, 14)) is not None  # one choice, the sweep sends only it


def test_t7_a_task_others_wait_on_comes_first():
    tasks = state(
        task("t1", "Slides", owner=MATTEO),
        task("t2", "Dati RASD", owner=MATIAS),
        task("t3", "Scrivere il RASD", owner=MATTEO, due=date(2026, 11, 28), depends_on=["t2"]),
    )
    best = pick(candidates(tasks, [], now=at(24, 14), tz=ROME), [], now=at(24, 14))
    assert best.task.task_id == "t2"
    assert [t.task_id for t in best.downstream] == ["t3"]


def test_t8_a_declined_or_answered_check_in_is_not_asked_again_for_that_date():
    ledger = [{"task_id": "t1", "due_date": "2026-11-25", "member_id": BRUNO, "asked_at": None,
               "answer": "declined", "answered_at": at(24, 9)}]
    assert candidates(state(task("t1", "Dashboard")), ledger, now=at(24, 14), tz=ROME) == []


def test_t9_a_new_due_date_is_a_new_question_and_the_old_one_never_fires():
    asked_for_old_date = [{"task_id": "t1", "due_date": "2026-11-25", "member_id": BRUNO,
                           "asked_at": at(24, 14), "answer": "", "answered_at": None}]
    moved = task("t1", "Dashboard", due=date(2026, 11, 27))
    # Two days before the new date: outside the window, so nothing (the old question is gone).
    assert candidates(state(moved), asked_for_old_date, now=at(25, 10), tz=ROME) == []
    # Inside the new window: it may be asked again, about the new date.
    found = candidates(state(moved), asked_for_old_date, now=at(26, 14), tz=ROME)
    assert [c.due_key for c in found] == ["2026-11-27"]


def test_t10_an_overdue_task_gets_one_gentle_question_then_nothing():
    late = task("t1", "Dashboard", due=date(2026, 11, 23))  # due end of 23rd
    found = candidates(state(late), [], now=at(24, 14), tz=ROME)
    assert len(found) == 1 and found[0].hours_left < 0
    asked = [{"task_id": "t1", "due_date": "2026-11-23", "member_id": BRUNO, "asked_at": at(24, 14),
              "answer": "", "answered_at": None}]
    assert candidates(state(late), asked, now=at(24, 18), tz=ROME) == []
    # Long overdue: no repeated pressure at all.
    assert candidates(state(late), [], now=at(26, 10), tz=ROME) == []


def test_t15_a_task_the_group_is_already_discussing_needs_no_question():
    chat = [(at(24, 9), "Matías Rodríguez: Bruno com'è messo il dashboard?"),
            (at(24, 9, 5), "Bruno Neri: quasi finito, stasera lo chiudo")]
    assert candidates(state(task("t1", "Dashboard")), [], now=at(24, 14), tz=ROME, chat=chat) == []


def test_a_snoozed_task_is_not_asked_about():
    found = candidates(state(task("t1", "Dashboard")), [], now=at(24, 14), tz=ROME, snoozed={"dashboard"})
    assert found == []


def test_the_same_member_is_not_asked_twice_in_a_day():
    tasks = state(task("t1", "Dashboard", owner=BRUNO), task("t2", "API", owner=BRUNO))
    ledger = [{"task_id": "t1", "due_date": "2026-11-25", "member_id": BRUNO, "asked_at": at(24, 9),
               "answer": "", "answered_at": None}]
    assert pick(candidates(tasks, ledger, now=at(24, 14), tz=ROME), ledger, now=at(24, 14)) is None


def test_quiet_hours_follow_the_project_time_zone_across_daylight_saving():
    assert quiet(at(24, 23), ROME) and quiet(at(24, 7), ROME) and not quiet(at(24, 9), ROME)
    # 29 March 2026: Rome moves to UTC+2; 08:30 local is still not quiet.
    assert not quiet(datetime(2026, 3, 29, 8, 30, tzinfo=ROME).timestamp(), ROME)
    assert deadline(date(2026, 3, 29), ROME).utcoffset().total_seconds() == 7200


# ----------------------------------------------------------------------
# The sweep: real store, simulated clock, fake wording
# ----------------------------------------------------------------------


class Harness:
    def __init__(self, store, *, now, chat=(), text="Bruno, come sta andando il dashboard? Ti manca qualcosa?",
                 conversation=None, busy=""):
        self.store = store
        self.now = [now]
        self.chat = list(chat)
        self.said: list[str] = []
        self.asked: list[str] = []
        self.text = text

        async def write(candidate, **_):
            self.asked.append(candidate.task.task_id)
            return self.text

        async def say(text):
            self.said.append(text)

        async def recent():
            return self.chat

        self.monitor = ProgressMonitor(
            conversation or FakeConversation(conversation_id="web:G1"),
            store=lambda: store,
            members=lambda: MEMBERS,
            write=write,
            say=say,
            chat=recent,
            busy=lambda: busy,
            tz=ROME,
            clock=lambda: self.now[0],
        )


async def add(store, task_id, title, owner, due="2026-11-25", depends_on=None):
    await store.add_task_by_hand(task_id, title, owner, due, depends_on=depends_on)


async def test_t1_the_sweep_sends_one_short_question_and_records_it(store):
    await add(store, "t1", "Dashboard", BRUNO)
    h = Harness(store, now=at(24, 14))
    assert await h.monitor.sweep() is True
    assert h.said == ["Bruno, come sta andando il dashboard? Ti manca qualcosa?"]
    (row,) = await store.progress_checkins()
    assert row["task_id"] == "t1" and row["due_date"] == "2026-11-25" and row["member_id"] == BRUNO
    # The question counts for the cards' spacing too (one budget).
    assert (await store.mark("proactive"))[1] == at(24, 14)


async def test_t3_t6_nothing_to_ask_means_silence_and_no_model_call(store):
    await add(store, "t1", "Dashboard", BRUNO)
    await store.set_task_status("t1", "done")
    h = Harness(store, now=at(24, 14))
    assert await h.monitor.sweep() is False
    assert h.asked == [] and h.said == []


async def test_t4_t16_a_group_back_after_days_with_four_deadlines_gets_one_message(store):
    for i, owner in enumerate((BRUNO, MATIAS, CHIARA, MATTEO), start=1):
        await add(store, f"t{i}", f"Deliverable {i} part", owner)
    h = Harness(store, now=at(24, 14))
    sent = [await h.monitor.sweep() for _ in range(4)]  # the timer looks again and again
    assert sent == [True, False, False, False] and len(h.said) == 1
    # Even hours later, the spacing allows only one more, never a burst.
    h.now[0] = at(24, 14) + SPACING_SECONDS + 60
    assert await h.monitor.sweep() is True
    assert len(h.said) == 2


async def test_t14_two_timers_on_the_same_file_send_the_question_once(store, tmp_path):
    from muca_server.cognition.coordination.store import CoordinationStore

    await add(store, "t1", "Dashboard", BRUNO)
    one = Harness(store, now=at(24, 14))
    two = Harness(CoordinationStore(tmp_path / "group.db"), now=at(24, 14))  # another worker, same file
    results = await asyncio.gather(one.monitor.sweep(), two.monitor.sweep())
    assert sorted(results) == [False, True]
    assert len(one.said) + len(two.said) == 1


async def test_t9_a_deadline_moved_after_the_question_does_not_repeat_the_old_one(store):
    await add(store, "t1", "Dashboard", BRUNO)
    h = Harness(store, now=at(24, 14))
    assert await h.monitor.sweep() is True
    await store.edit_task("t1", {"due_date": "2026-11-28"})
    h.now[0] = at(24, 14) + SPACING_SECONDS + 60
    assert await h.monitor.sweep() is False  # the old question is gone, the new date is not close


async def test_t8_a_member_who_declined_is_not_asked(store):
    await add(store, "t1", "Dashboard", BRUNO)
    await store.record_progress("t1", "2026-11-25", "declined", at(24, 8))
    h = Harness(store, now=at(24, 14))
    assert await h.monitor.sweep() is False


async def test_the_model_seeing_the_status_in_the_chat_keeps_cadence_silent_for_that_date(store):
    await add(store, "t1", "Dashboard", BRUNO)
    h = Harness(store, now=at(24, 14), text="")
    assert await h.monitor.sweep() is False and h.said == []
    h.now[0] += SPACING_SECONDS + 60
    assert await h.monitor.sweep() is False and h.asked == ["t1"]  # not even asked to the model again


async def test_the_shared_budget_waits_for_cards_quiet_hours_and_running_drafts(store):
    await add(store, "t1", "Dashboard", BRUNO)
    await store.save_mark("proactive", 3, at(24, 13, 50))
    assert await Harness(store, now=at(24, 14)).monitor.sweep() is False  # a card 10 minutes ago
    assert await Harness(store, now=at(24, 23)).monitor.sweep() is False  # quiet hours
    assert await Harness(store, now=at(24, 15), busy="R2 a card draft is running").monitor.sweep() is False
    chat = [(at(24, 14, 59), "Cadence: Ecco il piano aggiornato.")]
    assert await Harness(store, now=at(24, 15), chat=chat).monitor.sweep() is False  # Cadence spoke last
    assert await Harness(store, now=at(24, 15)).monitor.sweep() is True


async def test_never_in_a_direct_message(store):
    await add(store, "t1", "Dashboard", BRUNO)
    h = Harness(store, now=at(24, 14), conversation=FakeConversation(conversation_id="dm:1", is_private=True))
    assert await h.monitor.sweep() is False


async def test_a_member_changing_progress_counts_as_a_recent_update(store):
    await add(store, "t1", "Dashboard", BRUNO)
    await store.edit_task("t1", {"progress": 60})
    (saved,) = (await store.load_state()).tasks
    assert saved.touched_at is not None


# ----------------------------------------------------------------------
# The reply reaches the ask path (T5, T6) and the no-access claim (T13)
# ----------------------------------------------------------------------


def answer(text, **extra):
    return {"answer": text, "task_ids": [], "milestone_ids": [], "decision_ids": [], "documents": [], **extra}


def none_action():
    return {"kind": "none", "task_id": None, "title": "", "owner_id": None, "due_date": None,
            "depends_on_task_id": None, "depends_on_title": "", "depends_on_owner_id": None}


async def _web_task(web):
    await web.route_coordinator(command("task", 100_000_002, add="Dashboard", owner=100_000_002, due="2026-11-25"))
    await web.wait_for_background()


async def test_t6_an_on_track_reply_is_recorded_and_stops_further_questions(tmp_path):
    group = Group(tmp_path)
    web, web_q = await open_web_space(group)
    await _web_task(web)
    task_id = (await group.store_for(WEB).load_state()).tasks[0].task_id
    group.writer._client.answers.append(
        answer("Perfetto, grazie dell'aggiornamento.", action=none_action(), progress={"task_id": task_id, "status": "on_track"})
    )
    await web.route_coordinator(command("ask", 100_000_002, question="Tutto bene, mi manca solo un ultimo controllo", maybe=True))
    await web.wait_for_background()
    notice = [m for m in await drain(web_q) if isinstance(m, CoordinatorNoticeMessage)][-1]
    assert notice.text == "Perfetto, grazie dell'aggiornamento."
    (row,) = await group.store_for(WEB).progress_checkins()
    assert row["answer"] == "on_track"
    state_after = await group.store_for(WEB).load_state()
    assert candidates(state_after, [row], now=at(24, 14), tz=ROME) == []
    # The plan itself is untouched: a reply is memory, never a change.
    assert state_after.tasks[0].status is TaskStatus.TODO


async def test_t5_a_behind_reply_is_recorded_and_the_prompt_asks_for_a_kind_clarification(tmp_path):
    group = Group(tmp_path)
    web, web_q = await open_web_space(group)
    await _web_task(web)
    task_id = (await group.store_for(WEB).load_state()).tasks[0].task_id
    group.writer._client.answers.append(
        answer("Capito. È una questione di tempo o c'è qualcosa di specifico che ti blocca?",
               action=none_action(), progress={"task_id": task_id, "status": "behind"})
    )
    await web.route_coordinator(command("ask", 100_000_002, question="Sono indietro, non penso di farcela", maybe=True))
    await web.wait_for_background()
    (row,) = await group.store_for(WEB).progress_checkins()
    assert row["answer"] == "behind"
    system = group.writer._client.calls[-1]["messages"][0]["content"]
    assert "whether it is time or something specific" in system and "Never blame" in system


async def test_a_progress_answer_about_an_unknown_task_is_retried_then_silence(tmp_path):
    bad = answer("Ok.", action=none_action(), progress={"task_id": "nope", "status": "on_track"})
    group = Group(tmp_path, bad, bad)
    web, web_q = await open_web_space(group)
    await web.route_coordinator(command("ask", 100_000_002, question="tutto ok", maybe=True))
    await web.wait_for_background()
    assert await group.store_for(WEB).progress_checkins() == []


async def test_t13_an_answer_claiming_no_access_to_the_project_is_refused_and_retried(tmp_path):
    group = Group(tmp_path)
    web, web_q = await open_web_space(group)
    await _web_task(web)
    group.writer._client.answers.extend([
        answer("Non ho accesso allo stato del progetto oltre a questa chat.", action=none_action()),
        answer("Il dashboard è di Bruno, scade il 25 novembre.", action=none_action()),
    ])
    await web.route_coordinator(command("ask", 100_000_001, question="Cadence, chi fa il dashboard?"))
    await web.wait_for_background()
    notice = [m for m in await drain(web_q) if isinstance(m, CoordinatorNoticeMessage)][-1]
    assert notice.text.startswith("Il dashboard è di Bruno")


async def test_t11_t12_the_ask_prompt_allows_casual_talk_in_the_members_language(tmp_path):
    group = Group(tmp_path, answer("Non gioco a calcio, ma ne parlo volentieri. Tu giochi spesso?", action=none_action()))
    web, web_q = await open_web_space(group)
    await web.route_coordinator(command("language", 100_000_001, language="en"))
    await web.route_coordinator(command("ask", 100_000_001, question="Giochi spesso a calcio, Cadence?"))
    await web.wait_for_background()
    notice = [m for m in await drain(web_q) if isinstance(m, CoordinatorNoticeMessage)][-1]
    assert notice.text.startswith("Non gioco a calcio")
    system = group.writer._client.calls[-1]["messages"][0]["content"]
    assert "Do not steer them back to the project" in system
    # A group that chose English still gets chat replies in the member's own language.
    assert "follows the language of that member's own message" in system


# ----------------------------------------------------------------------
# R10: one responder per message
# ----------------------------------------------------------------------


def _bridge():
    conversation = FakeConversation(conversation_id="web:G1")
    return ProactiveBridge(
        conversation, writer=None, store=lambda: None, service=lambda: None, send_proposal=None, clock=lambda: 1000.0
    )


async def test_r10_a_follow_up_sent_to_cadences_ask_is_not_answered_again_by_the_lab_agent():
    bridge = _bridge()
    bridge.claim("E tu, giochi spesso a calcio?")
    history = [SimpleNamespace(content="e tu,  giochi spesso a calcio?", speaker_id=7)]
    verdict = await bridge.offer("respond_when_addressed", history=history, message_count=3, assistant_id=0)
    assert verdict is Verdict.VETOED
    other = [SimpleNamespace(content="MULCA, riassumi?", speaker_id=7)]
    assert await bridge.offer("respond_when_addressed", history=other, message_count=4, assistant_id=0) is Verdict.NOT_MINE


async def test_r10_the_ask_command_with_maybe_claims_its_text(tmp_path):
    group = Group(tmp_path, answer("", action=none_action()))
    web, web_q = await open_web_space(group)
    web.proactive = _bridge()
    await web.route_coordinator(command("ask", 100_000_002, question="e tu che ne pensi?", maybe=True))
    await web.wait_for_background()
    assert web.proactive.is_claimed("E tu che ne pensi?")


# ----------------------------------------------------------------------
# Timer and settings
# ----------------------------------------------------------------------


async def test_the_timer_never_sends_a_progress_question_and_a_checkin_card_in_one_look(store):
    from muca_server.cognition.coordination.checkin_timer import CheckinTimer

    calls = []

    async def progress():
        calls.append("progress")
        return True

    async def checkin():
        calls.append("checkin")
        return True

    timer = CheckinTimer(FakeConversation(), interval_seconds=1, store=lambda: store,
                         run_checkin=checkin, run_progress=progress, clock=lambda: 10_000.0)
    await store.save_mark("checkin", 0, 0.0)
    assert await timer.tick() is True and calls == ["progress"]


def test_progress_check_ins_are_on_with_the_coordinator_unless_switched_off():
    from muca_server.cognition.coordination.settings import CoordinatorSettings

    on = CoordinatorSettings.from_env({"MUCA_COORDINATOR_ENABLED": "on"})
    assert on.progress_checkins and on.timezone == "Europe/Rome"
    off = CoordinatorSettings.from_env({"MUCA_COORDINATOR_ENABLED": "on", "MUCA_COORDINATOR_PROGRESS_CHECKINS": "off",
                                        "MUCA_COORDINATOR_TIMEZONE": "Europe/London"})
    assert not off.progress_checkins and off.timezone == "Europe/London"


async def test_from_the_router_the_question_is_said_in_the_chat_and_joins_its_memory(tmp_path):
    from muca_server.cognition.coordination.router import CoordinatorRouter
    from muca_server.protocol import AssistantTextMessage

    from .conftest import FakeChatClient
    from muca_server.cognition.coordination.writer import ProposalWriter

    clock = [at(24, 14)]
    now = clock[0]
    group = Group(tmp_path, clock=lambda: clock[0])
    group.writer = ProposalWriter(FakeChatClient(
        {"text": "Bruno, come sta andando il dashboard? La consegna è domani: ti manca ancora qualcosa?", "skip_reason": ""}
    ))
    queue: asyncio.Queue = asyncio.Queue()
    web = CoordinatorRouter(
        FakeConversation(conversation_id=WEB, roster={100_000_001: "Ada", 100_000_002: "Bruno"}),
        queue, store_for=group.store_for, writer=group.writer, proposal_ttl_seconds=3600,
        catchup=group.catchup, progress_checkins=True, timezone="Europe/Rome",
    )
    await group.store_for(WEB).add_task_by_hand("t1", "Dashboard", 100_000_002, "2026-11-25")
    web.progress._clock = lambda: now
    assert await web.checkin_timer.tick() is True
    (said,) = [m for m in await drain(queue) if isinstance(m, AssistantTextMessage)]
    assert said.text.startswith("Bruno, come sta andando il dashboard?")
    clock[0] += 1
    assert any(line.startswith("Cadence: Bruno, come sta") for line in await group.catchup.recent_lines(WEB))
    # The question names the task and the owner; its prompt saw the deadline.
    prompt = group.writer._client.calls[-1]["messages"][-1]["content"]
    assert "it is due tomorrow" in prompt and "owner: Bruno" in prompt
