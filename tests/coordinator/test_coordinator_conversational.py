"""
Matteo, 20/11 (conversational optimization): the RASD / Matías case and what
it showed, each a regression test.

- F1 (live run 20/11): a CS5 draft on "I'll look at it tonight" made the CS7 on
  "Mathias must first send me the data for the RASD" be dropped 7 s later
  (R2). A more urgent strategy now replaces a draft still being written.
- F2: the prerequisite a CS7 card adds had no owner, though the chat named who
  delivers it; the card now carries `depends_on_owner_id` (a member only).
- F3: a link made by confirming a card skipped the loop and self checks the
  by-hand path had; one check now guards every way a link is made.
- F4: a link kept no origin, so "why are these linked?" had no answer; each
  link now keeps its source (old links: unknown, and Cadence says so).
- F5: Cadence could only describe a change; asked in words ("add the data as a
  prerequisite of the RASD", "yes, do it") it now drafts the card, which a
  member confirms. Duplicates, links already recorded and loops are refused
  by the server, not left to the model.
"""

from __future__ import annotations

import asyncio
from datetime import date

import aiosqlite

from muca_server.cognition.coordination.models import (
    BlockerCertainty,
    BlockerPayload,
    Evidence,
    ProactiveContext,
    ProposalKind,
    ProposalStatus,
)
from muca_server.cognition.coordination.plan_writer import _ask_action
from muca_server.cognition.coordination.proactive import ProactiveBridge, Verdict
from muca_server.cognition.coordination.proactive_writer import ProactiveDraft
from muca_server.cognition.coordination.proposals import ProposalService
from muca_server.cognition.coordination.store import CoordinationStore
from muca_server.protocol import CoordinatorNoticeMessage, CoordinatorProposalMessage

from .conftest import FakeConversation, drain
from .m3_fixtures import task_payload
from .test_coordinator_language_charter import make
from .test_coordinator_m3_proactive import HISTORY
from .test_coordinator_proposals import Clock
from .test_coordinator_router import click, command, of_type

RASD = "Rivedere e correggere il RASD"
ADA, BRUNO = 1, 2  # the router tests' roster: 1 Ada, 2 Bruno, 3 Chiara


def answer(text, action=None, **sources):
    none = {"kind": "none", "task_id": None, "title": "", "owner_id": None, "due_date": None,
            "depends_on_task_id": None, "depends_on_title": "", "depends_on_owner_id": None}
    return {"answer": text, "task_ids": [], "milestone_ids": [], "decision_ids": [], "documents": [],
            "action": {**none, **(action or {})}, **sources}


async def ask(router, queue, question, member=ADA):
    await router.route_coordinator(command("ask", member, question=question))
    await router.wait_for_background()
    sent = await drain(queue)
    return of_type(sent, CoordinatorNoticeMessage)[-1], [m.proposal for m in of_type(sent, CoordinatorProposalMessage)]


async def task(store, title, owner=None, depends_on=None):
    """A task straight into the store (no dependency suggestion, no model call)."""
    task_id = "t" + str(len((await store.load_state()).tasks) + 1)
    await store.add_task_by_hand(task_id, title, owner, None, "", depends_on)
    return task_id


async def by_id(store, task_id):
    return next(t for t in (await store.load_state()).tasks if t.task_id == task_id)


# ----------------------------------------------------------------------
# F5 + F2 + F4: asked in words, drafted as a card, confirmed by a member
# ----------------------------------------------------------------------


async def test_asked_to_add_a_prerequisite_cadence_drafts_a_card_and_only_the_click_saves_it(store):
    rasd = await task(store, RASD, owner=ADA)
    router, queue, client = make(store, answer(
        "Ecco la proposta: confermala qui sotto.",
        {"kind": "dependency", "task_id": rasd, "depends_on_title": "Bruno manda i dati del RASD", "depends_on_owner_id": BRUNO},
    ))
    notice, (card,) = await ask(router, queue, "aggiungi i dati di Bruno come prerequisito del RASD")
    assert notice.ok and "confermala" in notice.text
    payload = card["payload"]
    assert card["kind"] == "blocker" and card["status"] == "pending"
    assert payload["task_id"] == rasd and payload["certainty"] == "possible"
    assert payload["depends_on_owner_id"] == BRUNO
    assert payload["context"]["evidence"][-1]["text"] == "aggiungi i dati di Bruno come prerequisito del RASD"
    assert (await by_id(store, rasd)).depends_on == []  # nothing changed before the click

    await router.route_coordinator(click(card["proposal_id"], "confirm", member=ADA))
    sent = await drain(queue)
    # Cadence spoke Italian on the card: the server's confirmation is Italian too.
    assert "è di Bruno" in of_type(sent, CoordinatorNoticeMessage)[-1].text
    state = await store.load_state()
    data = next(t for t in state.tasks if t.title == "Bruno manda i dati del RASD")
    rasd_task = next(t for t in state.tasks if t.task_id == rasd)
    # Direction: the data come first, the RASD waits on them (data -> RASD).
    assert rasd_task.depends_on == [data.task_id] and data.depends_on == []
    assert data.owner_id == BRUNO and rasd_task.owner_id == ADA
    assert rasd_task.status.value == "todo"  # a prerequisite, nobody said it is stuck
    assert rasd_task.dependency_sources[data.task_id].startswith(f"card {card['proposal_id']}")
    assert "prerequisito del RASD" in rasd_task.dependency_sources[data.task_id]


