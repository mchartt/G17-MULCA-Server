"""
Matteo, 20/11: Cadence must know everything the project holds and answer
naturally, within the conversation. The `ask` path reads the goal, the
members' profiles, milestones, tasks (progress, description, what they wait
on), decisions, the documents the group gave it and the chat's last lines,
Cadence's own included; the question and the answer join the chat's log, so
a follow-up or a catch-up sees them. Every id and document name is checked.
"""

from __future__ import annotations

import time

from muca_server.protocol import CoordinatorNoticeMessage

from .conftest import drain
from .test_coordinator_links import WEB, WEB_ROSTER, Group, command, open_web_space, say


def answer(text="ok", **sources):
    return {"answer": text, "task_ids": [], "milestone_ids": [], "decision_ids": [], "documents": [], **sources}


async def ask(router, queue, question, member=100_000_001):
    await router.route_coordinator(command("ask", member, question=question))
    await router.wait_for_background()
    return [m for m in await drain(queue) if isinstance(m, CoordinatorNoticeMessage)][-1]


def prompt(group, n=-1):
    return group.writer._client.calls[n]["messages"][-1]["content"]


async def test_the_answer_sees_documents_profiles_progress_and_the_chat(tmp_path):
    group = Group(tmp_path, answer("The demo is on 2 December.", documents=["Brief.pdf"]))
    web, web_q = await open_web_space(group)
    await web.route_coordinator(command("profile", 100_000_001, discipline="HCI", skills=["Figma"]))
    await web.route_coordinator(
        command("document", 100_000_001, add={"name": "Brief.pdf", "text": "Intro.\n" + "x " * 600 + "The demo is on 2 December in room B."})
    )
    await web.route_coordinator(command("task", 100_000_001, add="Build the API", description="FastAPI, three endpoints"))
    await drain(web_q)
    await say(tmp_path, WEB, "Bruno", "When is the demo again?", time.time() - 30)

    notice = await ask(web, web_q, "cadence when is the demo?")
    assert notice.ok and notice.text == "The demo is on 2 December.\nFrom: “Brief.pdf”."
    user = prompt(group)
    assert "Goal: not set yet" in user
    assert "discipline: HCI" in user and "Figma" in user
    assert "Build the API" in user and "0% done" in user and "FastAPI, three endpoints" in user
    assert "[document: Brief.pdf]" in user and "room B" in user  # the passage that matches the question
    assert "Bruno: When is the demo again?" in user


async def test_the_question_and_the_answer_join_the_chat_so_a_follow_up_sees_them(tmp_path):
    group = Group(tmp_path, answer("Ada owns it."), answer("Friday, as planned."))
    web, web_q = await open_web_space(group)
    first = await ask(web, web_q, "Cadence, who owns the slides?")
    assert first.text == "Ada owns it."
    await ask(web, web_q, "and by when?", member=100_000_002)
    follow_up = prompt(group)
    assert f"{WEB_ROSTER[100_000_001]}: Cadence, who owns the slides?" in follow_up
    assert "Cadence: Ada owns it." in follow_up
    # The catch-up of another member sees the exchange too.
    await web.route_coordinator(command("catchup", 100_000_002, peek=True))
    catchup = [m for m in await drain(web_q) if getattr(m, "catchup", None)][-1].catchup
    assert any(m["text"] == "Ada owns it." for m in catchup["here"]["messages"])


async def test_a_document_the_project_does_not_have_is_never_cited(tmp_path):
    bad = answer("See the syllabus.", documents=["Syllabus.pdf"])
    group = Group(tmp_path, bad, bad)
    web, web_q = await open_web_space(group)
    notice = await ask(web, web_q, "What does the syllabus say?")
    assert not notice.ok


async def test_a_follow_up_that_is_not_for_cadence_gets_silence(tmp_path):
    """Matteo, 20/11: after Cadence answered, the next messages may be for it; when they are not, it stays quiet."""
    group = Group(tmp_path, answer(""), answer("Yes, Friday."))
    web, web_q = await open_web_space(group)
    await web.route_coordinator(command("ask", 100_000_002, question="ok Bruno I'll send you the file", maybe=True))
    await web.wait_for_background()
    (notice,) = [m for m in await drain(web_q) if isinstance(m, CoordinatorNoticeMessage)]
    assert notice.ok and notice.text == ""
    assert "does not name you" in prompt(group)
    # Nothing was said, so nothing joins the chat's memory.
    await web.route_coordinator(command("catchup", 100_000_001, peek=True))
    catchup = [m for m in await drain(web_q) if getattr(m, "catchup", None)][-1].catchup
    assert catchup["here"]["total"] == 0

    notice = await ask(web, web_q, "and is that still friday?")
    assert notice.text == "Yes, Friday."


async def test_an_empty_answer_to_a_direct_question_is_not_accepted(tmp_path):
    group = Group(tmp_path, answer(""), answer(""))
    web, web_q = await open_web_space(group)
    notice = await ask(web, web_q, "Cadence, who owns the slides?")
    assert not notice.ok  # named directly: silence is not an answer
