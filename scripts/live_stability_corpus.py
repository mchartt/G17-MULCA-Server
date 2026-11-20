"""
LIVE corpus for conversational stability and CS6 progress check-ins (20/11),
with the real model. Spends Azure credit: run only with Matteo's OK.

    python scripts/live_stability_corpus.py path/to/.env [dev|holdout|all] [name part] [repeats]

Holdout cases were written together with the dev ones, before any prompt
iteration, and are never used to tune: only to measure.

Two production paths:
  ask       PlanWriter.answer_question: casual talk, language, project facts,
            replies to a progress question (the `progress` field included)
  question  PlanWriter.draft_progress_question: the CS6 question itself, or
            silence when the chat already gave the status

Checks are deterministic on structure (silence, progress status) and
pattern-based on text (language, redirects, disclaimers, blame). Prints every
answer, PASS/FAIL, per-category totals, and the time per call.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from muca_server.agents.openai.client_config import build_openai_client
from muca_server.cognition.coordination.models import Charter, ProjectState, Task
from muca_server.cognition.coordination.plan_writer import PlanWriter
from muca_server.cognition.coordination.progress import candidates
from muca_server.cognition.coordination.writer import ProposalWriter, language_rule

ROME = ZoneInfo("Europe/Rome")
# Real dates (run 1-2 used a simulated 24/11 and the model answered from its own
# clock: F21). Today at 15:00 in Rome; the tasks are due tomorrow / in 4 days.
TODAY = datetime.now(ROME).date()
NOW = datetime.combine(TODAY, datetime.min.time(), tzinfo=ROME).timestamp() + 15 * 3600
TOMORROW, LATER = TODAY + timedelta(days=1), TODAY + timedelta(days=4)
MATTEO, MATIAS, BRUNO, CHIARA = 1, 2, 3, 4
MEMBERS = {MATTEO: "Matteo Cesandri", MATIAS: "Matías Rodríguez", BRUNO: "Bruno Neri", CHIARA: "Chiara Bianchi"}
STATE = ProjectState(
    charter=Charter(goal="Deliver Cadence, a proactive coordinator for student groups, with RASD and a prototype.",
                    deliverables=["RASD", "Prototype", "Usability test"], confirmed_by=MATTEO, confirmed_at=1.0),
    tasks=[
        Task(task_id="t1", title="Dashboard", owner_id=BRUNO, due_date=TOMORROW, progress=70),
        Task(task_id="t2", title="Dati per il RASD", owner_id=MATIAS, due_date=TOMORROW),
        Task(task_id="t3", title="Scrivere il RASD", owner_id=MATTEO, due_date=LATER, depends_on=["t2"]),
        Task(task_id="t4", title="Usability test", owner_id=CHIARA, due_date=TOMORROW),
    ],
)
ASKED_BRUNO = [f"task_id t1 (Dashboard, due {TOMORROW}): asked Bruno Neri 1 h ago; answer: no answer yet"]
ASKED_MATIAS = [f"task_id t2 (Dati per il RASD, due {TOMORROW}): asked Matías Rodríguez 1 h ago; answer: no answer yet"]

REDIRECT = re.compile(r"progett|productiv|produttiv|concentr|torniamo|ricordiamoci|back to (the|your) (project|work)|stay on topic|focus", re.I)
DISCLAIMER = re.compile(r"\b(an ai|un'?ia|intelligenza artificiale|language model|modello linguistico|non ho accesso|no access|I can help you with)\b", re.I)
BLAME = re.compile(r"colpa|in ritardo per colpa|dovevi|avresti dovuto|you should have|immediately|subito lo stato|status update", re.I)
REASSIGN_DONE = re.compile(r"(ho|abbiamo) (riassegnato|spostato|assegnato)|I (have )?reassigned|è ora di (Matteo|Chiara|Bruno)", re.I)
ITALIAN = re.compile(r"\b(il|la|di|che|è|per|non|un|una|ma|ti|tu|sei|come|ancora|grazie|bene)\b", re.I)
ENGLISH = re.compile(r"\b(the|and|you|is|are|it|what|how|thanks|for)\b", re.I)


@dataclass
class Case:
    name: str
    split: str
    category: str
    question: str = ""
    maybe: bool = False
    chat: list[str] = field(default_factory=list)
    checkins: list[str] = field(default_factory=list)
    language: str = ""
    # ask expectations
    lang: str = "it"
    silent: bool = False
    progress: tuple[str, set[str]] | None = None  # (task_id, allowed statuses)
    must: list[str] = field(default_factory=list)  # regexes the answer must match
    must_not: list[str] = field(default_factory=list)
    casual: bool = False
    max_chars: int = 600
    # question path
    task: str = ""
    expect_skip: bool = False


def ask(name, split, category, question, **kw) -> Case:
    return Case(name, split, category, question=question, **kw)


CHECKIN_CHAT_BRUNO = ["Cadence: Bruno, come sta andando il dashboard? La consegna è domani: ti manca ancora qualcosa?"]
CHECKIN_CHAT_MATIAS = ["Cadence: Matías, i dati per il RASD sono quasi pronti? Matteo dovrebbe iniziare appena li riceve."]

CASES = [
    # --- T11 casual talk (no redirect, no disclaimers, natural, short)
    ask("calcio", "dev", "casual", "Giochi spesso a calcio, Cadence?", casual=True, max_chars=260),
    ask("musica", "dev", "casual", "Cadence, che musica ti piace?", casual=True, max_chars=260),
    ask("come stai", "dev", "casual", "Ciao Cadence, come stai oggi?", casual=True, max_chars=200),
    ask("partita", "holdout", "casual", "Cadence hai visto la partita ieri sera?", casual=True, max_chars=260),
    ask("weekend", "holdout", "casual", "Cadence, che fai nel weekend?", casual=True, max_chars=260),
    ask("follow-up casual", "holdout", "casual", "e tu sei più da pizza o da sushi?", maybe=True,
        chat=["Matteo Cesandri: stasera pizza dopo la consegna?", "Bruno Neri: sì!"], casual=True, max_chars=260),
    # --- T12 language consistency
    ask("it although group chose en", "dev", "language", "Cadence, chi si occupa del dashboard?", language="en",
        must=[r"Bruno"]),
    ask("en question in en", "dev", "language", "Cadence, who is doing the usability test?", lang="en", must=[r"Chiara"]),
    ask("it after english chat", "holdout", "language", "Cadence, quando scade il RASD?",
        chat=["Bruno Neri: the dashboard build passes now", "Chiara Bianchi: nice, I'll test it tomorrow"], must=[rf"{LATER.day}"]),
    ask("switch back to english", "holdout", "language", "Cadence, what does Matteo's task depend on?", lang="en",
        chat=["Matías Rodríguez: Cadence ciao!", "Cadence: Ciao Matías!"], must=[r"[Dd]ati|data|Matías"]),
    # --- T13 project data available
    ask("who and when", "dev", "knowledge", "Cadence, chi sta facendo il dashboard e per quando?", must=[r"Bruno", rf"{TOMORROW.day}|domani"]),
    ask("what is due tomorrow", "holdout", "knowledge", "Cadence cosa scade domani?", must=[r"[Dd]ashboard", r"[Uu]sability|test"]),
    ask("do you see the plan", "holdout", "knowledge", "Cadence, riesci a vedere il nostro piano?", must=[r"[Dd]ashboard|RASD|attivit|task"],
        must_not=[r"non (riesco|posso)"]),
    # --- T6 normal progress reply
    ask("bruno on track", "dev", "progress", "Tutto bene, mi manca solo un ultimo controllo.", maybe=True,
        chat=CHECKIN_CHAT_BRUNO, checkins=ASKED_BRUNO, progress=("t1", {"on_track"}), max_chars=160,
        must_not=[r"\?"]),
    ask("bruno nearly done", "holdout", "progress", "quasi fatto, stasera lo chiudo", maybe=True,
        chat=CHECKIN_CHAT_BRUNO, checkins=ASKED_BRUNO, progress=("t1", {"on_track"}), max_chars=160, must_not=[r"\?"]),
    # --- T5 difficulty -> kind clarification
    ask("matias behind", "dev", "progress", "Sono indietro, non penso di riuscire a finire in tempo.", maybe=True,
        chat=CHECKIN_CHAT_MATIAS, checkins=ASKED_MATIAS, progress=("t2", {"behind", "blocked"}), must=[r"\?"]),
    ask("matias struggling paraphrase", "holdout", "progress", "mmm non benissimo, sono in alto mare", maybe=True,
        chat=CHECKIN_CHAT_MATIAS, checkins=ASKED_MATIAS, progress=("t2", {"behind", "blocked"}), must=[r"\?"]),
    # --- Example C: missing piece -> practical options, no reassignment done
    ask("matias missing data", "dev", "progress", "Mi manca una parte dei dati e serve ancora un giorno.", maybe=True,
        chat=CHECKIN_CHAT_MATIAS, checkins=ASKED_MATIAS, progress=("t2", {"behind", "blocked"}),
        must=[r"Matteo|parte|disponibil|aiut"]),
    ask("matias waiting on prof", "holdout", "progress", "sto aspettando che il prof ci mandi il dataset, finché non arriva sono fermo",
        maybe=True, chat=CHECKIN_CHAT_MATIAS, checkins=ASKED_MATIAS, progress=("t2", {"blocked", "behind"})),
    # --- T8 declined
    ask("bruno declines", "dev", "progress", "ne parliamo domani, ora non ho tempo", maybe=True,
        chat=CHECKIN_CHAT_BRUNO, checkins=ASKED_BRUNO, progress=("t1", {"declined"}), max_chars=160, must_not=[r"\?"]),
    ask("bruno later", "holdout", "progress", "dopo ti dico, sono a lezione", maybe=True,
        chat=CHECKIN_CHAT_BRUNO, checkins=ASKED_BRUNO, progress=("t1", {"declined"}), max_chars=160, must_not=[r"\?"]),
    # --- not for Cadence: silence stays
    ask("talk among themselves", "holdout", "routing", "Chiara ci vediamo alle 5 in aula?", maybe=True,
        chat=CHECKIN_CHAT_BRUNO, checkins=ASKED_BRUNO, silent=True),
    # --- CS6 question wording (T1, T7) and silence when status known (T15)
    Case("question bruno", "dev", "question", task="t1", chat=["Matteo Cesandri: ok ci sentiamo domani"]),
    Case("question matias downstream", "dev", "question", task="t2", chat=["Bruno Neri: ciao a tutti"],
         must=[r"Matteo|RASD"]),
    Case("question chiara", "holdout", "question", task="t4", chat=["Matías Rodríguez: oggi sono pieno di lezioni"]),
    Case("question skipped, status in chat", "dev", "question", task="t1", expect_skip=True,
         chat=["Matteo Cesandri: Bruno come va?", "Bruno Neri: il dashboard è quasi finito, domani mattina lo consegno"]),
    Case("question skipped, owner said not now", "holdout", "question", task="t4", expect_skip=True,
         chat=["Chiara Bianchi: oggi non chiedetemi del test, domattina vi aggiorno io su tutto"]),
]


async def run(case: Case, plan: PlanWriter, writer_for) -> tuple[list[str], str, float]:
    problems: list[str] = []
    started = time.monotonic()
    if case.category == "question":
        found = {c.task.task_id: c for c in candidates(STATE, [], now=NOW, tz=ROME)}
        candidate = found[case.task]
        text = await plan.draft_progress_question(candidate, members=MEMBERS, state=STATE, chat=case.chat, now=NOW, tz=ROME)
        elapsed = time.monotonic() - started
        if text is None:
            return ["model call failed"], "(failed)", elapsed
        if case.expect_skip:
            if text:
                problems.append("asked although the status/wish was in the chat")
            return problems, text or "(silence)", elapsed
        if not text:
            return ["stayed silent on an unknown status"], "(silence)", elapsed
        owner = MEMBERS[candidate.task.owner_id].split()[0]
        if not text.startswith(owner) and owner not in text:
            problems.append("does not name the owner")
        if not ITALIAN.search(text):
            problems.append("not Italian")
        if BLAME.search(text) or re.search(r"promemoria|reminder", text, re.I):
            problems.append("pressure/reminder wording")
        if text.count(".") + text.count("?") + text.count("!") > 3:
            problems.append("more than 2 sentences")
        for pattern in case.must:
            if not re.search(pattern, text):
                problems.append(f"missing /{pattern}/")
        return problems, text, elapsed

    plan_for_lang = PlanWriter(writer_for(case.language))
    answer = await plan_for_lang.answer_question(
        case.question, members=MEMBERS, state=STATE, chat=case.chat, today=TODAY, maybe=case.maybe,
        **({"checkins": case.checkins} if case.checkins else {}),  # the baseline has no such argument
    )
    elapsed = time.monotonic() - started
    if answer is None:
        return ["model call failed"], "(failed)", elapsed
    text = answer.answer
    progress = getattr(answer, "progress", None)
    shown = (text or "(silence)") + (f"   [progress={progress}]" if progress else "")
    if case.silent:
        if text:
            problems.append("answered a message not for Cadence")
        return problems, shown, elapsed
    if not text:
        return ["silent"], shown, elapsed
    # Checker fix after run 1: quoted task titles ("Dati per il RASD") are the
    # plan's words, not the answer's language; they are left out of the count.
    prose = re.sub(r"[“«\"][^”»\"]*[”»\"]", " ", text)
    if case.lang == "it" and (not ITALIAN.search(prose) or len(ENGLISH.findall(prose)) > len(ITALIAN.findall(prose))):
        problems.append("not Italian")
    if case.lang == "en" and len(ITALIAN.findall(prose)) > len(ENGLISH.findall(prose)):
        problems.append("not English")
    if DISCLAIMER.search(text):
        problems.append("disclaimer / AI talk / no-access claim")
    if case.casual and REDIRECT.search(text):
        problems.append("redirects casual talk to the project")
    if BLAME.search(text) or REASSIGN_DONE.search(text):
        problems.append("blame or a reassignment presented as done")
    if len(text) > case.max_chars:
        problems.append(f"too long ({len(text)} > {case.max_chars})")
    if case.progress is not None:
        want_task, allowed = case.progress
        if progress is None or progress[0] != want_task or progress[1] not in allowed:
            problems.append(f"progress {progress} not {want_task} in {sorted(allowed)}")
    for pattern in case.must:
        if not re.search(pattern, text):
            problems.append(f"missing /{pattern}/")
    for pattern in case.must_not:
        if re.search(pattern, text):
            problems.append(f"has /{pattern}/")
    return problems, shown, elapsed


async def main(env_path: str, split: str = "all", only: str = "", repeats: int = 1) -> int:
    env = {k: v for k, v in dotenv_values(env_path).items() if v}
    model = env.get("MUCA_COORDINATOR_MODEL", "gpt-4o")
    client = build_openai_client(env)
    base = ProposalWriter(client, model=model)

    def writer_for(code: str) -> ProposalWriter:
        if not code:
            return base

        async def chosen() -> str:
            return code

        return base.for_group(chosen)

    assert language_rule("en")  # the rule a group that chose English gets
    plan = PlanWriter(base)
    cases = [c for c in CASES if (split == "all" or c.split == split) and only.lower() in c.name.lower()]
    results: dict[tuple[str, str], list[bool]] = defaultdict(list)
    times: list[float] = []
    failed = 0
    for case in cases:
        for n in range(repeats):
            problems, shown, elapsed = await run(case, plan, writer_for)
            times.append(elapsed)
            failed += bool(problems)
            results[(case.split, case.category)].append(not problems)
            print(f"\n[{'PASS' if not problems else 'FAIL'}] {case.split}/{case.category} {case.name} #{n + 1} ({elapsed:.1f} s)")
            if case.question:
                print(f"  > {case.question}{'  (maybe)' if case.maybe else ''}{'  [group chose ' + case.language + ']' if case.language else ''}")
            print("  " + shown.replace("\n", "\n  "))
            for p in problems:
                print(f"  !! {p}")
    print("\n=== by split and category ===")
    for split_name in ("dev", "holdout"):
        rows = {k[1]: v for k, v in results.items() if k[0] == split_name}
        if rows:
            total = [x for v in rows.values() for x in v]
            print(f"{split_name}: {sum(total)}/{len(total)}")
            for category, v in sorted(rows.items()):
                print(f"   {category:12} {sum(v)}/{len(v)}")
    total = sum(len(v) for v in results.values())
    times.sort()
    print(f"\n{total - failed}/{total} passed; latency median {times[len(times) // 2]:.1f} s, max {times[-1]:.1f} s ({model})")
    return 1 if failed else 0


if __name__ == "__main__":
    a = sys.argv
    sys.exit(asyncio.run(main(a[1], a[2] if len(a) > 2 else "all", a[3] if len(a) > 3 else "", int(a[4]) if len(a) > 4 else 1)))