async def test_asked_for_a_task_for_someone_cadence_drafts_an_owner_card(store):
    router, queue, client = make(store, answer(
        "Pronta da confermare.", {"kind": "new_task", "title": "Raccogliere i dati", "owner_id": BRUNO, "due_date": "2026-11-23"},
    ))
    notice, (card,) = await ask(router, queue, "crea una task per Bruno: raccogliere i dati entro il 23")
    assert card["kind"] == "task_owner" and card["payload"]["owner_id"] == BRUNO
    assert (await store.load_state()).tasks == []
    await router.route_coordinator(click(card["proposal_id"], "confirm"))
    await drain(queue)
    (saved,) = (await store.load_state()).tasks
    assert saved.title == "Raccogliere i dati" and saved.owner_id == BRUNO and saved.due_date == date(2026, 11, 23)


async def test_a_link_already_in_the_plan_gets_no_card_and_the_server_says_so(store):
    api = await task(store, "Build the API")
    tests = await task(store, "Test the API", depends_on=[api])
    router, queue, _ = make(store, answer("Fatto, card pronta.", {"kind": "dependency", "task_id": tests, "depends_on_task_id": api}))
    notice, cards = await ask(router, queue, "collega i test all'API")
    assert cards == [] and "registra già" in notice.text


async def test_a_link_that_would_close_a_loop_gets_no_card(store):
    api = await task(store, "Build the API")
    tests = await task(store, "Test the API", depends_on=[api])
    router, queue, _ = make(store, answer("Card pronta.", {"kind": "dependency", "task_id": api, "depends_on_task_id": tests}))
    notice, cards = await ask(router, queue, "l'API dipende dai test")
    assert cards == [] and "giro chiuso" in notice.text
    assert (await by_id(store, api)).depends_on == []


async def test_saying_it_twice_gives_one_card(store):
    rasd = await task(store, RASD, owner=ADA)
    action = {"kind": "dependency", "task_id": rasd, "depends_on_title": "Dati del RASD"}
    router, queue, _ = make(store, answer("Card pronta.", action), answer("Card pronta.", action))
    _, first = await ask(router, queue, "aggiungi i dati come prerequisito del RASD")
    notice, second = await ask(router, queue, "AGGIUNGI I DATI COME PREREQUISITO DEL RASD")
    assert len(first) == 1 and second == [] and "card qui sopra" in notice.text


async def test_an_action_on_a_task_that_is_not_in_the_plan_is_retried_never_carded(store):
    bad = answer("Card pronta.", {"kind": "dependency", "task_id": "nope", "depends_on_title": "x"})
    router, queue, client = make(store, bad, bad)
    notice, cards = await ask(router, queue, "collega nope")
    assert cards == [] and not notice.ok
    assert len(client.calls) == 2


def test_the_action_check_refuses_self_links_strangers_and_one_sided_links():
    tasks = {"a": type("T", (), {"title": "A", "depends_on": []})()}
    members = {1: "Ada"}
    for bad in (
        {"kind": "dependency", "task_id": "a", "depends_on_task_id": "a"},
        {"kind": "dependency", "task_id": "a", "depends_on_title": "", "depends_on_task_id": None},
        {"kind": "dependency", "task_id": "a", "depends_on_title": "Data", "depends_on_owner_id": 99},
        {"kind": "new_task", "title": "X", "owner_id": 99},
        {"kind": "new_task", "title": "X", "due_date": "2026-01-01"},
    ):
        try:
            _ask_action(bad, tasks, members, date(2026, 11, 20))
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    assert _ask_action({"kind": "none"}, tasks, members, None) is None


# ----------------------------------------------------------------------
# F3: every link is checked, also when a card is confirmed
# ----------------------------------------------------------------------


async def test_confirming_a_card_that_would_now_close_a_loop_saves_nothing(store):
    api = await task(store, "Build the API")
    tests = await task(store, "Test the API")
    router, queue, _ = make(store, answer("Card pronta.", {"kind": "dependency", "task_id": tests, "depends_on_task_id": api}))
    _, (card,) = await ask(router, queue, "i test dipendono dall'API")
    await store.edit_task(api, {}, depends_on=[tests], source="by hand (Bruno)")  # meanwhile, the other way round
    await router.route_coordinator(click(card["proposal_id"], "confirm"))
    notice = of_type(await drain(queue), CoordinatorNoticeMessage)[-1]
    assert not notice.ok and "circle" in notice.text
    assert (await by_id(store, tests)).depends_on == []
    assert (await store.get_proposal(card["proposal_id"])).status is ProposalStatus.EXPIRED


