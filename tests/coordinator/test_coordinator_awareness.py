"""
Matteo's 20/11 screenshots (full application awareness), each a regression test.

- A: "Controlla le mie task" got "which Matteo Cesandri are you, Software
  Engineer or Design?". The prompt never said who was writing and listed task
  owners by name only. The server now names the asker by member id, lists
  their tasks by owner id, and tells a same-name record apart.
- B: after "Frontend Application", "sono al 30%" got "which task?" again. The
  task a member was last talking about is kept by task id (per member, for a
  while) and given to the model as their focus.
- C: a second message while the first was being answered got "already
  working" (and a follow-up, nothing at all). Messages to Cadence now wait
  their turn and every one is answered.
- D: the update itself is the existing card path (test_coordinator_ask_updates).
- Sources: an answer that drafts a card has no "Fonti:" line under it.

Fake model: these tests prove what the server sends and does, not how a real
model answers (that is CONVERSATION_EVAL.md).
"""

from __future__ import annotations

import asyncio
from datetime import date

from muca_server.cognition.coordination.plan_commands import FOCUS_SECONDS
from muca_server.cognition.coordination.router import CoordinatorRouter
from muca_server.cognition.coordination.writer import ProposalWriter

from .conftest import FakeConversation
from .test_coordinator_conversational import ADA, BRUNO, answer, ask, task
from .test_coordinator_language_charter import make
from .test_coordinator_router import command
from .test_coordinator_writer import FakeChatClient

FRONTEND = "Frontend Application"


def prompt(client, n=-1) -> str:
    return client.calls[n]["messages"][-1]["content"]


def make_with(store, roster, *answers):
    client = FakeChatClient(*answers)
    queue: asyncio.Queue = asyncio.Queue()
    router = CoordinatorRouter(
        FakeConversation(roster=roster), queue, store_for=lambda _: store, writer=ProposalWriter(client),
        proposal_ttl_seconds=3600, today=lambda: date(2026, 11, 15),
    )
    return router, queue, client


# ----------------------------------------------------------------------
# A: who is writing comes from the member id, never from the model
# ----------------------------------------------------------------------


async def test_the_prompt_names_the_asker_and_their_tasks_by_member_id(store):
    mine = await task(store, FRONTEND, owner=ADA)
    await task(store, "API", owner=BRUNO)
    router, queue, client = make(store, answer("Hai una task: Frontend Application, al 0%."))
    await ask(router, queue, "Controlla le mie task.")
    user = prompt(client)
    block = user.split("<asker>")[1].split("</asker>")[0]
    assert "The member writing to you is id 1: Ada" in block
    assert f"task_id {mine}: {FRONTEND}" in block and "API" not in block
    assert "never ask which member or which profile" in block
    # Task owners carry their id, so a shared name is never ambiguous.
    assert f"{FRONTEND} | owner: Ada (id 1)" in user


async def test_two_members_with_the_same_name_are_told_apart_by_id(store):
    roster = {1: "Matteo Cesandri", 2: "Matteo Cesandri", 3: "Bruno"}
    await task(store, FRONTEND, owner=2)
    router, queue, client = make_with(store, roster, answer("Con questo account non hai task."))
    await ask(router, queue, "Controlla le mie task.", member=1)
    block = prompt(client).split("<asker>")[1].split("</asker>")[0]
    assert "id 1: Matteo Cesandri" in block
    assert "- (none)" in block  # the other record's task is not theirs
    assert "Member id 2 has the same name but is another member record" in block


async def test_a_message_from_someone_not_in_the_roster_names_nobody(store):
    router, queue, client = make(store, answer("Ok."))
    from muca_server.cognition.coordination.plan_writer import _asker
    from muca_server.cognition.coordination.models import ProjectState

    assert _asker(99, None, {1: "Ada"}, ProjectState()) == ""


# ----------------------------------------------------------------------
# B: the task the member was last talking about stays "it"
# ----------------------------------------------------------------------


