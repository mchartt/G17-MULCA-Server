# Long-term memory: design draft

Status: **built** (cognition/memory/day_memory.py, CoalaPolicy, agents/openai/day_summarizer.py). Replaces the management cycle.

## Why

Measured on a real local database (Oct 4–14, ~1,300 turns, 57 management cycles):

- **The summary is fixed-size, not cumulative.** It grew to ~250 words by the 4th cycle and stayed at 200–300 words for the next 53, although the prompt asks for ~1,500. Every cycle rewrites the whole summary from the previous one plus 24 new messages, so new content can only get in by pushing old content out.
- **It drifts into abstraction.** The prompt asks for "the high-level process", and a vague sentence gets merged away by the next rewrite ("Thomas expands the evaluation into MULCA's handling of conversational topics...").
- **The guardrails don't hold.** Contributions came out as role labels and action points had no owners and were partly invented, although the prompt forbids both. Nothing checks them.
- **MULCA's own turns are excluded** from what gets summarized.
- **Bugs, fixed separately:** the cycle re-ran without new messages (10 of 57 cycles), and a restarted group session was never sent the summary.

Design rule that follows: **every summary is generated from the stored log, never rewritten from a previous summary.**

## Memory layers

| Layer | What | Written | Rewritten? |
|---|---|---|---|
| **Log** | `episodic_log`: every message and spoken turn, MULCA's replies | Every turn (as today) | Never |
| **Day summary** | One per active day, from that day's log | First activity of the next day | Never |
| **Today so far** | Same format, from today's log | When today no longer fits the context budget | Replaced, always regenerated from the full log |
| **Overview** | The project so far, from all day summaries | First activity of a new day | Rebuilt from day summaries, never from the previous overview |
| **Recent window** | The last ~150 messages, verbatim | Rolling | — |

Days with no activity have no summary. A day is the local calendar day, Europe/Rome.

## Summary format

The narrative is the main component; the fields capture what a narrative tends to lose.

```
date            the day (day summary / today so far), or the span covered (overview)
narrative       what happened and how, concretely -- names, numbers, titles, decisions

WHAT WAS DECIDED AND DONE
decisions       [{decision, decided_by, rationale}]          overview adds: date, changed_on
action_points   [{owner, task, status: open|done}]           overview adds: raised_on
conclusions     [results or answers the group reached]
knowledge       [facts and findings gained, incl. from MULCA or a web search]

HOW THEY GOT THERE
contributions   [{name, items: [what they specifically proposed, built, argued, decided]}]
decision_making [how each decision came about: who proposed, objections, how settled]
process_notes   [how it went: phases, where they got stuck, conflicts, changes of direction]
mulca           [what MULCA was asked or contributed, and whether it helped]
```

Stored as JSON; code renders it to text with dated headings. No word limit -- length follows content.

## Input to the summarizer

That day's log, in order, one line per turn: `Ada (voice): ...` / `MULCA (text): ...`. **No per-turn times** -- the day's date is the timestamp.

A separator line where a day has a gap of 30+ minutes or a voice call ended (`--- break of 2h ---`, `--- voice call ended ---`), so two separate sessions on one day aren't read as one conversation.

Left out: untranscribed turns, management-cycle rows, session events.

## Prompts (draft)

### Day summary / today so far

> You are writing the memory of one day of a student group's project work, for an assistant (MULCA) that supports the group and will rely on this to remember what happened. The log below is everything said that day, in text and voice, by the students and by MULCA, in order.
>
> Write **narrative** first: a concrete account of the day -- what the group worked on, what they decided and produced, and how they got there. Then fill in the fields, each capturing what the narrative may leave out.
>
> Rules:
> - Always state the content, never just the topic: "agreed the deadline is Fri 24 Oct", not "discussed the deadline". Keep names, numbers, titles, dates and links.
> - Only include what is in the log. Do not infer goals, invent action points or describe what the group "should" do.
> - Contributions are per person and specific: what they proposed, built, argued or decided -- not a role label. Someone who barely contributed reads as barely contributing.
> - Every action point has an owner: a name, "group", or "unassigned".
> - MULCA's contributions go in `mulca`, not in `contributions`.
> - Ignore lines that are garbled or meaningless; they are likely transcription noise.
> - Leave a field empty rather than pad it. Write in English, whatever language was spoken.

### Overview