# ----------------------------------------------------------------------
# F4: where a link came from
# ----------------------------------------------------------------------


async def test_a_file_from_before_link_sources_opens_and_its_links_have_no_known_origin(tmp_path):
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            "CREATE TABLE coord_task_dependency (task_id TEXT NOT NULL, depends_on TEXT NOT NULL, PRIMARY KEY (task_id, depends_on));"
            "INSERT INTO coord_task_dependency VALUES ('t2', 't1');"
        )
        await db.commit()
    store = CoordinationStore(path)
    await store.add_task_by_hand("t1", "Mockup", None, None)
    await store.add_task_by_hand("t2", RASD, None, None)
    rasd = await by_id(store, "t2")
    assert rasd.depends_on == ["t1"] and rasd.dependency_sources == {}


async def test_a_link_made_by_hand_says_who_made_it_and_the_ask_prompt_shows_it(store):
    router, queue, client = make(store, answer("Perché lo ha deciso Ada a mano."))
    await router.route_coordinator(command("task", ADA, add="Build the API"))
    await drain(queue)
    api = (await store.load_state()).tasks[0].task_id
    await store.add_task_by_hand("t9", "Old link", None, None)
    await router.route_coordinator(command("task", ADA, add="Test the API", depends_on=[api, "t9"]))
    await router.wait_for_background()
    await drain(queue)
    tests = next(t for t in (await store.load_state()).tasks if t.title == "Test the API")
    assert tests.dependency_sources == {api: "by hand (Ada)", "t9": "by hand (Ada)"}
    await ask(router, queue, "perché i test sono collegati all'API?")
    prompt = client.calls[-1]["messages"][-1]["content"]
    assert "Build the API (linked by hand (Ada))" in prompt


# ----------------------------------------------------------------------
# F1: R2, a more urgent strategy replaces a draft still being written
# ----------------------------------------------------------------------


class SlowCs5Writer:
    """CS5 drafts slowly (a model call in flight); CS7 answers at once."""

    def __init__(self, cs7_draft) -> None:
        self.cs7_draft = cs7_draft
        self.cancelled = False

    async def draft(self, strategy_id, **kwargs):
        if strategy_id == "cs5_owner_and_date":
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return ProactiveDraft(ProposalKind.TASK_OWNER, task_payload(), "Stepping in because.")
        return self.cs7_draft


def cs7_draft(task_id):
    payload = BlockerPayload(
        context=ProactiveContext(strategy_id="cs7_blockers_dependencies", topic="Dati per il RASD",
                                 evidence=[Evidence(speaker="Ada", text="Bruno deve mandarmi prima i dati del RASD")]),
        task_id=task_id, title=RASD, depends_on_title="Dati del RASD", depends_on_owner_id=BRUNO,
        certainty=BlockerCertainty.POSSIBLE,
    )
    return ProactiveDraft(ProposalKind.BLOCKER, payload, "Intervengo perché il RASD aspetta i dati.")


def bridge_for(store, writer):
    clock = Clock()
    sent = []

    async def send(proposal):
        sent.append(proposal)

    bridge = ProactiveBridge(
        FakeConversation(), writer=writer, store=lambda: store,
        service=lambda: ProposalService(store, clock=clock), send_proposal=send,
        today=lambda: date(2026, 11, 20), clock=clock,
    )
    return bridge, sent


async def test_a_blocker_replaces_an_owner_card_still_being_drafted(store):
    rasd = await task(store, RASD, owner=ADA)
    writer = SlowCs5Writer(cs7_draft(rasd))
    bridge, sent = bridge_for(store, writer)
    assert await bridge.offer("cs5_owner_and_date", history=HISTORY, message_count=20, assistant_id=0) is Verdict.TAKEN
    await asyncio.sleep(0.05)  # the CS5 call is in flight
    assert await bridge.offer("cs7_blockers_dependencies", history=HISTORY, message_count=21, assistant_id=0) is Verdict.TAKEN
    await bridge.wait()
    assert writer.cancelled
    assert [p.kind for p in sent] == [ProposalKind.BLOCKER]


