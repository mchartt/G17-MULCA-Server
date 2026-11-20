"""
LIVE end-to-end run of the RASD / Matías scenario against a running
MUCA-Server with the coordinator on and the real model. Spends Azure credit:
run only with Matteo's OK.

Start the server first, in its own shell, with the coordinator on and a
throw-away database folder (never the deployment's):

    MUCA_COORDINATOR_ENABLED=on MUCA_DB_DIR=<scratch> MUCA_SERVER_PORT=8765 \
        python -m muca_server.run_server        (with the .env loaded)

then:

    python scripts/live_rasd_e2e.py [ws://localhost:8765/] [--confirm]

What it does, like the web adapter (prototype/mulca_discord/web/hub.py):
a fresh conversation, three members, two tasks added by hand, a few chat
lines, then Matteo's two messages about Mathias' data. It prints every card
and notice the server sends, so each stage can be checked:

  1. does a CS7 card come (the intervention agent picked cs7, no rule vetoed it)?
  2. is its payload right (task = the RASD, the work it waits on = Matías'
     data, direction prerequisite -> RASD)?
  3. with --confirm: the card is confirmed and the saved state is printed
     (the Dependencies board draws exactly this);
  4. "Cadence, da cosa dipende adesso il RASD?" and the follow-up "E se
     Matías non riesce a mandarmi i dati entro domani?" through `ask`.

Exit code 0 only when every stage that ran passed.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import uuid

import websockets

URL = next((a for a in sys.argv[1:] if a.startswith("ws")), "ws://localhost:8765/")
CONFIRM = "--confirm" in sys.argv
MATTEO, MATIAS, BRUNO = 1, 2, 3
PEOPLE = [
    {"id": MATTEO, "name": "Matteo Cesandri"},
    {"id": MATIAS, "name": "Matías Rodríguez"},
    {"id": BRUNO, "name": "Bruno Neri"},
]
NAMES = {p["id"]: p["name"] for p in PEOPLE}
CHAT = [
    (BRUNO, "Ho caricato la prima bozza del mockup su Figma"),
    (MATTEO, "Bello, gli do un'occhiata stasera"),
    (BRUNO, "Domani provo a sistemare la navbar"),
    (MATTEO, "Io intanto devo chiudere il RASD entro il 24"),
    (MATTEO, "Mathias deve mandarmi prima però i dati su cui si basa il RASD."),
    (MATTEO, "Mathias DEVE MANDARMI PRIMA I DATI affinché io possa fare il RASD."),
]


class Session:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.frames: list[dict] = []
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for raw in self.ws:
            frame = json.loads(raw)
            if frame.get("event") in ("coordinator_proposal", "coordinator_notice", "coordinator_state", "assistant_text", "error"):
                self.frames.append(frame)

    async def send(self, frame: dict) -> None:
        await self.ws.send(json.dumps(frame))

    async def command(self, command: str, args: dict, member: int = MATTEO, timeout: float = 60) -> dict | None:
        rid = uuid.uuid4().hex[:8]
        await self.send({"event": "coordinator_command", "command": command, "memberId": member, "args": args,
                         "channel": "web", "requestId": rid})
        return await self.wait(lambda f: f.get("event") == "coordinator_notice" and f.get("requestId") == rid, timeout)

    async def wait(self, test, timeout: float) -> dict | None:
        end = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < end:
            for frame in self.frames[seen:]:
                if test(frame):
                    return frame
            seen = len(self.frames)
            await asyncio.sleep(0.3)
        return None

    def cards(self) -> list[dict]:
        return [f["proposal"] for f in self.frames if f.get("event") == "coordinator_proposal"]


ASK_MODE = "--ask" in sys.argv
DIRECT = "Cadence, Mathias deve mandarmi prima i dati su cui si basa il RASD: aggiungilo come prerequisito"


def show_state(s: Session) -> dict:
    states = [f for f in s.frames if f.get("event") == "coordinator_state"]
    tasks = states[-1]["state"]["tasks"] if states else []
    by_id = {t["task_id"]: t for t in tasks}
    print("\nSTATE (what the Dependencies board draws):")
    for t in tasks:
        print(f"  [{t['task_id']}] {t['title']} owner={NAMES.get(t.get('owner_id'))} status={t['status']} "
              f"waits_on={[by_id.get(d, {}).get('title', d) for d in t.get('depends_on', [])]} sources={t.get('dependency_sources')}")
    return by_id


async def main() -> int:
    problems: list[str] = []
    conversation = f"live-rasd-{'ask' if ASK_MODE else 'cs7'}-{int(time.time())}"
    async with websockets.connect(URL, max_size=None) as ws:
        s = Session(ws)
        await s.send({"event": "connect", "conversationId": conversation, "isDm": False})
        await s.send({"event": "create_auxiliary_agent", "auxiliaryAgentType": "intervention_detection"})
        await s.send({"event": "participants", "participants": PEOPLE, "modality": "text"})
        await asyncio.sleep(2)
        for args in (
            {"add": "Rivedere e correggere il RASD", "owner": MATTEO, "due": "2026-11-24"},
            {"add": "Create first mockup", "owner": BRUNO, "due": "2026-11-23"},
        ):
            notice = await s.command("task", args)
            print("task add ->", notice and notice["text"])
        await asyncio.sleep(15)  # a dependency suggestion after tasks are added may come (the 6.5 screenshot)
        for card in s.cards():
            print("\nCARD after tasks added:", json.dumps(card["payload"], ensure_ascii=False)[:600])
        before = len(s.cards())
        chat = CHAT[:4] if ASK_MODE else CHAT
        for speaker, text in chat:
            await s.send({"event": "participants", "participants": PEOPLE, "modality": "text"})
            await s.send({"event": "user_text", "speaker": speaker, "speakerName": NAMES[speaker], "text": text})
            print(f"> {NAMES[speaker]}: {text}")
            await asyncio.sleep(4)
        if ASK_MODE:
            notice = await s.command("ask", {"question": DIRECT.split(",", 1)[1].strip()}, timeout=60)
            print(f"> {DIRECT}\nCADENCE: {notice and notice['text']}")
        card = await s.wait(lambda f: f.get("event") == "coordinator_proposal" and f["proposal"]["kind"] == "blocker"
                            and f["proposal"]["status"] == "pending" and len(s.cards()) > before, 90)
        if card is None:
            problems.append("stage 1: no CS7 card")
            print("\n!! no CS7 card. Cards seen:", [c["kind"] for c in s.cards()])
        else:
            p = card["proposal"]
            pay = p["payload"]
            print("\nCARD:", json.dumps({"rationale": p["rationale"], **{k: v for k, v in pay.items() if k != "context"}},
                                        ensure_ascii=False, indent=1))
            if not pay.get("task_id"):
                problems.append("stage 2: the card does not point at the existing RASD task")
            if not (pay.get("depends_on_title") or pay.get("depends_on_task_id")):
                problems.append("stage 2: the card does not say what the RASD waits on")
            if pay.get("depends_on_owner_id") != MATIAS:
                problems.append(f"stage 2: the prerequisite's owner is {NAMES.get(pay.get('depends_on_owner_id'))}, not Matías")
            # The member answers the card in words first, as people do (the web adapter
            # offers a message typed right after a card to Cadence as "maybe").
            cards_before = len([c for c in s.cards() if c["status"] == "pending"])
            notice = await s.command("ask", {"question": "Sì, fallo.", "maybe": True}, timeout=60)
            said = (notice or {}).get("text", "")
            print(f"\n> Matteo Cesandri: Sì, fallo.\nCADENCE: {said or '(silent)'}")
            await asyncio.sleep(2)
            if len([c for c in s.cards() if c["status"] == "pending"]) > cards_before:
                problems.append("stage 2b: 'Sì, fallo' made a second card")
            if not re.search(r"card|pulsante|conferm", said, re.I):
                problems.append("stage 2b: 'Sì, fallo' did not point to the card")
            if re.search(r"ho (aggiunto|salvato|collegato)", said, re.I):
                problems.append("stage 2b: claims a change before the click")
            if CONFIRM and pay.get("task_id"):
                rid = uuid.uuid4().hex[:8]
                await s.send({"event": "proposal_response", "proposalId": p["proposal_id"], "action": "confirm",
                              "memberId": MATTEO, "channel": "web", "requestId": rid})
                notice = await s.wait(lambda f: f.get("event") == "coordinator_notice" and f.get("requestId") == rid, 30)
                print("confirm ->", notice and notice["text"])
        await s.command("state", {})
        by_id = show_state(s)
        rasd = next((t for t in by_id.values() if "RASD" in t["title"] and t.get("owner_id") == MATTEO), None)
        prereq = None
        if CONFIRM and card is not None:
            prereq = by_id.get((rasd or {}).get("depends_on", [None])[0] if rasd and rasd.get("depends_on") else None)
            if prereq is None:
                problems.append("stage 3: the RASD does not wait on a new task after the click")
            else:
                if prereq.get("owner_id") != MATIAS:
                    problems.append("stage 3: the prerequisite is not Matías'")
                if prereq.get("depends_on"):
                    problems.append("stage 3: the prerequisite waits on something (direction reversed?)")
        notice = await s.command("ask", {"question": "da cosa dipende adesso il RASD?"}, timeout=60)
        answer = (notice or {}).get("text", "")
        print(f"\n> Cadence, da cosa dipende adesso il RASD?\nCADENCE: {answer}")
        if not re.search(r"mat[ií]as|mathias", answer, re.I) or not re.search(r"dat", answer, re.I):
            problems.append("stage 4: the answer does not say the RASD waits on Matías' data")
        follow = "E se Matías non riesce a mandarmi i dati entro domani?"
        notice = await s.command("ask", {"question": follow, "maybe": True}, timeout=60)
        answer = (notice or {}).get("text", "")
        print(f"\n> {follow}\nCADENCE: {answer}")
        if not answer:
            problems.append("stage 5: the follow-up got no answer")
        if re.search(r"already working|sta già rispondendo", answer, re.I):
            problems.append("stage 5: the follow-up was refused as busy")
        if re.search(r"colpa|in ritardo per colpa|pigr|lazy", answer, re.I):
            problems.append("stage 5: blame")
        if prereq is not None:
            await s.command("task", {"task": prereq["task_id"], "status": "done"}, member=MATIAS)
            question = "il RASD aspetta ancora qualcosa?"
            notice = await s.command("ask", {"question": question}, timeout=60)
            answer = (notice or {}).get("text", "")
            print(f"\n> (Matías marks the data done) Cadence, {question}\nCADENCE: {answer}")
            if re.search(r"\b(ancora (in )?attesa|aspetta ancora i dati|still wait)", answer, re.I) and not re.search(r"\bnon\b", answer, re.I):
                problems.append("stage 6: an obsolete wait is presented as current")
        await s.send({"event": "disconnect"})
        s.reader.cancel()
    print("\n" + ("PASS" if not problems else "FAIL: " + "; ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