async def test_the_task_named_in_an_answer_becomes_the_members_focus(store):
    front = await task(store, FRONTEND, owner=ADA)
    await task(store, "API", owner=BRUNO)
    progress = {"kind": "update_task", "task_id": front, "progress": 30}
    router, queue, client = make(
        store,
        answer("Frontend Application è al 0%.", task_ids=[front]),
        answer("Ho preparato l'aggiornamento di Frontend Application al 30%: confermalo dalla card.", progress),
    )
    await ask(router, queue, "Frontend Application.")
    notice, (card,) = await ask(router, queue, "Oii Cadence, sono al 30% della mia task.")
    assert f"last talking about with you: task_id {front}: {FRONTEND}" in prompt(client)
    assert card["kind"] == "checkin" and card["payload"]["options"][0]["new_progress"] == 30
    assert "Fonti" not in notice.text and "From:" not in notice.text  # the card shows what changes


async def test_focus_is_per_member_and_expires(store):
    front = await task(store, FRONTEND, owner=ADA)
    router, queue, client = make(
        store, answer("Ok.", task_ids=[front]), answer("Ok."), answer("Ok."),
    )
    now = [1000.0]
    router._plan._clock = lambda: now[0]
    await ask(router, queue, "Frontend Application.", member=ADA)
    await ask(router, queue, "E la mia?", member=BRUNO)
    assert "last talking about" not in prompt(client)  # Bruno never talked about it
    now[0] += FOCUS_SECONDS + 1
    await ask(router, queue, "Sono al 30%.", member=ADA)
    assert "last talking about" not in prompt(client)  # too long ago


async def test_a_list_of_tasks_keeps_the_earlier_focus(store):
    front = await task(store, FRONTEND, owner=ADA)
    api = await task(store, "API", owner=ADA)
    router, queue, client = make(
        store, answer("Ok.", task_ids=[front]), answer("Due task.", task_ids=[front, api]), answer("Ok."),
    )
    await ask(router, queue, "Frontend Application.")
    await ask(router, queue, "Quali sono le mie task?")
    await ask(router, queue, "Sono al 30%.")
    assert f"task_id {front}: {FRONTEND}. \"it\"" in prompt(client)


# ----------------------------------------------------------------------
# C: no message to Cadence is refused or lost
# ----------------------------------------------------------------------


async def test_two_messages_sent_at_once_are_both_answered_in_order(store):
    gate = asyncio.Event()

    class Slow(FakeChatClient):
        async def _create(self, **kwargs):
            await gate.wait()
            return await super()._create(**kwargs)

    client = Slow(answer("Primo: ricevuto."), answer("Sì, ti sento."))
    queue: asyncio.Queue = asyncio.Queue()
    router = CoordinatorRouter(FakeConversation(), queue, store_for=lambda _: store, writer=ProposalWriter(client),
                               proposal_ttl_seconds=3600, today=lambda: date(2026, 11, 15))
    first = command("ask", ADA, question="sono al 30% della mia task")
    second = command("ask", ADA, question="mi senti?")
    second = second.model_copy(update={"request_id": "r2"})
    await router.route_coordinator(first)
    await router.route_coordinator(second)
    gate.set()
    await router.wait_for_background()
    notices = []
    while not queue.empty():
        notices.append(queue.get_nowait())
    texts = [(n.request_id, n.text) for n in notices if getattr(n, "text", None) is not None]
    assert texts == [(first.request_id, "Primo: ricevuto."), ("r2", "Sì, ti sento.")]
    assert "sono al 30%" in prompt(client, 0) and "mi senti?" in prompt(client, 1)


async def test_a_failed_model_call_on_a_direct_message_is_said_not_swallowed(store):
    from muca_server.cognition.coordination.router import UNAVAILABLE

    router, queue, _ = make(store, {"broken": True}, {"broken": True})
    notice, cards = await ask(router, queue, "Cadence, sono al 30%.")
    assert notice.ok is False and notice.text == UNAVAILABLE and cards == []
