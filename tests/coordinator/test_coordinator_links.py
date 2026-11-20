"""
Linked groups: a Discord channel linked to a web space shares the project
(plan, decisions, cards) and Catch up reads what the other channel said.
links.py, catchup.py and the router's link / unlink / catchup commands.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import aiosqlite
import pytest

from muca_server.cognition.coordination.catchup import CatchUp, summary_line
from muca_server.cognition.coordination.links import GroupLinks, LinkError, LiveGroups
from muca_server.cognition.coordination.router import CoordinatorRouter
from muca_server.cognition.coordination.store import CoordinationStore
from muca_server.cognition.coordination.writer import ProposalWriter
from muca_server.cognition.memory.records import EpisodicRecord
from muca_server.cognition.memory.sqlite import SqliteEpisodicStore, db_path_for
from muca_server.protocol import (
    CoordinatorMirrorMessage,
    CoordinatorNoticeMessage,
    CoordinatorProposalMessage,
    CoordinatorStateMessage,
    parse_incoming_message,
    serialize_outgoing_message,
)

from .conftest import GOOD_CHARTER, FakeChatClient, FakeConversation, drain

WEB = "web:AB12CD"
DISCORD = "channel:42"
WEB_ROSTER = {100_000_001: "Ada", 100_000_002: "Bruno"}
DISCORD_ROSTER = {9001: "Chiara", 9002: "Dario"}


def command(name, member, **args):
    return parse_incoming_message(
        {"event": "coordinator_command", "command": name, "memberId": member,
         "args": args, "requestId": "r1"}
    )


def click(proposal_id, member, action="confirm", channel="discord"):
    return parse_incoming_message(
        {"event": "proposal_response", "proposalId": proposal_id,
         "action": action, "memberId": member, "channel": channel, "requestId": "c1"}
    )


def of_type(messages, cls):
    return [m for m in messages if isinstance(m, cls)]


class Group:
    """A web space and a Discord channel on one server, as wiring.py builds them."""

    def __init__(self, db_dir, *answers, clock=None) -> None:
        self.db_dir = db_dir
        self.links = GroupLinks(db_dir)
        self.live = LiveGroups()
        self.catchup = CatchUp(
            lambda cid: SqliteEpisodicStore(db_path_for(cid, base_dir=db_dir)),
            **({"clock": clock} if clock else {}),
        )
        self.writer = ProposalWriter(FakeChatClient(*answers))

    def store_for(self, conversation_id) -> CoordinationStore:
        return CoordinationStore(db_path_for(conversation_id, base_dir=self.db_dir))

    def router(self, conversation_id, roster):
        queue: asyncio.Queue = asyncio.Queue()
        router = CoordinatorRouter(
            FakeConversation(conversation_id=conversation_id, roster=dict(roster)),
            queue,
            store_for=self.store_for,
            writer=self.writer,
            proposal_ttl_seconds=3600,
            today=lambda: date(2026, 11, 16),
            links=self.links,
            live=self.live,
            catchup=self.catchup,
        )
        return router, queue


async def open_web_space(group):
    """The web space exists once its members used it (its file is on disk)."""
    web, web_q = group.router(WEB, WEB_ROSTER)
    await web.route_coordinator(command("state", 100_000_001))
    await drain(web_q)
    return web, web_q


# ----------------------------------------------------------------------
# GroupLinks
# ----------------------------------------------------------------------


def test_a_conversation_that_is_not_linked_is_its_own_home(tmp_path):
    links = GroupLinks(tmp_path)
    assert links.home(DISCORD) == DISCORD
    assert links.group(WEB) == [WEB]


def test_only_a_discord_channel_links_and_only_to_an_existing_space(tmp_path):
    links = GroupLinks(tmp_path)
    with pytest.raises(LinkError):
        links.link("web:OTHER", "AB12CD", member_id=1)
    with pytest.raises(LinkError):
        links.link(DISCORD, "", member_id=1)
    with pytest.raises(LinkError, match="No web space"):
        links.link(DISCORD, "AB12CD", member_id=1)


def test_link_unlink_and_a_deleted_space(tmp_path):
    links = GroupLinks(tmp_path)
    db_path_for(WEB, base_dir=tmp_path).touch()
    assert links.link(DISCORD, " AB12CD ", member_id=9001) == WEB
    assert links.home(DISCORD) == WEB
    assert links.group(WEB) == [WEB, DISCORD]
    assert links.link("slack:C123", "AB12CD", member_id=7) == WEB
    assert links.group(WEB) == [WEB, DISCORD, "slack:C123"]
    assert links.unlink("slack:C123") == WEB
    assert links.unlink(DISCORD) == WEB
    assert links.unlink(DISCORD) is None
    links.link(DISCORD, "AB12CD", member_id=9001)
    links.forget_home(WEB)
    assert links.home(DISCORD) == DISCORD


def test_a_path_trick_in_the_code_stays_inside_the_data_folder(tmp_path):
    links = GroupLinks(tmp_path)
    with pytest.raises(LinkError):
        links.link(DISCORD, "../../etc", member_id=1)


# ----------------------------------------------------------------------
# The router: one project across two channels
# ----------------------------------------------------------------------


async def test_link_needs_the_feature_and_answers_the_member(tmp_path):
    group = Group(tmp_path)
    discord, q = group.router(DISCORD, DISCORD_ROSTER)
    await discord.route_coordinator(command("link", 9001, code="NOPE00"))
    (notice,) = await drain(q)
    assert not notice.ok and "No web space" in notice.text

    plain = CoordinatorRouter(
        FakeConversation(conversation_id=DISCORD, roster=DISCORD_ROSTER),
        q,
        store_for=group.store_for,
        writer=group.writer,
        proposal_ttl_seconds=3600,
    )
    await plain.route_coordinator(command("link", 9001, code="AB12CD"))
    (notice,) = await drain(q)
    assert not notice.ok and "not available" in notice.text


async def test_a_card_from_the_web_is_confirmed_on_discord(tmp_path):
    group = Group(tmp_path, GOOD_CHARTER)
    web, web_q = await open_web_space(group)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)

    await discord.route_coordinator(command("link", 9001, code="AB12CD"))
    sent = await drain(discord_q)
    assert of_type(sent, CoordinatorNoticeMessage)[0].ok
    assert of_type(sent, CoordinatorStateMessage)  # the shared state, at once
    assert of_type(await drain(web_q), CoordinatorStateMessage)  # the web follows too

    # A kickoff on the web: the card appears in both channels.
    await web.route_coordinator(command("kickoff", 100_000_001, text="Assignment"))
    await web.wait_for_background()
    (web_card,) = of_type(await drain(web_q), CoordinatorProposalMessage)
    (discord_card,) = of_type(await drain(discord_q), CoordinatorProposalMessage)
    assert web_card.proposal == discord_card.proposal

    # Chiara (Discord) confirms it: both channels redraw it and get the state.
    await discord.route_coordinator(click(web_card.proposal["proposal_id"], 9002))
    on_discord = await drain(discord_q)
    on_web = await drain(web_q)
    assert of_type(on_discord, CoordinatorNoticeMessage)[0].ok
    for sent in (on_discord, on_web):
        assert of_type(sent, CoordinatorProposalMessage)[0].proposal["status"] == "applied"
        assert of_type(sent, CoordinatorStateMessage)[-1].state["charter"]["confirmed_by"] == 9002

    # The state lives in the web space's file; Discord's own has no charter.
    assert (await group.store_for(WEB).load_state()).charter is not None
    assert (await group.store_for(DISCORD).load_state()).charter is None


async def test_a_web_member_cannot_click_through_the_discord_session(tmp_path):
    group = Group(tmp_path, GOOD_CHARTER)
    web, web_q = await open_web_space(group)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)
    await discord.route_coordinator(command("link", 9001, code="AB12CD"))
    await web.route_coordinator(command("kickoff", 100_000_001, text="Assignment"))
    await web.wait_for_background()
    (card,) = of_type(await drain(web_q), CoordinatorProposalMessage)
    await drain(discord_q)

    # The click must come from a member of the session it arrives on.
    await discord.route_coordinator(click(card.proposal["proposal_id"], 100_000_001))
    (notice,) = of_type(await drain(discord_q), CoordinatorNoticeMessage)
    assert not notice.ok


async def test_owners_can_be_members_of_the_other_channel(tmp_path):
    group = Group(tmp_path)
    web, _ = await open_web_space(group)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)
    await discord.route_coordinator(command("link", 9001, code="AB12CD"))
    await drain(discord_q)
    # Both rosters, this channel's members win on a clash of ids.
    assert discord._roster() == {**WEB_ROSTER, **DISCORD_ROSTER}


async def test_unlink_gives_the_channel_its_own_plan_back(tmp_path):
    group = Group(tmp_path, GOOD_CHARTER)
    web, web_q = await open_web_space(group)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)
    await discord.route_coordinator(command("link", 9001, code="AB12CD"))
    await discord.route_coordinator(command("unlink", 9001))
    sent = await drain(discord_q)
    assert [n.ok for n in of_type(sent, CoordinatorNoticeMessage)] == [True, True]
    await drain(web_q)

    await web.route_coordinator(command("kickoff", 100_000_001, text="Assignment"))
    await web.wait_for_background()
    assert of_type(await drain(web_q), CoordinatorProposalMessage)
    assert not await drain(discord_q)  # no longer the same group

    await discord.route_coordinator(command("unlink", 9001))
    (notice,) = await drain(discord_q)
    assert not notice.ok


async def test_a_closed_session_stops_receiving(tmp_path):
    group = Group(tmp_path, GOOD_CHARTER)
    web, web_q = await open_web_space(group)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)
    await discord.route_coordinator(command("link", 9001, code="AB12CD"))
    discord._leave_live()  # what close() does after the Conversation's own close
    await drain(discord_q)
    await web.route_coordinator(command("state", 100_000_001))
    assert not await drain(discord_q)


# ----------------------------------------------------------------------
# Catch up
# ----------------------------------------------------------------------


async def say(db_dir, conversation_id, speaker, content, at):
    store = SqliteEpisodicStore(db_path_for(conversation_id, base_dir=db_dir))
    await store.log(EpisodicRecord(
        timestamp=at, conversation_id=conversation_id, intervention_path="none",
        speaker=speaker, content=content,
    ))


async def add_decision(db_dir, at):
    store = CoordinationStore(db_path_for(WEB, base_dir=db_dir))
    await store.load_state()  # creates the tables
    async with aiosqlite.connect(db_path_for(WEB, base_dir=db_dir)) as db:
        await db.execute(
            "INSERT INTO coord_decision (decision_id, question, choice, reason, decided_by, decided_at, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("d1", "Which lab slot?", "Tuesday 17:00", "Everyone is free", 9001, at, "card p1"),
        )
        await db.commit()


async def test_catch_up_on_the_web_shows_the_web_chat_and_what_discord_said_and_decided(tmp_path):
    now = [1_000_000.0]
    group = Group(tmp_path, clock=lambda: now[0])
    web, web_q = await open_web_space(group)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)
    await discord.route_coordinator(command("link", 9001, code="AB12CD"))
    await drain(discord_q)
    await drain(web_q)

    await say(tmp_path, DISCORD, "Chiara", "Shall we book the lab for Tuesday?", now[0] - 3600)
    await say(tmp_path, DISCORD, "Dario", "Yes,   Tuesday 17:00 works", now[0] - 3000)
    await say(tmp_path, WEB, "Bruno", "I pushed the slides draft", now[0] - 2000)
    await say(tmp_path, "dm:9001", "Chiara", "a private message", now[0] - 1000)
    await add_decision(tmp_path, now[0] - 2900)

    await web.route_coordinator(command("catchup", 100_000_001))
    (notice,) = await drain(web_q)
    assert notice.ok and notice.request_id == "r1"
    # Matteo, 20/11: the group chat of this channel is part of the catch-up too.
    assert notice.text == "Since your last catch-up: 1 decision, 1 message here, 2 messages on Discord."
    result = notice.catchup
    assert [d["choice"] for d in result["decisions"]] == ["Tuesday 17:00"]
    assert result["here"]["channel"] == "web"
    assert [(m["speaker"], m["text"]) for m in result["here"]["messages"]] == [("Bruno", "I pushed the slides draft")]
    (place,) = result["elsewhere"]
    assert place["channel"] == "discord"
    assert [(m["speaker"], m["text"]) for m in place["messages"]] == [
        ("Chiara", "Shall we book the lab for Tuesday?"),
        ("Dario", "Yes, Tuesday 17:00 works"),
    ]
    # The wire form keeps the payload (exclude_none drops it only when absent).
    assert json.loads(serialize_outgoing_message(notice))["catchup"]["linked"] is True

    # The mark moved: asking again right after shows nothing new.
    now[0] += 60
    await web.route_coordinator(command("catchup", 100_000_001))
    (notice,) = await drain(web_q)
    assert notice.text == "Nothing new since your last catch-up."


async def test_catch_up_works_from_discord_too_and_without_a_link(tmp_path):
    group = Group(tmp_path)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)
    await discord.route_coordinator(command("catchup", 9001))
    (notice,) = await drain(discord_q)
    assert notice.ok and not notice.catchup["linked"]
    assert "not linked" in summary_line(notice.catchup)


async def test_a_peek_keeps_the_mark_until_the_member_closes_the_catch_up(tmp_path):
    """The web shows the catch-up, and moves the mark only when the member closes it."""
    now = [1_000_000.0]
    group = Group(tmp_path, clock=lambda: now[0])
    web, web_q = await open_web_space(group)
    await say(tmp_path, WEB, "Bruno", "Slides are in the drive", now[0] - 600)

    await web.route_coordinator(command("catchup", 100_000_001, peek=True))
    (notice,) = await drain(web_q)
    assert notice.catchup["here"]["total"] == 1
    now[0] += 60
    await web.route_coordinator(command("catchup", 100_000_001, peek=True))
    (notice,) = await drain(web_q)
    assert notice.catchup["here"]["total"] == 1  # opening it again still shows it

    await web.route_coordinator(command("catchup", 100_000_001, seen=True))
    (notice,) = await drain(web_q)
    assert notice.ok and notice.catchup is None
    now[0] += 60
    await web.route_coordinator(command("catchup", 100_000_001, peek=True))
    (notice,) = await drain(web_q)
    assert notice.catchup["here"]["total"] == 0


async def test_catch_up_lists_tasks_done_and_added_since_the_last_visit(tmp_path):
    group = Group(tmp_path)
    web, web_q = await open_web_space(group)
    await web.route_coordinator(command("task", 100_000_001, add="Write the survey"))
    await web.route_coordinator(command("task", 100_000_001, add="Book the lab"))
    await drain(web_q)
    store = CoordinationStore(db_path_for(WEB, base_dir=tmp_path))
    first = next(t for t in (await store.load_state()).tasks if t.title == "Write the survey")
    await store.edit_task(first.task_id, {"status": "done"})

    await web.route_coordinator(command("catchup", 100_000_001))
    notice = [m for m in await drain(web_q) if getattr(m, "catchup", None)][-1]
    assert [t["title"] for t in notice.catchup["tasks_done"]] == ["Write the survey"]
    assert [t["title"] for t in notice.catchup["tasks_added"]] == ["Book the lab"]
    assert "1 task done, 1 new task" in notice.text


# ----------------------------------------------------------------------
# The chat, mirrored
# ----------------------------------------------------------------------


def text(speaker, name, content):
    return parse_incoming_message(
        {"event": "user_text", "speaker": speaker, "speakerName": name, "text": content}
    )


async def test_a_message_is_shown_in_the_linked_channel_and_handled_once(tmp_path):
    group = Group(tmp_path)
    web, web_q = await open_web_space(group)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)
    await discord.route_coordinator(command("link", 9001, code="AB12CD"))
    await drain(discord_q)
    await drain(web_q)

    await discord.route_user_text(text(9001, "Chiara", "Lab on Tuesday?"))
    assert discord._conversation.texts == ["Lab on Tuesday?"]  # its own agent hears it
    assert not hasattr(web._conversation, "texts")  # the web agent does not
    (copy,) = await drain(web_q)
    assert isinstance(copy, CoordinatorMirrorMessage)
    assert (copy.channel, copy.speaker_name, copy.text) == ("discord", "Chiara", "Lab on Tuesday?")
    assert json.loads(serialize_outgoing_message(copy))["speakerName"] == "Chiara"
    assert not await drain(discord_q)  # no echo to the sender's channel


async def test_nothing_is_mirrored_without_a_link_or_from_a_dm(tmp_path):
    group = Group(tmp_path)
    web, web_q = await open_web_space(group)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)
    await discord.route_user_text(text(9001, "Chiara", "hello"))
    assert not await drain(web_q)

    await discord.route_coordinator(command("link", 9001, code="AB12CD"))
    await drain(web_q)
    discord._conversation.is_private = True
    await discord.route_user_text(text(9001, "Chiara", "secret"))
    assert not await drain(web_q)
