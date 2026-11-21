"""
Linked groups: one project shared by a web space and its Discord / Slack channels.

Why this exists (DECISIONS.md, 16/11): web and Discord stay two conversations,
each with its own chat, its own session and its own episodic memory. What a
link shares is the *project*: plan, tasks, decisions and proposal cards. The
lab's files are not touched (no runtime.py, no single merged session) and no
accounts are linked: members keep the id their own channel gives them.

How:
  - GroupLinks remembers "channel:<id> belongs to web:<code>" in one small
    SQLite file next to the conversations' files. The web space is the
    group's *home*: its SQLite file holds the shared project state. A
    conversation that is not linked is its own home, so nothing changes for
    groups that never link.
  - LiveGroups knows which sessions of a group are open right now in this
    process, so a card or a new state reaches every linked channel (each
    session puts it on its own outbound queue; the client draws it as usual).

The coordination store already copes with two channels at once: every call
opens its own short connection and resolve_proposal is one transaction
("the first valid click wins", store.py).

GroupLinks uses the standard sqlite3 module: one row per link, read on a
session's first coordinator call and on link/unlink only, so a blocking call
of a few microseconds is simpler than an async one and safe here.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..memory.sqlite import db_path_for

LINKS_FILE = "coord_links.sqlite3"
WEB_PREFIX = "web:"
DISCORD_PREFIX = "channel:"
SLACK_PREFIX = "slack:"
# The channels that can be linked to a web space.
LINKABLE = (DISCORD_PREFIX, SLACK_PREFIX)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS coord_link (
    conversation_id  TEXT PRIMARY KEY,   -- the linked channel, e.g. channel:123
    home_id          TEXT NOT NULL,      -- the web space, e.g. web:AB12CD
    linked_by        INTEGER NOT NULL,   -- the member who ran the command
    linked_at        REAL NOT NULL
);
"""


def invite_code(text: str) -> str:
    """
    The invite code out of what a member typed: the code itself, or the
    whole invite link (".../join/AB12CD34EF"), with or without spaces, in
    any case. On 21/11 a pasted link got "Write the invite code": it is
    longer than a code, and nothing said why.
    """
    text = (text or "").strip().strip("<>\"'")
    if "/" in text:
        text = [part for part in text.split("?")[0].split("/") if part][-1] if text.strip("/") else ""
    return "".join(text.split()).replace("-", "").upper()


class LinkError(ValueError):
    """A link that cannot be made; the message is shown to the member."""


class GroupLinks:
    def __init__(self, db_dir: str | Path) -> None:
        self._db_dir = Path(db_dir)
        self._path = self._db_dir / LINKS_FILE

    def _connect(self) -> sqlite3.Connection:
        self._db_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._path)
        db.execute(_SCHEMA)
        return db

    def home(self, conversation_id: str) -> str:
        """The conversation whose file holds this group's project state."""
        with self._connect() as db:
            row = db.execute(
                "SELECT home_id FROM coord_link WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return row[0] if row else conversation_id

    def group(self, home_id: str) -> list[str]:
        """Every conversation of the group: the home first, then its links."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT conversation_id FROM coord_link WHERE home_id = ? ORDER BY linked_at",
                (home_id,),
            ).fetchall()
        return [home_id, *(r[0] for r in rows)]

    def link(self, conversation_id: str, code: str, *, member_id: int) -> str:
        """
        Link a Discord or Slack channel to the web space with this invite code and
        return the web space's conversation id. The invite code is the same
        secret that lets someone join the web space, so knowing it gives no
        more than joining would.
        """
        if not conversation_id.startswith(LINKABLE):
            raise LinkError("Run this in the Discord or Slack channel you want to link to the web space.")
        code = invite_code(code)
        if not code or len(code) > 20:
            raise LinkError(
                "Write the invite code of the web space: the 10 letters and digits shown "
                "in the web app, bottom left (or paste the invite link)."
            )
        home_id = WEB_PREFIX + code
        # A web space has a file as soon as its members have used it.
        if not db_path_for(home_id, base_dir=self._db_dir).exists():
            raise LinkError(
                "No web space with this invite code. Check the code, and open the space "
                "on the web once before linking it."
            )
        with self._connect() as db:
            db.execute(
                "INSERT INTO coord_link (conversation_id, home_id, linked_by, linked_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "home_id = excluded.home_id, linked_by = excluded.linked_by, "
                "linked_at = excluded.linked_at",
                (conversation_id, home_id, member_id, time.time()),
            )
        return home_id

    def unlink(self, conversation_id: str) -> str | None:
        """Remove this channel's link; returns the web space it had, if any."""
        with self._connect() as db:
            row = db.execute(
                "SELECT home_id FROM coord_link WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            db.execute("DELETE FROM coord_link WHERE conversation_id = ?", (conversation_id,))
        return row[0] if row else None

    def forget_home(self, home_id: str) -> None:
        """The web space was deleted: its links go with it."""
        with self._connect() as db:
            db.execute("DELETE FROM coord_link WHERE home_id = ?", (home_id,))


class LiveGroups:
    """The open sessions of each group, in this process (no disk)."""

    def __init__(self) -> None:
        self._by_home: dict[str, set[Any]] = {}

    def join(self, home_id: str, member: Any) -> None:
        self._by_home.setdefault(home_id, set()).add(member)

    def leave(self, home_id: str, member: Any) -> None:
        members = self._by_home.get(home_id)
        if members is None:
            return
        members.discard(member)
        if not members:
            del self._by_home[home_id]

    def of(self, home_id: str) -> Iterator[Any]:
        # A copy: a session may leave while we are sending.
        return iter(list(self._by_home.get(home_id, ())))
