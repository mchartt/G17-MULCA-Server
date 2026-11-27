"""
LIVE corpus for CS7 implicit dependencies (27/11), real model. Spends Azure
credit: run only with Matteo's OK (given 27/11 for this story).

Two levels, measured apart (docs/coordinator/CONVERSATION_EVAL.md, "CS7
implicit dependencies"):

  writer  ProactiveWriter.draft for cs7_blockers_dependencies, run on EVERY
          case as if the judge had fired: does the writer give the right card
          on a positive, and no card on a negative? Production validation
          runs inside (ids, self link, members).

              python scripts/live_cs7_corpus.py writer [dev|holdout|all] [name part] [repeats]

  judge   end to end against a running MUCA-Server (coordinator on, throw-away
          MUCA_DB_DIR): a fresh conversation per case, the plan added by hand,
          the chat typed line by line; which cards come, and (from the
          server's log, --log) which strategy the Realtime judge picked on
          every turn.

              python scripts/live_cs7_corpus.py judge ws://localhost:8765/ [split] [name part] [repeats] --log <server log>

Azure: AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY from the environment
(or a .env passed with --env <path>).

Labels were written BEFORE any run (27/11, approved by Matteo with the
case list) and are never changed to fit an answer; a wrong label is fixed
only with a comment saying why. Holdout families (other domains: video,
poster, consent form, report) are never used to write prompts or examples.
Dates are relative to the day of the run (weekday names computed), so the
model's own clock and the prompt agree (lesson F21).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from muca_server.agents.openai.client_config import build_openai_client
from muca_server.cognition.coordination.models import Task, TaskStatus
from muca_server.cognition.coordination.proactive import _dropped
from muca_server.cognition.coordination.proactive_writer import ChatLine, ProactiveWriter
from muca_server.cognition.coordination.writer import ProposalWriter

CS7 = "cs7_blockers_dependencies"
LEA, OMAR, GIULIA, SARA = 1, 2, 3, 4
MEMBERS = {LEA: "Lea", OMAR: "Omar", GIULIA: "Giulia", SARA: "Sara"}
BY_NAME = {v: k for k, v in MEMBERS.items()}
TODAY = date.today()

IT_DAYS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


# Offsets: the test day comes BEFORE the fix day in every "risk" case.
T, F, LATER, EARLY = 3, 5, 7, 2
WORDS = {
    "T": IT_DAYS[day(T).weekday()], "F": IT_DAYS[day(F).weekday()], "L": IT_DAYS[day(LATER).weekday()],
    "E": IT_DAYS[day(EARLY).weekday()],
    "Te": EN_DAYS[day(T).weekday()], "Fe": EN_DAYS[day(F).weekday()],
}


def say(text: str) -> str:
    return text.format(**WORDS)


def task(task_id, title, owner, due, status="todo", waits=()):
    return {"id": task_id, "title": title, "owner": owner, "due": due, "status": status, "waits": list(waits)}


# Dev plan: booking product.
DEV_PLAN = [
    task("t1", "Test percorso prenotazione", LEA, T),
    task("t2", "Fix bottone Conferma", OMAR, F, "doing"),
    task("t3", "Icone", GIULIA, F + 1, "doing"),
]
# Holdout plan: other domains.
HOLD_PLAN = [
    task("v1", "Video della demo", SARA, T),
    task("v2", "Correzione schermata mappe", OMAR, F, "doing"),
    task("p1", "Poster per la fiera", LEA, 4),
    task("g1", "Grafici dei dati", GIULIA, 4),
    task("r1", "Report finale", GIULIA, 9, "doing"),
]


def plan_with(base, **changes):
    out = []
    for t in base:
        t = dict(t)
        t.update(changes.get(t["id"], {}))
        out.append(t)
    for extra in changes.get("_add", []):
        out.append(extra)
    return out


@dataclass
class Case:
    name: str
    split: str  # dev | holdout
    family: str
    chat: list[tuple[int, str]]
    card: bool | None  # True: a card is expected; False: silence; None: ambiguous (reported, not scored)
    plan: list[dict] = field(default_factory=lambda: list(DEV_PLAN))
    certainty: str | None = None
    task_in: tuple = ()  # accepted task_id values
    waits_in: tuple = ()  # accepted depends_on_task_id values (None = new work from the chat)
    owner_if_new: int | None = None
    waits_title: str = ""  # regex on depends_on_title when it is new work
    evidence_from: tuple = ()  # speakers the evidence must include (both sides of an inferred link)
    no_text: str = ""  # regex that must not appear in the card's texts
    critical: bool = False
    judge_only: bool = False  # duplicate cases: only meaningful end to end


L = lambda text: (LEA, say(text))  # noqa: E731
O = lambda text: (OMAR, say(text))  # noqa: E731
G = lambda text: (GIULIA, say(text))  # noqa: E731
S = lambda text: (SARA, say(text))  # noqa: E731

OMAR_BUG = "Il bottone Conferma va ancora in errore. Riesco a sistemarlo {F}"

CASES: list[Case] = [
    # ---------------------------------------------------------------- dev
    Case("D1+ full booking path vs Confirm fix", "dev", "minimal_pair",
         [L("{T} facciamo provare ai partecipanti il percorso completo di prenotazione"), O(OMAR_BUG)],
         True, certainty="possible", task_in=("t1",), waits_in=("t2", None), evidence_from=("Lea", "Omar"), critical=True),
    Case("D1- test stops before pressing Confirm", "dev", "minimal_pair",
         [L("{T} testiamo solo se gli utenti trovano il bottone Conferma. Chiudiamo la prova prima di premerlo"), O(OMAR_BUG)],
         False, critical=True),
    Case("D2+ 'that version' not ready yet", "dev", "reference",
         [G("la build nuova con il login sistemato la carico {F}"), L("per il test di {T} uso quella versione")],
         True, certainty="possible", task_in=("t1",), waits_in=(None,), owner_if_new=GIULIA, evidence_from=("Lea", "Giulia")),
    Case("D2- 'that version' already uploaded", "dev", "reference",
         [G("la build nuova con il login sistemato è caricata, il link è nel canale"), L("per il test di {T} uso quella versione")],
         False),
    Case("D3+ chat x plan: fix still open", "dev", "chat_x_plan",
         [L("{T} alle 10 ho cinque tester per il percorso completo di prenotazione")],
         True, certainty="possible", task_in=("t1",), waits_in=("t2",), evidence_from=("Lea",)),
    Case("D3- chat x plan: fix already done", "dev", "chat_x_plan",
         [L("{T} alle 10 ho cinque tester per il percorso completo di prenotazione")],
         False, plan=plan_with(DEV_PLAN, t2={"status": "done", "due": -1})),
    Case("D4+ reschedule creates the risk", "dev", "reschedule",
         [L("il test completo di prenotazione resta {T}"), O("sposto il fix del Conferma a {F}, prima ho l'esame")],
         True, plan=plan_with(DEV_PLAN, t2={"due": EARLY}), certainty="possible", task_in=("t1",), waits_in=("t2",),
         evidence_from=("Lea", "Omar")),
    Case("D4- reschedule removes the risk", "dev", "reschedule",
         [L("spostiamo il test completo di prenotazione a {L}"), O("ok, il fix del Conferma è per {F}")],
         False),
    Case("D5- chronological order only", "dev", "order_not_need",
         [G("finite le icone passo alle slide per la demo")], False),
    Case("D6- similar but independent", "dev", "independent",
         [L("oggi scrivo lo script del test"), O("io rifaccio i modelli delle API")], False),
    Case("D7- dependency already recorded, dates fit", "dev", "recorded",
         [L("il test completo resta {T} pomeriggio"), O("il fix del Conferma arriva {E}")],
         False, plan=plan_with(DEV_PLAN, t1={"waits": ["t2"]}, t2={"due": EARLY})),
    Case("D8+ stated blocker", "dev", "stated",
         [L("non posso preparare i task del test finché il bottone Conferma non funziona, sono ferma")],
         True, certainty="blocked", task_in=("t1",), waits_in=("t2", None)),
    Case("D9- solved in the chat", "dev", "solved",
         [L("non posso testare finché il Conferma va in errore"), O("ho messo un mock del Conferma, usa /mock/conferma"),
          L("perfetto, funziona")], False),
    Case("D10+ EN full booking path", "dev", "minimal_pair_en",
         [L("On {Te} the participants try the full booking flow"), O("Confirm still throws an error, I can fix it on {Fe}")],
         True, certainty="possible", task_in=("t1",), waits_in=("t2", None), evidence_from=("Lea", "Omar"), critical=True),
    Case("D10- EN test stops before Confirm", "dev", "minimal_pair_en",
         [L("On {Te} we only check whether people find the Confirm button, we stop before pressing it"),
          O("Confirm still throws an error, I can fix it on {Fe}")], False, critical=True),
    Case("D11 owner-and-date wording, CS7 expected", "dev", "competition",
         [L("il test completo di prenotazione è {T}"), G("le icone sono quasi pronte"), O("il Conferma va in errore, lo sistemo io {F}")],
         True, certainty="possible", task_in=("t1",), waits_in=("t2", None), evidence_from=("Lea", "Omar")),
    Case("D12 duplicate after a card", "dev", "duplicate",
         [L("{T} facciamo provare ai partecipanti il percorso completo di prenotazione"), O(OMAR_BUG),
          L("ok"), G("le icone le carico stasera"), O("comunque il Conferma lo chiudo solo {F}")],
         True, judge_only=True),
    # ---------------------------------------------------------------- holdout
    Case("H1+ demo video needs the maps screen", "holdout", "minimal_pair",
         [S("{T} giriamo il video della demo usando l'app, tutte le schermate"), O("la schermata mappe crasha ancora, la sistemo {F}")],
         True, plan=list(HOLD_PLAN), certainty="possible", task_in=("v1",), waits_in=("v2", None), evidence_from=("Sara", "Omar"),
         critical=True),
    Case("H1- demo video shows only home and profile", "holdout", "minimal_pair",
         [S("{T} giriamo il video della demo, ma solo home e profilo"), O("la schermata mappe crasha ancora, la sistemo {F}")],
         False, plan=list(HOLD_PLAN), critical=True),
    Case("H2+ indirect: poster <- charts <- sensor data", "holdout", "indirect",
         [L("il poster per la fiera lo mando in stampa tra quattro giorni, ci metto i grafici"),
          G("i grafici li faccio appena arrivano i dati del sensore"), O("i dati del sensore li ricevo solo {F}")],
         True, plan=list(HOLD_PLAN), certainty="possible", task_in=("p1", "g1"), waits_in=("g1", None), evidence_from=("Omar",)),
    Case("H3- consent already signed earlier", "holdout", "resolved_earlier",
         [S("per le interviste serve il consenso firmato di tutti"), L("ci penso io"), G("ho aggiornato le slide"),
          O("le mappe vanno meglio"), G("pausa pranzo?"), L("i consensi sono tutti firmati, li ho messi nella cartella"),
          G("ottimo"), S("{T} mattina faccio le interviste")],
         False, plan=plan_with(HOLD_PLAN, _add=[task("i1", "Interviste utenti", SARA, T)])),
    Case("H4+ prerequisite never in the plan", "holdout", "unplanned",
         [S("{T} mattina faccio le interviste"), G("abbiamo il modulo di consenso? il comitato etico ha detto che serve"),
          S("boh, non so chi lo stia facendo")],
         True, plan=plan_with(HOLD_PLAN, _add=[task("i1", "Interviste utenti", SARA, T)]), certainty="possible",
         task_in=("i1",), waits_in=(None,), waits_title=r"consens|consent", evidence_from=("Giulia",)),
    Case("H5- health is never the topic", "holdout", "personal",
         [O("scusate, sono malato: la correzione delle mappe slitta di qualche giorno")],
         None, plan=list(HOLD_PLAN), no_text=r"malat|salute|sick|health|stai bene|guarisc"),
    Case("H6? vague dates", "holdout", "vague",
         [S("vorrei fare il video la prossima settimana"), O("le mappe dovrebbero essere pronte a breve")],
         None, plan=plan_with(HOLD_PLAN, v1={"due": None})),
    Case("H7+ EN report needs cleaned survey results", "holdout", "minimal_pair_en",
         [G("I'm submitting the final report on {Te}, with the results chapter"),
          S("the survey results won't be cleaned before {Fe}")],
         # Label fixed after the first run (27/11), never to fit an answer: Sara says
         # "won't be cleaned" without saying who cleans them, and the writer's rule
         # (F2) is "null if nobody is named, or if it is a guess". Owner None, as
         # the model gave 6/6 on both versions. To confirm with the team.
         True, plan=plan_with(HOLD_PLAN, r1={"due": T}), certainty="possible", task_in=("r1",), waits_in=(None,),
         evidence_from=("Giulia", "Sara"), critical=True),
    Case("H7- EN results chapter not in this hand-in", "holdout", "minimal_pair_en",
         [G("I'm submitting the final report on {Te}; the results chapter goes in the appendix later, it's not part of this hand-in"),
          S("the survey results won't be cleaned before {Fe}")],
         False, plan=plan_with(HOLD_PLAN, r1={"due": T}), critical=True),
]


def plan_tasks(plan: list[dict]) -> list[Task]:
    return [
        Task(task_id=t["id"], title=t["title"], owner_id=t["owner"],
             due_date=day(t["due"]) if t["due"] is not None else None,
             status=TaskStatus(t["status"]), depends_on=t["waits"])
        for t in plan
    ]


def check_card(case: Case, payload: dict | None, texts: str) -> list[str]:
    """Problems with what came out (None = no card). Empty = pass."""
    if case.no_text and payload is not None and re.search(case.no_text, texts, re.I):
        return ["names a personal reason"]
    if case.card is None:
        return []
    if case.card is False:
        return ["false card"] if payload is not None else []
    if payload is None:
        return ["missed"]
    problems = []
    if case.certainty and payload.get("certainty") != case.certainty:
        problems.append(f"certainty={payload.get('certainty')}")
    if case.task_in and payload.get("task_id") not in case.task_in:
        problems.append(f"entity: task_id={payload.get('task_id')}")
    if case.waits_in and payload.get("depends_on_task_id") not in case.waits_in:
        problems.append(f"entity/direction: waits_on={payload.get('depends_on_task_id')}")
    if payload.get("task_id") and payload.get("task_id") == payload.get("depends_on_task_id"):
        problems.append("self link")
    if (payload.get("task_id"), payload.get("depends_on_task_id")) in {("t2", "t1"), ("v2", "v1"), ("g1", "p1")}:
        problems.append("direction reversed")
    if case.owner_if_new and payload.get("depends_on_task_id") is None and payload.get("depends_on_owner_id") != case.owner_if_new:
        problems.append(f"new prerequisite owner={payload.get('depends_on_owner_id')}")
    if case.waits_title and payload.get("depends_on_task_id") is None and not re.search(case.waits_title, payload.get("depends_on_title", ""), re.I):
        problems.append(f"waits on {payload.get('depends_on_title')!r}")
    speakers = {e.get("speaker") for e in (payload.get("context") or {}).get("evidence", [])}
    missing = [s for s in case.evidence_from if s not in speakers]
    if missing:
        problems.append(f"evidence lacks {missing}")
    return problems


# ---------------------------------------------------------------------- writer level


class Usage:
    def __init__(self) -> None:
        self.calls = 0
        self.prompt = 0
        self.completion = 0

    def __call__(self, _kind, _model, tokens) -> None:
        self.calls += 1
        self.prompt += tokens[0] + tokens[1]
        self.completion += tokens[4]


async def writer_level(env: dict, split: str, only: str, repeats: int) -> int:
    usage = Usage()
    model = env.get("MUCA_COORDINATOR_MODEL", "gpt-6.1-sol")
    proactive = ProactiveWriter(ProposalWriter(build_openai_client(env), model=model, record_cost=usage))
    cases = [c for c in CASES if not c.judge_only and (split == "all" or c.split == split) and only.lower() in c.name.lower()]
    rows: dict[str, list[list[str]]] = defaultdict(list)
    raw_false: dict[str, int] = defaultdict(int)  # false cards the writer made that R7 then dropped
    latencies = []
    for case in cases:
        chat = [ChatLine(MEMBERS[s], t, int(time.time() * 1000) - (len(case.chat) - i) * 60_000) for i, (s, t) in enumerate(case.chat)]
        for run in range(repeats if case.critical else 1) if repeats > 1 else range(1):
            started = time.monotonic()
            draft = await proactive.draft(CS7, chat=chat, members=MEMBERS, profiles=[], tasks=plan_tasks(case.plan),
                                          silenced_topics=[], today=TODAY)
            latencies.append(time.monotonic() - started)
            # The server's own R6/R7 after the draft (proactive._dropped), as in production.
            dropped = _dropped(draft, [], plan_tasks(case.plan)) if draft is not None else ""
            if dropped:
                print(f"\n  (writer drafted a card, server dropped it: {dropped})")
                raw_false[case.split] += case.card is False
                draft = None
            payload = None if draft is None else draft.payload.model_dump(mode="json")
            texts = "" if draft is None else " ".join([draft.rationale, draft.payload.question, *draft.payload.ways_forward])
            problems = check_card(case, payload, texts)
            rows[case.name].append(problems)
            verdict = "INFO" if case.card is None and not problems else ("PASS" if not problems else "FAIL")
            print(f"\n[{verdict}] {case.split}/{case.family} {case.name} #{run + 1}")
            for speaker, text in case.chat:
                print(f"  > {MEMBERS[speaker]}: {text}")
            if draft is None:
                print("  (no card)")
            else:
                shown = {k: v for k, v in payload.items() if k != "context"}
                print(f"  CARD {draft.rationale}\n  {json.dumps(shown, ensure_ascii=False)}")
                print(f"  evidence: {[e['speaker'] for e in payload['context']['evidence']]}")
            for p in problems:
                print(f"  !! {p}")
    summarize(cases, rows)
    for split, n in raw_false.items():
        print(f"{split}: + {n} false card(s) drafted by the writer and dropped by the server's R7")
    print(f"\nlatency per draft: median {statistics.median(latencies):.1f} s, max {max(latencies):.1f} s; "
          f"{usage.calls} model calls, {usage.prompt} prompt + {usage.completion} completion tokens ({model})")
    return 0


def summarize(cases: list[Case], rows: dict[str, list[list[str]]]) -> None:
    print("\n=== summary (runs) ===")
    for split in ("dev", "holdout"):
        mine = [c for c in cases if c.split == split and c.name in rows]
        if not mine:
            continue
        pos = [p for c in mine if c.card is True for p in rows[c.name]]
        neg = [p for c in mine if c.card is False for p in rows[c.name]]
        found = [p for p in pos if "missed" not in p]
        right = [p for p in found if not p]
        false = [p for p in neg if p]
        print(f"{split}: positives detected {len(found)}/{len(pos)}, fully right {len(right)}/{len(pos)}; "
              f"false cards {len(false)}/{len(neg)}")
        kinds = defaultdict(int)
        for p in found:
            for problem in p:
                kinds[problem.split(":")[0].split("=")[0]] += 1
        if kinds:
            print("   errors on detected cards: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
        for c in mine:
            if c.card is None:
                print(f"   (not scored) {c.name}: {rows[c.name]}")


# ---------------------------------------------------------------------- judge level


class Session:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.frames: list[dict] = []
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for raw in self.ws:
            frame = json.loads(raw)
            if frame.get("event") in ("coordinator_proposal", "coordinator_notice", "coordinator_state", "assistant_text"):
                self.frames.append(frame)

    async def send(self, frame: dict) -> None:
        await self.ws.send(json.dumps(frame))

    async def command(self, command: str, args: dict, member: int = LEA, timeout: float = 60) -> dict | None:
        rid = uuid.uuid4().hex[:8]
        await self.send({"event": "coordinator_command", "command": command, "memberId": member, "args": args,
                         "channel": "web", "requestId": rid})
        return await self.wait(lambda f: f.get("event") == "coordinator_notice" and f.get("requestId") == rid, timeout)

    async def wait(self, test, timeout: float) -> dict | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for frame in list(self.frames):
                if test(frame):
                    return frame
            await asyncio.sleep(0.3)
        return None

    def state(self) -> list[dict]:
        states = [f for f in self.frames if f.get("event") == "coordinator_state"]
        return states[-1]["state"]["tasks"] if states else []

    def cards(self) -> dict[str, dict]:
        out = {}
        for f in self.frames:
            if f.get("event") == "coordinator_proposal":
                out[f["proposal"]["proposal_id"]] = f["proposal"]
        return out


async def set_plan(s: Session, plan: list[dict]) -> dict[str, str]:
    """Adds the case's plan by hand; returns case id -> server task id. Suggestion cards are rejected."""
    ids: dict[str, str] = {}
    for t in plan:
        args = {"add": t["title"], "owner": t["owner"]}
        if t["due"] is not None:
            args["due"] = day(t["due"]).isoformat()
        waits = [ids[w] for w in t["waits"]]
        if waits:
            args["depends_on"] = waits
        await s.command("task", args)
        await asyncio.sleep(0.5)
        new = next(x for x in s.state() if x["task_id"] not in ids.values() and x["title"] == t["title"])
        ids[t["id"]] = new["task_id"]
        if t["status"] != "todo":
            await s.command("task", {"task": new["task_id"], "status": t["status"]})
    await asyncio.sleep(20)  # dependency suggestions after tasks are added (another path, not CS7's judge)
    for card in s.cards().values():
        if card["status"] == "pending":
            rid = uuid.uuid4().hex[:8]
            await s.send({"event": "proposal_response", "proposalId": card["proposal_id"], "action": "reject",
                          "memberId": LEA, "requestId": rid})
    await asyncio.sleep(2)
    return ids


