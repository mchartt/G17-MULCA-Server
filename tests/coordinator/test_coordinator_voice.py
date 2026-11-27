"""
The Discord voice call in the project (router._on_voice, /coordinator transcript).

Every spoken turn, once transcribed, is kept in the channel's log (Cadence
reads it as context), shown in the linked web space with a voice tag, and
listed by /coordinator transcript. Cards from spoken turns: the CS strategies
list the "audio" modality (test_coordinator_m3_strategies.py).
"""

from __future__ import annotations

from muca_server.protocol import CoordinatorMirrorMessage, CoordinatorNoticeMessage

from .conftest import drain
from .test_coordinator_links import DISCORD, DISCORD_ROSTER, Group, command, of_type, open_web_space


async def linked(tmp_path):
    group = Group(tmp_path)
    web, web_q = await open_web_space(group)
    discord, discord_q = group.router(DISCORD, DISCORD_ROSTER)
    group.links.link(DISCORD, WEB_CODE, member_id=9001)
    await discord.route_coordinator(command("state", 9001))
    await drain(discord_q)
    return group, web, web_q, discord, discord_q


WEB_CODE = "AB12CD"


async def test_a_spoken_turn_is_kept_shown_on_the_web_and_listed(tmp_path):
    _, web, web_q, discord, discord_q = await linked(tmp_path)
    await discord._on_voice(9001, "Chiara", "  I'll print the survey results by Friday  ", 0)
    await discord._on_voice(9002, "Dario", "", 0)  # nothing understood: nothing kept

    shown = of_type(await drain(web_q), CoordinatorMirrorMessage)
    assert [(m.channel, m.speaker_name, m.text) for m in shown] == [
        ("discord_voice", "Chiara", "I'll print the survey results by Friday")
    ]

    await discord.route_coordinator(command("transcript", 9002))
    (answer,) = of_type(await drain(discord_q), CoordinatorNoticeMessage)
    assert answer.ok
    assert answer.text.startswith("Voice call, last 60 minutes:")
    assert "Chiara: I'll print the survey results by Friday" in answer.text
    assert "(voice)" not in answer.text

    # Cadence's context: the line is in the channel's log, marked as spoken.
    lines = await discord._catchup.recent_lines(DISCORD)
    assert lines == ["Chiara (voice): I'll print the survey results by Friday"]


async def test_no_voice_yet_says_so(tmp_path):
    _, _, _, discord, discord_q = await linked(tmp_path)
    await discord.route_coordinator(command("transcript", 9001, minutes=30))
    (answer,) = of_type(await drain(discord_q), CoordinatorNoticeMessage)
    assert answer.ok and answer.text == "Nothing was said in the voice call in the last 30 minutes."


async def test_the_router_listens_to_the_conversations_transcripts(tmp_path):
    group = Group(tmp_path)
    discord, _ = group.router(DISCORD, DISCORD_ROSTER)
    discord._conversation.transcript_listeners = []

    async def started() -> None:
        return None

    discord._conversation.start = started
    await discord.start()
    assert discord._conversation.transcript_listeners == [discord._on_voice]
    await discord.checkin_timer.close()