async def test_a_less_urgent_strategy_still_waits_for_the_running_draft(store):
    rasd = await task(store, RASD, owner=ADA)

    class SlowCs7(SlowCs5Writer):
        async def draft(self, strategy_id, **kwargs):
            await asyncio.sleep(0.2)
            return self.cs7_draft

    bridge, sent = bridge_for(store, SlowCs7(cs7_draft(rasd)))
    await bridge.offer("cs7_blockers_dependencies", history=HISTORY, message_count=20, assistant_id=0)
    assert await bridge.offer("cs5_owner_and_date", history=HISTORY, message_count=21, assistant_id=0) is Verdict.VETOED
    await bridge.wait()
    assert [p.kind for p in sent] == [ProposalKind.BLOCKER]


async def test_a_maybe_that_is_only_a_thanks_gets_silence_without_a_model_call(store):
    """F6 (20/11 corpus): after the prompt grew, "ok thanks!" got "You're welcome!" 2 times out of 2."""
    router, queue, client = make(store, answer("You're welcome!"))
    await router.route_coordinator(command("ask", ADA, question="ok thanks!", maybe=True))
    await router.wait_for_background()
    notice = of_type(await drain(queue), CoordinatorNoticeMessage)[-1]
    assert notice.ok and notice.text == "" and client.calls == []
    # A real follow-up still reaches the model; a courtesy-only answer to a maybe is silence.
    await router.route_coordinator(command("ask", ADA, question="grazie, e chi fa i test?", maybe=True))
    await router.wait_for_background()
    assert of_type(await drain(queue), CoordinatorNoticeMessage)[-1].text == "" and len(client.calls) == 1



async def test_saying_it_again_after_the_link_was_confirmed_adds_no_second_prerequisite(store):
    """Review of 20/11: the same prerequisite must not come back as a new task, by ask or by a CS7 card."""
    rasd = await task(store, RASD, owner=ADA)
    first = {"kind": "dependency", "task_id": rasd, "depends_on_title": "Dati del RASD", "depends_on_owner_id": BRUNO}
    again = {"kind": "dependency", "task_id": rasd, "depends_on_title": "I dati su cui si basa il RASD", "depends_on_owner_id": BRUNO}
    router, queue, _ = make(store, answer("Card pronta.", first), answer("Card pronta.", again))
    _, (card,) = await ask(router, queue, "aggiungi i dati di Bruno come prerequisito del RASD")
    await router.route_coordinator(click(card["proposal_id"], "confirm"))
    await drain(queue)
    notice, cards = await ask(router, queue, "Bruno deve mandarmi prima i dati del RASD, aggiungilo")
    assert cards == [] and "registra già" in notice.text
    assert len((await store.load_state()).tasks) == 2

    # The proactive path drops the same prerequisite too (R7).
    from muca_server.cognition.coordination.proactive import _dropped

    state = await store.load_state()
    assert _dropped(cs7_draft(rasd), [], state.tasks) == "R7 the plan already records this prerequisite"


async def test_yes_under_a_card_points_to_that_card_and_drafts_no_second_one(store):
    rasd = await task(store, RASD, owner=ADA)
    action = {"kind": "dependency", "task_id": rasd, "depends_on_title": "Dati", "depends_on_owner_id": BRUNO}
    router, queue, client = make(store, answer("Card pronta.", action), answer("Ok!", action))
    await ask(router, queue, "aggiungi i dati di Bruno come prerequisito del RASD")
    notice, cards = await ask(router, queue, "Sì, fallo.")
    assert cards == [] and "card qui sopra" in notice.text
    # Cadence was shown the card waiting for a click.
    assert "“Rivedere e correggere il RASD” waits on “Dati” (delivered by Bruno)" in client.calls[-1]["messages"][-1]["content"]


async def test_a_prerequisite_named_like_a_plan_task_links_that_task(store):
    rasd = await task(store, RASD, owner=ADA)
    data = await task(store, "Dati del RASD", owner=BRUNO)
    router, queue, _ = make(store, answer("Card pronta.", {"kind": "dependency", "task_id": rasd, "depends_on_title": "dati del RASD!"}))
    _, (card,) = await ask(router, queue, "il RASD dipende dai dati del RASD")
    assert card["payload"]["depends_on_task_id"] == data and card["payload"]["depends_on_owner_id"] is None


def test_the_servers_own_sentences_follow_the_groups_language():
    from muca_server.cognition.coordination.wording import italian, localize

    assert italian("", "Intervengo perché il RASD aspetta i dati.") and not italian("", "Stepping in because the API is late.")
    assert italian("it", "Stepping in") and not italian("en", "Intervengo perché")
    assert localize(
        "Dependency saved: “Rivedere il RASD” waits on “Dati del RASD”. Matías Rodríguez owns “Dati del RASD”.", True
    ) == "Dipendenza salvata: «Rivedere il RASD» aspetta «Dati del RASD». «Dati del RASD» è di Matías Rodríguez."
    assert localize("This card was already applied.", True) == "Questa card è già applicata."
    assert localize("Confirmed and saved.", False) == "Confirmed and saved."
    assert localize("A sentence nobody translated.", True) == "A sentence nobody translated."