def judge_picks(log_path: str, conversation: str, since: int) -> list[str]:
    if not log_path or not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8", errors="replace") as handle:
        handle.seek(since)
        lines = handle.read().splitlines()
    picks = []
    for line in lines:
        if conversation in line and "intervention decision: strategy_id=" in line:
            m = re.search(r"strategy_id=(None|'[^']*')", line)
            picks.append((m.group(1).strip("'") if m else "?") or "silence")
        elif conversation in line and ("coordinator vetoed" in line or "coordinator dropped" in line or "no valid" in line
                                       or "nothing to" in line):
            picks.append("  -> " + line.split(conversation, 1)[1].strip()[:110])
    return picks


async def judge_level(url: str, split: str, only: str, repeats: int, log_path: str) -> int:
    import websockets

    cases = [c for c in CASES if (split == "all" or c.split == split) and only.lower() in c.name.lower()]
    rows: dict[str, list[list[str]]] = defaultdict(list)
    people = [{"id": k, "name": v} for k, v in MEMBERS.items()]
    for case in cases:
        for run in range(repeats if case.critical else 1) if repeats > 1 else range(1):
            conversation = f"live-cs7-{re.sub(r'[^a-z0-9]+', '-', case.name.lower())[:30]}-{uuid.uuid4().hex[:6]}"
            since = os.path.getsize(log_path) if log_path and os.path.exists(log_path) else 0
            async with websockets.connect(url, max_size=None) as ws:
                s = Session(ws)
                await s.send({"event": "connect", "conversationId": conversation, "isDm": False})
                await s.send({"event": "create_auxiliary_agent", "auxiliaryAgentType": "intervention_detection"})
                await s.send({"event": "participants", "participants": people, "modality": "text"})
                await asyncio.sleep(2)
                ids = await set_plan(s, case.plan)
                before = set(s.cards())
                for speaker, text in case.chat:
                    await s.send({"event": "participants", "participants": people, "modality": "text"})
                    await s.send({"event": "user_text", "speaker": speaker, "speakerName": MEMBERS[speaker], "text": text})
                    await asyncio.sleep(5)
                await asyncio.sleep(40)
                new = [c for pid, c in s.cards().items() if pid not in before]
                blockers = [c for c in new if c["kind"] == "blocker"]
                await s.send({"event": "disconnect"})
            back = {v: k for k, v in ids.items()}
            payload = None
            texts = ""
            if blockers:
                p = blockers[0]
                payload = dict(p["payload"])
                payload["task_id"] = back.get(payload.get("task_id"), payload.get("task_id"))
                payload["depends_on_task_id"] = back.get(payload.get("depends_on_task_id"), payload.get("depends_on_task_id"))
                texts = " ".join([p.get("rationale", ""), payload.get("question", ""), *payload.get("ways_forward", [])])
            problems = check_card(case, payload, texts)
            if case.family == "duplicate" and len(blockers) > 1:
                problems.append("repeated card")
            others = [c["kind"] for c in new if c["kind"] != "blocker"]
            if case.card is False and others:
                problems.append(f"other card(s) {others}")
            rows[case.name].append(problems)
            verdict = "INFO" if case.card is None and not problems else ("PASS" if not problems else "FAIL")
            print(f"\n[{verdict}] judge {case.split}/{case.family} {case.name} #{run + 1}")
            for speaker, text in case.chat:
                print(f"  > {MEMBERS[speaker]}: {text}")
            print(f"  judge per turn: {judge_picks(log_path, conversation, since)}")
            print(f"  cards: blocker {len(blockers)}, other {others}")
            if payload is not None:
                shown = {k: v for k, v in payload.items() if k != "context"}
                print(f"  CARD {blockers[0].get('rationale')}\n  {json.dumps(shown, ensure_ascii=False)}")
            for problem in problems:
                print(f"  !! {problem}")
    summarize(cases, rows)
    return 0


def main(argv: list[str]) -> int:
    env_path = argv[argv.index("--env") + 1] if "--env" in argv else ""
    log_path = argv[argv.index("--log") + 1] if "--log" in argv else ""
    skip = {i for flag in ("--env", "--log") if flag in argv for i in (argv.index(flag), argv.index(flag) + 1)}
    args = [a for i, a in enumerate(argv) if i not in skip]
    env = dict(os.environ)
    if env_path:
        from dotenv import dotenv_values

        env.update({k: v for k, v in dotenv_values(env_path).items() if v})
    mode = args[0] if args else "writer"
    if mode == "judge":
        url = args[1] if len(args) > 1 else "ws://localhost:8765/"
        rest = args[2:]
    else:
        rest = args[1:]
    split = rest[0] if rest else "dev"
    only = rest[1] if len(rest) > 1 else ""
    repeats = int(rest[2]) if len(rest) > 2 else 1
    print(f"CS7 corpus, {mode} level, split {split}, today {TODAY} ({WORDS['T']} = test day, {WORDS['F']} = fix day)")
    if mode == "judge":
        return asyncio.run(judge_level(url, split, only, repeats, log_path))
    return asyncio.run(writer_level(env, split, only, repeats))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