> Below are the day summaries of a student group's project so far, each with its date. Write the group's current memory of the whole project, for MULCA to rely on.
>
> Same format, with dates. Rules on top of the day prompt:
> - **decisions:** keep every decision with the date it was made. If one was later changed, keep both: "changed on 21 Oct from A to B".
> - **action_points:** one current list; drop items a later day marks done. Each keeps its owner and the date it was raised.
> - **conclusions / knowledge:** the current state, with the date each was established; note anything a later day overturned.
> - **contributions:** per person across the whole project, dating the key ones.
> - **narrative:** how the project developed: a line per active day, then phases, turning points and recurring problems.
> - Work only from the day summaries given; don't carry over anything they don't contain.

## Triggers

- **First activity of a new day** (any message or turn, or a reconnect): summarize every earlier day that has log entries but no day summary (and discard its "today so far"), then rebuild the overview.
- **An agent's session is full** (its own token limit, ~24k): if today no longer fits verbatim (~12k tokens), regenerate "today so far" from today's full log; then reset both agents' context (below). A renewal that never arrives falls back to dropping the oldest half at 1.5x the limit.
- A failed summary call leaves the previous state in place and is retried at the next trigger; it never blocks a turn.

## What each agent gets

The same memory for both agents, each with the same budget (~24k tokens). The intervention agent decides whether MULCA speaks, so it shouldn't know less than the agent that speaks; mostly cached, the extra context costs ~$0.0005 per judged turn.

1. **Overview** (if any)
2. **Today:** verbatim if it fits; otherwise "today so far" + the recent window
3. Live turns as they happen (as today)

Sent on connect and after every context reset. On a context reset, the agent's realtime session is cleared and re-sent 1–2 -- this replaces `trim_context` dropping the oldest half with nothing re-sent.

Additionally, for the main agent only, on request:

- **provide_summary**, or a question about an earlier day: the relevant day summaries. The intervention agent names the period the question is about (`history_period`: today, yesterday, last_session, this_week, last_week, a date, or all); the policy turns it into days and sends only those days, plus today only if it's in the period and the overview only for "all". Anything it can't read means "all".
- **remember**: similarity search over the log (unchanged)

**DMs:** no summaries of their own (as today); they get the linked group's overview and today, read-only.

## Replacing the management cycle

Everything that depends on it:

| Depends on the cycle | Replacement |
|---|---|
| Participation numbers, computed over `wm.history`, which the cycle trims every 24 messages | Rolling window: last ~50 messages per modality |
| Bounding `wm.history` and the tail re-sent after a restart | Recent window, ~150 messages |
| DMs mirror the group's summary, roles and action points | Group's overview and today |
| `provide_summary`, the main agent's and the intervention agent's catch-up | Overview, today, and day summaries on request |
| Management-cycle rows in the log | Stay; nothing reads them for memory |

## Storage

New table only; `episodic_log` and `memory_snapshot` are untouched, so older server versions keep working on the same database file (same rule as `session_event`).

```sql
CREATE TABLE IF NOT EXISTS memory_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'day' | 'today' | 'overview'
    day TEXT NOT NULL,           -- 'YYYY-MM-DD'; for an overview, the day it was built
    content TEXT NOT NULL,       -- JSON, the format above
    through_log_id INTEGER,      -- last episodic_log id it covers
    created_at REAL NOT NULL
);
```

The snapshot's `history_summary`, `participant_roles_summary` and `action_points` stop being written; they're left in place for a rollback.

**Existing data:** on first start, backfill day summaries for past days from the existing log, then build the overview.

## Cost

Per group per active day: one day summary and one overview on a stronger model (a setting; only these once-a-day jobs use it), and "today so far" 0–3 times on a busy day on gpt-4o (~$0.07 each for a busy day's ~25k tokens). Roughly $0.10–0.50 per group per active day depending on the model -- compared with one call per 24 messages today.

## Decided

- Break separators: yes, 30+ minute gap or a voice call ending.
- Budget: the same ~24k tokens for both agents.
- Windows: recent ~150 messages; participation ~50 per modality.
- Day summaries and overview on a stronger model (which one: open); "today so far" on gpt-4o.

## Open questions

None. Decided since: gpt-5 for the once-a-day summaries (falls back to gpt-4o; MUCA_DAILY_SUMMARY_MODEL overrides), and backfill of past days happens automatically at a group's first start.

## Test plan

- Unit tests: triggers, what each agent receives, DMs, a failed summary call, the day boundary.
- On a copy of the real local database: backfill and overview from the actual log; read the output for quality; confirm the old server version still runs on the same file.
- Live: a local session spanning a forced day change and a forced budget overflow.
