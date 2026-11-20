"""
LIVE end-to-end CS6 flow with the real model, through the production router
(CoordinatorRouter, store, catch-up log, ProgressMonitor, ask). Spends Azure
credit: run only with Matteo's OK.

    PYTHONPATH=. python scripts/live_cs6_flow.py path/to/.env [repeats]

Each scenario starts from a fresh group file:
  1. four tasks due tomorrow; the timer looks four times -> exactly ONE question
  2. the question is moved 5-9 hours into the past (as if nobody answered)
  3. the owner writes in the chat, without naming Cadence, hours later
  4. checks: an answer appears (or silence when the message is about something
     else), the progress recorded, tone, language, and the next message from
     the same member costs no model call once answered

What is NOT here: the Realtime judge (lab agent) and the web adapter; the
browser path of the same flow is proven offline (Playwright, SCRIPTED model).
Prints every message, PASS/FAIL, tokens and latency.
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiosqlite
from dotenv import dotenv_values

from muca_server.agents.openai.client_config import build_openai_client
from muca_server.cognition.coordination.catchup import CatchUp
from muca_server.cognition.coordination.router import CoordinatorRouter
from muca_server.cognition.coordination.store import CoordinationStore
from muca_server.cognition.coordination.writer import ProposalWriter
from muca_server.cognition.memory.sqlite import SqliteEpisodicStore, db_path_for
from muca_server.protocol import AssistantTextMessage
from tests.coordinator.conftest import FakeConversation

ROME = ZoneInfo("Europe/Rome")
BRUNO, MATIAS, CHIARA, MATTEO = 11, 12, 13, 14
ROSTER = {BRUNO: "Bruno", MATIAS: "Matías", CHIARA: "Chiara", MATTEO: "Matteo"}
ITALIAN = re.compile(r"\b(il|la|di|che|è|per|non|un|una|ma|ti|tu|come|ancora|grazie|bene|ok|va)\b", re.I)
BLAME = re.compile(r"colpa|dovevi|avresti dovuto|in ritardo per|promemoria|reminder|status update", re.I)

# (name, split, reply, expected status (None = silence and nothing recorded), hours later)
REPLIES = [
    ("on track", "dev", "Tutto bene, mi manca solo un ultimo controllo.", {"on_track"}, 5),
    ("on track, paraphrase", "holdout", "procede, stasera lo chiudo", {"on_track"}, 8),
    ("behind", "dev", "Sono indietro, non penso di riuscire a finire in tempo.", {"behind", "blocked"}, 6),
    ("stuck, paraphrase", "holdout", "sono fermo, aspetto ancora il file dal prof", {"blocked", "behind"}, 9),
    ("declined", "dev", "ne parliamo domani, ora non ho tempo", {"declined"}, 5),
    ("about something else", "holdout", "qualcuno ha gli appunti di ieri?", None, 7),
    ("done", "holdout", "fatto! l'ho appena caricato", {"done"}, 6),
]


class Tokens:
    def __init__(self) -> None:
        self.calls = 0
        self.prompt = 0
        self.completion = 0

    def __call__(self, _kind, _model, parts) -> None:
        self.calls += 1
        self.prompt += parts[0] + parts[1]
        self.completion += parts[4]


async def scenario(env, name: str, reply: str, expected, hours: int, tokens: Tokens) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    shown: list[str] = []
    workdir = Path(tempfile.mkdtemp(prefix="cs6-live-"))
    writer = ProposalWriter(build_openai_client(env), model=env.get("MUCA_COORDINATOR_MODEL", "gpt-4o"), record_cost=tokens)
    conversation_id = "web:LIVE"
    catchup = CatchUp(lambda cid: SqliteEpisodicStore(db_path_for(cid, base_dir=workdir)))
    queue: asyncio.Queue = asyncio.Queue()
    router = CoordinatorRouter(
        FakeConversation(conversation_id=conversation_id, roster=dict(ROSTER)),
        queue,
        store_for=lambda cid: CoordinationStore(db_path_for(cid, base_dir=workdir)),
        writer=writer,
        proposal_ttl_seconds=3600,
        catchup=catchup,
        progress_checkins=True,
        timezone="Europe/Rome",
    )
    store = CoordinationStore(db_path_for(conversation_id, base_dir=workdir))
    tomorrow = (datetime.now(ROME) + timedelta(days=1)).date().isoformat()
    later = (datetime.now(ROME) + timedelta(days=4)).date().isoformat()
    await store.add_task_by_hand("t1", "Dashboard", BRUNO, tomorrow)
    await store.add_task_by_hand("t2", "Dati per il RASD", MATIAS, tomorrow)
    await store.add_task_by_hand("t3", "Usability test", CHIARA, tomorrow)
    await store.add_task_by_hand("t4", "Slide finali", MATTEO, tomorrow)
    await store.add_task_by_hand("t5", "Scrivere il RASD", MATTEO, later, depends_on=["t2"])
    # A daytime "now" for the sweep (quiet hours would otherwise depend on when this runs).
    noon = datetime.now(ROME).replace(hour=15, minute=0, second=0, microsecond=0).timestamp()
    router.progress._clock = lambda: noon

    started = time.monotonic()
    sent = [await router.progress.sweep() for _ in range(4)]
    said = [m.text for m in await _drain(queue) if isinstance(m, AssistantTextMessage)]
    shown.append(f"sweeps {sent} ({time.monotonic() - started:.1f} s)")
    for text in said:
        shown.append(f"Cadence: {text}")
    if sent.count(True) != 1 or len(said) != 1:
        return [f"expected exactly one question, got {len(said)}"], shown
    question = said[0]
    (row,) = [r for r in await store.progress_checkins() if r.get("asked_at")]
    owner = row["member_id"]
    if ROSTER[owner] not in question:
        problems.append("the question does not name the owner")
    if not ITALIAN.search(question) or BLAME.search(question):
        problems.append("question not Italian, or pressure wording")
    if row["task_id"] != "t2":
        problems.append(f"asked about {row['task_id']}, not the task others wait on (t2)")

    # Nobody answered for hours: move the question into the past.
    async with aiosqlite.connect(db_path_for(conversation_id, base_dir=workdir)) as db:
        await db.execute("UPDATE coord_progress_checkin SET asked_at = ?", (time.time() - hours * 3600,))
        await db.commit()

    started = time.monotonic()
    await router.route_user_text(SimpleNamespace(text=reply, speaker=owner, speaker_name=ROSTER[owner]))
    await router.wait_for_background()
    answers = [m.text for m in await _drain(queue) if isinstance(m, AssistantTextMessage)]
    shown.append(f"{ROSTER[owner]} ({hours} h later): {reply}")
    shown.extend(f"Cadence: {a}" for a in answers)
    shown.append(f"({time.monotonic() - started:.1f} s)")
    (row,) = [r for r in await store.progress_checkins() if r["task_id"] == row["task_id"]]
    if expected is None:
        if answers or row["answer"]:
            problems.append(f"answered or recorded {row['answer']!r} for a message about something else")
    else:
        if len(answers) != 1:
            problems.append(f"expected one answer, got {len(answers)}")
        if row["answer"] not in expected:
            problems.append(f"recorded {row['answer']!r}, expected {sorted(expected)}")
        for a in answers:
            if not ITALIAN.search(a) or BLAME.search(a):
                problems.append("answer not Italian, or blame/pressure")
            if row["answer"] == "on_track" and "?" in a:
                problems.append("an on-track reply got another question")
        before = tokens.calls
        await router.route_user_text(SimpleNamespace(text="ok a dopo", speaker=owner, speaker_name=ROSTER[owner]))
        await router.wait_for_background()
        if tokens.calls != before:
            problems.append("a message after the answer was recorded still cost a model call")
    return problems, shown


async def _drain(queue: asyncio.Queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


async def main(env_path: str, repeats: int = 1) -> int:
    env = {k: v for k, v in dotenv_values(env_path).items() if v}
    tokens = Tokens()
    failed = total = 0
    started = time.monotonic()
    for name, split, reply, expected, hours in REPLIES:
        for n in range(repeats):
            total += 1
            problems, shown = await scenario(env, name, reply, expected, hours, tokens)
            failed += bool(problems)
            print(f"\n[{'PASS' if not problems else 'FAIL'}] {split} {name} #{n + 1}")
            for line in shown:
                print("  " + line)
            for p in problems:
                print(f"  !! {p}")
    print(f"\n{total - failed}/{total} passed in {time.monotonic() - started:.0f} s")
    print(f"tokens: {tokens.calls} calls, {tokens.prompt} prompt, {tokens.completion} completion "
          f"(~{tokens.prompt // max(tokens.calls, 1)} prompt per call)")
    return 1 if failed else 0


if __name__ == "__main__":
    a = sys.argv
    sys.exit(asyncio.run(main(a[1], int(a[2]) if len(a) > 2 else 1)))
