"""
The plan opener (story CS-OPEN-01, contract docs: CS_PLAN_OPENER.md): once the
charter is confirmed and the plan is empty, Cadence asks the group how it
wants to plan the work, listens, and sums the answers up in a card that a
member confirms. No real model: FakeChatClient answers; clock and sleep are fake.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date


from muca_server.cognition.coordination.opener import (
    ACKS,
    NO_CARD,
    is_question,
    MARK,
    MAX_OPEN_SECONDS,
    MIN_OPEN_SECONDS,
    QUIET_SECONDS,
    close_at,
)
from muca_server.cognition.coordination.proactive import Verdict
from muca_server.cognition.coordination.proactive_writer import ProactiveWriter
from muca_server.cognition.coordination.router import CoordinatorRouter
from muca_server.cognition.coordination.writer import ProposalWriter
from muca_server.cognition.ports import Utterance
from muca_server.protocol.coordinator import (
    CoordinatorNoticeMessage,
    CoordinatorProposalMessage,
)
from muca_server.protocol.incoming import parse_incoming_message
from muca_server.protocol.outgoing import AssistantTextMessage

from .conftest import GOOD_CHARTER, FakeChatClient, FakeConversation, drain

AGREED = {
    "outcome": "agreed",
    "topic": "how we work",
    "evidence": [1, 2],
    "rationale": "Stepping in because you said how you want to work.",
    "question": "",
    "summary": "Split by area, check in every Monday after the lab.",
    "agreements": [
        {"text": "Split by area: Ada the API, Bruno the dashboard", "evidence": [1]},
        {"text": "Check in every Monday after the lab", "evidence": [2]},
    ],
    "open_points": ["Where to keep the slides"],
    "options": [],
}
CONFLICT = {
    "outcome": "conflict",
    "topic": "how tasks are assigned",
    "evidence": [1, 2],
    "rationale": "Stepping in because two ways of assigning tasks were proposed.",
    "question": "How do we assign the tasks?",
    "summary": "",
    "agreements": [],
    "open_points": [],
    "options": [
        {"label": "Everyone picks from a list", "argument": "Ada: everyone picks"},
        {"label": "One person assigns", "argument": "Bruno: one person assigns"},
    ],
}
NOTHING = {**AGREED, "outcome": "none", "agreements": [], "summary": "", "open_points": []}


@dataclass
class TalkingConversation(FakeConversation):
    """The three public Conversation members the opener uses, recorded."""

    is_answering: bool = False
    told: list[str] = field(default_factory=list)
    idle_waits: int = 0

    async def tell_observers(self, text: str) -> None:
        self.told.append(text)

    async def wait_until_idle(self) -> None:
        self.idle_waits += 1
        self.is_answering = False


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)
        await asyncio.sleep(0)


def make_router(store, *answers, conversation=None, proactive=False):
    queue: asyncio.Queue = asyncio.Queue()
    writer = ProposalWriter(FakeChatClient(*answers))
    router = CoordinatorRouter(
        conversation or TalkingConversation(),
        queue,
        store_for=lambda conversation_id: store,
        writer=writer,
        proposal_ttl_seconds=3600,
        today=lambda: date(2026, 11, 20),
        proactive_writer=ProactiveWriter(ProposalWriter(FakeChatClient())) if proactive else None,
    )
    clock = Clock()
    router.opener.clock = clock
    router.opener.sleep = clock.sleep
    return router, queue, clock


def command(name, member=1, **args):
    return parse_incoming_message(
        {"event": "coordinator_command", "command": name, "memberId": member,
         "args": args, "requestId": "r1", "channel": "web"}
    )


def click(proposal_id, action="confirm", member=1, option=None):
    data = {"event": "proposal_response", "proposalId": proposal_id, "action": action,
            "memberId": member, "channel": "web", "requestId": "c1"}
    if option is not None:
        data["option"] = option
    return parse_incoming_message(data)


def said(member, name, text):
    return parse_incoming_message({"event": "user_text", "speaker": member, "speakerName": name, "text": text})


def of_type(messages, cls):
    return [m for m in messages if isinstance(m, cls)]


async def confirm_charter(router, queue) -> None:
    await router.route_coordinator(command("kickoff", text="Assignment"))
    await router.wait_for_background()
    (card,) = of_type(await drain(queue), CoordinatorProposalMessage)
    await router.route_coordinator(click(card.proposal["proposal_id"], member=2))
    await router.opener.wait()


async def answer(router, queue, *lines) -> list:
    """The group answers the question; the window closes; what was sent."""
    await drain(queue)
    for member, name, text in lines:
        await router.route_user_text(said(member, name, text))
    await router.opener.wait()
    return await drain(queue)


# ----------------------------------------------------------------------
# When the question is asked
# ----------------------------------------------------------------------


async def test_a_confirmed_charter_with_an_empty_plan_gets_the_question_once(store):
    router, queue, clock = make_router(store, GOOD_CHARTER, GOOD_CHARTER)
    await confirm_charter(router, queue)
    sent = await drain(queue)
    (question,) = of_type(sent, AssistantTextMessage)
    assert question.is_final and question.modality == "text"
    assert "How do you want to plan the work?" in question.text
    # The question comes after the applied card and the state, not before.
    assert isinstance(sent[-1], AssistantTextMessage)
    assert router._conversation.told and "How do you want to plan the work?" in router._conversation.told[0]
    assert await store.mark(MARK) is not None
    assert router.opener.listening

    # A second charter (edited, confirmed again) does not ask again.
    clock.now += MAX_OPEN_SECONDS + 1
    await confirm_charter(router, queue)
    assert of_type(await drain(queue), AssistantTextMessage) == []


async def test_no_question_when_the_plan_already_has_tasks(store):
    router, queue, _ = make_router(store, GOOD_CHARTER)
    # A task added by hand before the charter: the group has already started planning.
    await router.route_coordinator(command("task", add="Build the API", owner="", due=""))
    await router.wait_for_background()
    assert len((await store.load_state()).tasks) == 1
    await confirm_charter(router, queue)
    assert of_type(await drain(queue), AssistantTextMessage) == []
    assert not router.opener.listening


async def test_never_in_a_direct_message(store):
    router, queue, _ = make_router(store, GOOD_CHARTER, conversation=TalkingConversation(is_private=True))
    await router.opener.ask(channel="discord", auto=True)
    await router.opener.wait()
    assert of_type(await drain(queue), AssistantTextMessage) == []


async def test_the_question_waits_until_the_agent_has_finished_answering(store):
    talking = TalkingConversation(is_answering=True)
    router, queue, _ = make_router(store, GOOD_CHARTER, conversation=talking)
    await confirm_charter(router, queue)
    assert talking.idle_waits == 1
    assert len(of_type(await drain(queue), AssistantTextMessage)) == 1


async def test_the_opener_never_starts_r4s_five_minutes(store):
    """Decision of Matteo, 19/11: R8 only. The question and its card leave R4 alone,
    so CS5/CS7 may step in as soon as the group has answered (demo: kickoff, then CS5)."""
    router, queue, _ = make_router(store, GOOD_CHARTER, AGREED)
    await confirm_charter(router, queue)
    assert await store.mark("proactive") is None
    await answer(router, queue, (1, "Ada", "Split by area."), (2, "Bruno", "Monday check-ins."))
    assert await store.mark("proactive") is None


async def test_plan_together_asks_on_demand_even_after_the_first_time(store):
    router, queue, clock = make_router(store, GOOD_CHARTER)
    await confirm_charter(router, queue)
    clock.now += MAX_OPEN_SECONDS + 1
    await drain(queue)

    await router.route_coordinator(command("approach"))
    await router.opener.wait()
    sent = await drain(queue)
    assert of_type(sent, CoordinatorNoticeMessage)[0].ok
    assert len(of_type(sent, AssistantTextMessage)) == 1

    # Asked again while listening: a notice, no second question.
    await router.route_coordinator(command("approach"))
    await router.opener.wait()
    sent = await drain(queue)
    assert not of_type(sent, CoordinatorNoticeMessage)[0].ok
    assert of_type(sent, AssistantTextMessage) == []


async def test_plan_together_from_a_non_member_is_refused(store):
    router, queue, _ = make_router(store)
    await router.route_coordinator(command("approach", member=77))
    await router.opener.wait()
    sent = await drain(queue)
    assert not of_type(sent, CoordinatorNoticeMessage)[0].ok
    assert of_type(sent, AssistantTextMessage) == []


# ----------------------------------------------------------------------
# The listening window
# ----------------------------------------------------------------------


def test_the_window_has_a_minimum_a_quiet_time_and_a_cap():
    asked = 0.0
    assert close_at(asked, None) == MAX_OPEN_SECONDS  # nobody answers: it just ends
    assert close_at(asked, 2.0) == MIN_OPEN_SECONDS  # an early answer still waits for the minimum
    assert close_at(asked, 100.0) == 100.0 + QUIET_SECONDS  # each answer restarts the quiet time
    assert close_at(asked, MAX_OPEN_SECONDS - 1) == MAX_OPEN_SECONDS  # never past the cap


async def test_only_members_count_and_nothing_after_the_window(store):
    router, queue, clock = make_router(store, GOOD_CHARTER, AGREED)
    await confirm_charter(router, queue)
    sent = await answer(
        router, queue,
        (77, "Stranger", "Let's do everything on Fridays"),
        (1, "Ada", "Let's split by area: I take the API, Bruno the dashboard."),
        (2, "Bruno", "Ok, and a check-in every Monday after the lab."),
    )
    (card,) = of_type(sent, CoordinatorProposalMessage)
    quotes = [e["text"] for e in card.proposal["payload"]["context"]["evidence"]]
    assert all("Fridays" not in q for q in quotes)
    assert not router.opener.listening

    await router.route_user_text(said(1, "Ada", "One more thing"))
    await router.opener.wait()
    assert of_type(await drain(queue), CoordinatorProposalMessage) == []


# ----------------------------------------------------------------------
# The card
# ----------------------------------------------------------------------


async def test_agreed_answers_become_an_approach_card_grounded_on_the_chat(store):
    router, queue, _ = make_router(store, GOOD_CHARTER, AGREED)
    await confirm_charter(router, queue)
    sent = await answer(
        router, queue,
        (1, "Ada", "Let's split by area: I take the API, Bruno the dashboard."),
        (2, "Bruno", "Ok, and a check-in every Monday after the lab."),
    )
    (card,) = of_type(sent, CoordinatorProposalMessage)
    proposal = card.proposal
    assert proposal["kind"] == "approach" and proposal["status"] == "pending"
    payload = proposal["payload"]
    assert payload["question"] == "How the group plans the work"  # set by the server, not the model
    assert payload["agreements"] == ["Split by area: Ada the API, Bruno the dashboard", "Check in every Monday after the lab"]
    assert payload["open_points"] == ["Where to keep the slides"]
    evidence = payload["context"]["evidence"]
    assert [e["speaker"] for e in evidence] == ["Ada", "Bruno"]
    assert evidence[0]["text"] == "Let's split by area: I take the API, Bruno the dashboard."  # copied, not written
    assert payload["context"]["strategy_id"] == "plan_opener"


async def test_the_question_is_never_a_chat_line_the_model_can_cite(store):
    router, queue, _ = make_router(store, GOOD_CHARTER, AGREED)
    await confirm_charter(router, queue)
    await answer(router, queue, (1, "Ada", "Split by area."), (2, "Bruno", "Monday check-ins."))
    call = router._writer._client.calls[-1]  # noqa: SLF001
    user = call["messages"][1]["content"]
    chat = user.split("<chat>")[1]
    assert "How do you want to plan the work?" not in chat
    assert "[1] Ada: Split by area." in chat


async def test_different_answers_become_a_decision_card_with_the_options(store):
    router, queue, _ = make_router(store, GOOD_CHARTER, CONFLICT)
    await confirm_charter(router, queue)
    sent = await answer(router, queue, (1, "Ada", "Everyone picks from a list."), (2, "Bruno", "One person assigns."))
    (card,) = of_type(sent, CoordinatorProposalMessage)
    assert card.proposal["kind"] == "decision"
    assert [o["label"] for o in card.proposal["payload"]["options"]] == ["Everyone picks from a list", "One person assigns"]
    assert card.proposal["payload"]["context"]["strategy_id"] == "plan_opener"


async def test_answers_about_something_else_give_no_card(store):
    router, queue, _ = make_router(store, GOOD_CHARTER, NOTHING)
    await confirm_charter(router, queue)
    sent = await answer(router, queue, (1, "Ada", "Which font do we use?"))
    assert of_type(sent, CoordinatorProposalMessage) == []


async def test_an_invalid_draft_is_retried_once_then_silence(store):
    bad = {**AGREED, "agreements": [{"text": "Invented", "evidence": [9]}]}
    router, queue, _ = make_router(store, GOOD_CHARTER, bad, bad)
    await confirm_charter(router, queue)
    sent = await answer(router, queue, (1, "Ada", "Split by area."))
    assert of_type(sent, CoordinatorProposalMessage) == []
    assert len(router._writer._client.calls) == 3  # noqa: SLF001 - charter + two attempts


async def test_a_model_error_never_breaks_the_chat(store):
    router, queue, _ = make_router(store, GOOD_CHARTER, RuntimeError("down"), RuntimeError("down"))
    await confirm_charter(router, queue)
    sent = await answer(router, queue, (1, "Ada", "Split by area."))
    assert of_type(sent, CoordinatorProposalMessage) == []
    assert router._conversation.texts[-1] == "Split by area."  # the message itself went through


# ----------------------------------------------------------------------
# The click
# ----------------------------------------------------------------------


async def test_confirm_saves_the_approach_in_the_decision_log_with_its_source(store):
    router, queue, _ = make_router(store, GOOD_CHARTER, AGREED)
    await confirm_charter(router, queue)
    sent = await answer(router, queue, (1, "Ada", "Split by area."), (2, "Bruno", "Monday check-ins."))
    proposal_id = of_type(sent, CoordinatorProposalMessage)[0].proposal["proposal_id"]

    await router.route_coordinator(click(proposal_id, member=77))  # not a member
    assert (await store.load_state()).decisions == []

    await router.route_coordinator(click(proposal_id, member=3))
    (decision,) = (await store.load_state()).decisions
    assert decision.question == "How the group plans the work"
    assert decision.choice == "Split by area, check in every Monday after the lab."
    assert "Check in every Monday after the lab" in decision.reason
    assert decision.decided_by == 3 and decision.source == f"proposal:{proposal_id}"

    await drain(queue)
    await router.route_coordinator(click(proposal_id, member=1))  # second click
    notice = of_type(await drain(queue), CoordinatorNoticeMessage)[0]
    assert not notice.ok
    assert len((await store.load_state()).decisions) == 1


async def test_not_now_closes_the_card_and_changes_nothing(store):
    router, queue, _ = make_router(store, GOOD_CHARTER, AGREED)
    await confirm_charter(router, queue)
    sent = await answer(router, queue, (1, "Ada", "Split by area."), (2, "Bruno", "Monday check-ins."))
    proposal_id = of_type(sent, CoordinatorProposalMessage)[0].proposal["proposal_id"]
    await router.route_coordinator(click(proposal_id, action="not_now", member=2))
    card = of_type(await drain(queue), CoordinatorProposalMessage)[0]
    assert card.proposal["status"] == "snoozed"
    assert (await store.load_state()).decisions == []


# ----------------------------------------------------------------------
# No other unsolicited card while the group answers
# ----------------------------------------------------------------------


async def test_proactive_cards_are_vetoed_while_the_opener_listens(store):
    router, queue, clock = make_router(store, GOOD_CHARTER, proactive=True)
    await confirm_charter(router, queue)
    history = [Utterance(speaker_id=1, speaker_name="Ada", content="I take the API", timestamp_ms=0)] * 2
    verdict = await router.proactive.offer("cs5_owner_and_date", history=history, message_count=500, assistant_id=0)
    assert verdict is Verdict.VETOED

    clock.now += MAX_OPEN_SECONDS + 1  # the window is over: no rule says no any more (no R4 from the opener)
    assert not router.opener.listening
    assert router.proactive._veto(history, 500, 0) == ""  # noqa: SLF001


# ----------------------------------------------------------------------
# Live test 21/11: language of the question, and no silent minute
# ----------------------------------------------------------------------

ITALIAN_CHARTER = {
    **GOOD_CHARTER,
    "goal": "Progettare e prototipare un'app per coordinare i gruppi di studenti.",
    "deliverables": ["RASD", "Prototipo interattivo", "Demo finale"],
    "rationale": "Intervengo perché le scadenze sono vicine.",
}


async def test_an_italian_charter_gets_the_question_in_italian(store):
    router, queue, _ = make_router(store, ITALIAN_CHARTER)
    await confirm_charter(router, queue)
    (question,) = of_type(await drain(queue), AssistantTextMessage)
    assert question.text.startswith("Il charter è confermato.")


async def test_a_chosen_language_wins_over_the_charter(store):
    router, queue, _ = make_router(store, ITALIAN_CHARTER)
    await store.save_setting("language", "en")
    await confirm_charter(router, queue)
    (question,) = of_type(await drain(queue), AssistantTextMessage)
    assert "How do you want to plan the work?" in question.text


async def test_the_first_answer_is_acknowledged_once_in_the_groups_language(store):
    router, queue, _ = make_router(store, ITALIAN_CHARTER, AGREED)
    await confirm_charter(router, queue)
    sent = await answer(
        router, queue,
        (1, "Ada", "Dividiamo i compiti il lunedì."),
        (2, "Bruno", "E decidiamo in chat."),
    )
    texts = [m.text for m in of_type(sent, AssistantTextMessage)]
    assert texts == [ACKS["it"]]
    # The ack comes before the card.
    first_card = next(i for i, m in enumerate(sent) if isinstance(m, CoordinatorProposalMessage))
    assert sent.index(of_type(sent, AssistantTextMessage)[0]) < first_card


async def test_no_ack_when_nobody_answers(store):
    router, queue, clock = make_router(store, GOOD_CHARTER)
    await confirm_charter(router, queue)
    await drain(queue)
    clock.now += MAX_OPEN_SECONDS + 1
    await router.route_user_text(said(1, "Ada", "Late answer"))
    await router.opener.wait()
    assert of_type(await drain(queue), AssistantTextMessage) == []


async def test_a_question_to_the_group_gets_no_i_am_listening_line(store):
    # Live test 21/11: "qualcuno sa se il prof ha caricato le slide?" got "Ricevuto".
    # The line comes once, for the first message that is not a question
    # (a window with questions only stays silent: see the last test below).
    router, queue, _ = make_router(store, ITALIAN_CHARTER, AGREED)
    await confirm_charter(router, queue)
    sent = await answer(
        router, queue,
        (1, "Ada", "qualcuno sa se il prof ha caricato le slide di ieri?"),
        (2, "Bruno", "Dividiamo i compiti il lunedì."),
        (1, "Ada", "E decidiamo in chat."),
    )
    assert [m.text for m in of_type(sent, AssistantTextMessage)] == [ACKS["it"]]


async def test_the_line_says_cadence_listens_not_that_an_answer_was_received():
    for text in ACKS.values():
        assert "Ricevuto" not in text and "Got it" not in text


async def test_when_no_card_follows_cadence_says_so_and_how_to_ask_again(store):
    nothing = {**AGREED, "outcome": "none", "agreements": [], "open_points": []}
    router, queue, _ = make_router(store, ITALIAN_CHARTER, nothing)
    await confirm_charter(router, queue)
    sent = await answer(router, queue, (1, "Ada", "oii che dobbiamo fare"))
    assert [m.text for m in of_type(sent, AssistantTextMessage)] == [ACKS["it"], NO_CARD["it"]]
    assert of_type(sent, CoordinatorProposalMessage) == []


async def test_only_questions_and_no_card_means_silence(store):
    nothing = {**AGREED, "outcome": "none", "agreements": [], "open_points": []}
    router, queue, _ = make_router(store, ITALIAN_CHARTER, nothing)
    await confirm_charter(router, queue)
    sent = await answer(router, queue, (1, "Ada", "a che ora è la lezione domani?"))
    assert of_type(sent, AssistantTextMessage) == []


def test_a_question_is_recognised_without_a_question_mark_but_an_answer_is_not():
    for text in ("ok good, whats the goal", "what is the deadline", "quando è la consegna", "scusate, chi fa le slide",
                 "a che ora è la lezione?", "why for a minute only"):
        assert is_question(text), text
    for text in ("Dividiamo i compiti il lunedì.", "E decidiamo in chat.", "e come sempre decidiamo in chat",
                 "ok, facciamo il punto il giovedì", "per me va bene"):
        assert not is_question(text), text


def test_the_lines_do_not_say_listen_or_hear():
    # Slack, 21/11: "hear???? im just writing".
    for text in [*ACKS.values(), *NO_CARD.values()]:
        assert "listen" not in text.lower() and "hear" not in text.lower() and "ascolt" not in text.lower()


async def test_a_question_to_cadence_while_the_window_is_open_is_answered_not_silenced(store):
    # Slack, 21/11: "ok good, whats the goal" right after Cadence's reply got the opener's line instead of the goal.
    answer_json = {"answer": "The goal is to write a paper about AI and education.", "task_ids": [], "milestone_ids": [],
                   "decision_ids": [], "documents": [],
                   "action": {"kind": "none", "task_id": None, "title": "", "owner_id": None, "due_date": None,
                              "depends_on_task_id": None, "depends_on_title": "", "depends_on_owner_id": None}}
    router, queue, _ = make_router(store, GOOD_CHARTER, answer_json)
    await confirm_charter(router, queue)
    await drain(queue)
    assert router.opener.listening
    await router.route_coordinator(command("ask", member=1, question="ok good, whats the goal", maybe=True))
    await router.wait_for_background()
    notices = of_type(await drain(queue), CoordinatorNoticeMessage)
    assert notices and notices[-1].text.startswith("The goal is to write a paper")
    # A line that is not a question is still the opener's: silence from the ask, no model call.
    await router.route_coordinator(command("ask", member=1, question="dividiamo i compiti il lunedì", maybe=True))
    await router.wait_for_background()
    notices = of_type(await drain(queue), CoordinatorNoticeMessage)
    assert notices and notices[-1].text == ""
