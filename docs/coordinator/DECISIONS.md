# DECISIONS — Group Coordinator

Newest first. Each entry: date, decision, alternatives, reason, status.

## 2026-11-27 · CS7-W1 the blocker writer may say "no dependency" (approved by Matteo)

- **Problem:** a CS7 card had one filter only, the Realtime judge that fires
  the strategy. The card writer, which sees the whole plan (done tasks too),
  had no way to decline: `COMMON_RULES` excluded blockers from the `title ""`
  exit, and a "possible" card on nothing is invalid, so declining cost two
  invalid answers (two calls). Live 27/11, writer level, baseline `7dc7548`:
  false cards 2/13 on dev negatives in both runs (+1 that only R7 dropped),
  21 invalid answers out of 35 calls.
- **Decision:** `certainty` may be `"none"` (`proactive_writer.NO_DEPENDENCY`):
  the server turns it into `NothingToDraft`, no card, no retry. The BLOCKER
  rules first ask whether there is a dependency at all (the planned work
  leaves out the part that needs it, the other work is done or ready in time,
  only an order of work, already recorded with dates that fit, solved later
  in the chat). `BlockerCertainty` unchanged: a card never carries "none".
- **Alternatives:** a deterministic veto on words (rejected: rules built on a
  scenario's words); changing the judge's trigger (kept for later: the judge
  cannot be measured from the cloud sessions, WebSocket is not proxied);
  showing done tasks to the judge (later iteration, one change at a time).
- **Not changed:** judge, Realtime/voice, the lab's strategies, R1–R12, the
  click as the only way to change state.
- **Status:** IMPLEMENTED/TESTED OFFLINE (3 tests) + REAL-MODEL TESTED at the
  writer level (`CONVERSATION_EVAL.md`, "CS7 implicit dependencies"). Judge
  level and end to end: NOT VERIFIED.

## 2026-11-20 · Conversational optimization (taken by Claude, to confirm by Matteo)

Evidence and runs: `CONVERSATION_EVAL.md`.

- **Asked in words, drafted as a card.** "Aggiungi la dipendenza", "crea una
  task per Matías", "sì, fallo" make the `ask` answer carry an `action`; the
  server checks it and posts an ordinary `blocker` (possible) or `task_owner`
  card. A typed "yes" never saves anything: the click does.
  Alternative: apply on a typed yes. Rejected: the confirmation model is a
  click by a member (project rule), and a typed yes in a group is ambiguous.
- **A CS7 card may name who delivers new prerequisite work**
  (`depends_on_owner_id`), only when the chat names a member; confirming gives
  that new task its owner. Like CS5's suggested owner, unlike CS2 (which never
  assigns). The old docstring "the card never assigns" is superseded.
- **R2 by priority.** CS7 > CS3 > CS5 > CS2: a more urgent strategy replaces a
  draft still in its model call; once saving starts it is never replaced.
  NFR3 (R3/R4) unchanged: still one card at a time.
- **Every link keeps its origin** (`coord_task_dependency.source`, additive).
- **Routing in two levels** (client `addressing.py`): a clear call is
  answered; a passing mention is offered as "maybe" (Cadence may stay silent);
  a message to the group ("secondo voi…") is never offered.

## 2026-11-15 · Kickoff runs in the background

- **Decision:** `kickoff` starts a task and returns at once; one kickoff per
  group at a time; each model call has a 30 s limit.
- **Alternative:** wait for the model inside the dispatcher (first version).
- **Reason:** the session reads messages one at a time
  (`session_runtime.py` `receive_loop`): waiting froze the group's chat and
  voice (found by the M1 review).

## 2026-11-15 · Azure stays independent of the coordinator switch

- **Decision:** `AZURE_OPENAI_*` alone selects Azure, as the project
  instructions want the whole chain on Azure. Voice on Azure is marked NOT
  VERIFIED (transcription connects without a deployment name).

## 2026-11-15 · Coordinator plugged in as a wrapper of the Conversation

- **Decision:** when on, `session_components_factory.py` wraps the session's
  `Conversation` in `CoordinatorRouter` (Decorator pattern): it handles the
  new events and forwards everything else unchanged. When off, no wrapper.
- **Alternatives:** add methods to `agents/conversation.py` (2744 lines,
  shared by the lab); a separate WebSocket endpoint.
- **Reason:** smallest change to shared files (one optional argument), and
  "off" is provably identical (the router is the plain Conversation).

## 2026-11-15 · Third server event `coordinator_notice`; client events dropped when off

- **Decision:** besides `coordinator_proposal` and `coordinator_state`, the
  server sends `coordinator_notice` (ok + short text) to answer a command or
  a click. With the coordinator off, the client events are logged and dropped.
- **Reason:** clients need to tell the member why a click was refused; the
  dispatcher has no access to the outbound queue, so "off" cannot answer.

## 2026-11-15 · Private copy on GitHub

- **Decision:** both repos mirrored to private GitHub repos of Matteo's
  account (`mchartt/G17-MULCA-Server`, `mchartt/G17-MULCA-Project`), remote
  `github` in the local clones; updated at the end of each stage. GitLab
  (`origin`) still untouched.
- **Reason:** Matteo asked for an online copy; moving to GitLab later is a push.

## 2026-11-15 · Never touch the project's GitLab

- **Decision:** no push, no remote branch, no merge request, no edit on
  GitLab. The GitLab connector is used read-only. Commits stay local on
  `group17/coordinator` until Matteo asks otherwise.
- **Reason:** Matteo's explicit instruction.

## 2026-11-15 · Scope adapted for feasibility

- **Decision:** first web app + Discord with CS5, CS2, CS3, project charter
  and "why did we decide X?". iCalendar feed kept. Slack only if M4 is
  closed by 29/11. Cinematic style only on the entry pages (welcome,
  create/join a space, data notice); the three-column console stays clean.
- **Alternatives:** full scope with Slack; cinematic style everywhere.
- **Reason:** 17 days to the demo; Slack is the first cut allowed by the
  project instructions; a scroll-film inside a working tool slows it down.
- **Status:** approved by Matteo.

## 2026-11-15 · All channels of a group share the hub's one session

- **Decision:** web, Discord (and Slack) of one group go through the single
  `ScopeSession` the MULCA process opens for `channel:<id>`.
- **Alternative:** each channel opens its own MUCA-Server connection with the
  same `conversationId`.
- **Reason:** the server builds a new `CoalaPolicy` per connection and keeps
  no registry by conversation id (INTEGRATION_MAP.md, section 9): two
  connections would be two brains on one SQLite file.
- **Status:** verified in the code.

## 2026-11-15 · Azure region East US 2, deployment names = model names

- **Reason:** only region in the Global Standard table with every model the
  code uses; same names mean no code change besides the client.
- **Status:** NOT VERIFIED (live) until the probe passes.

## 2026-11-15 · M3: a proactive strategy answers with a card only

- **Decision:** when CS5, CS2 or CS3 fires, the group sees one card (reason
  "Stepping in because…", content, buttons), no separate chat message.
  `CoalaPolicy` hands the strategy to `ProactiveBridge` after the existing
  vetoes; the main agent is not asked to reply.
- **Alternative:** a short agent message plus the card (two messages, one
  more model call).
- **Reason:** one message is cheaper, easier to control (rules R1–R7 in
  code, rationale ≤ 60 words checked) and matches "one request per message".
- **Status:** chosen by Matteo (15/11).

## 2026-11-15 · M3: evidence by line number, copied by the server

- **Decision:** the writer receives the chat as numbered quoted lines and
  answers with numbers; the server copies those messages into the card
  (`Evidence`). A number that is not a line, an owner that is not a member, a
  task that already has an owner or a date in the past makes the draft invalid
  (one retry, then silence).
- **Reason:** "Why?" must never show a quote the model invented (rule 0).

## 2026-11-15 · M3: "Not now" topic key is deterministic

- **Decision:** the topic is normalised (lower case, letters and digits,
  single spaces); a draft whose topic key is silenced for that strategy is
  dropped, and the silenced topics are also listed to the model.
- **Alternative:** similarity between topics (embeddings).
- **Reason:** explainable at the oral and testable; the model avoids
  near-duplicates because it sees the list. Limit: two different wordings of
  the same topic are two keys.

## 2026-11-15 · M3: CS2 never suggests an owner, CS5 may

- **Decision:** a delegation card only has "I'll take it"; a task_owner card
  may suggest a member (skills, free hours) that any member can confirm, or
  anyone can take it.
- **Reason:** spec S2 "do not assign anyone"; spec S5 "suggest based on
  skills and load". Both apply only after a member's click.

## 2026-11-15 · M3: rules R3/R4 count only cards actually posted, in memory

- **Decision:** the 10-message and 5-minute gaps (NFR3) start when a card is
  posted; a failed or dropped draft does not silence the group. The counters
  live in the session (a server restart resets them); the strategy's own
  cooldown is also persisted by the lab's snapshot.

## 2026-11-15 · M4: what the check-in finds is computed in code

- **Decision:** `checkin.py` lists late, blocked, due-soon, ownerless tasks,
  milestones within 48 h with open tasks and dependencies due later than the
  task that waits on them. The model only proposes ≤ 3 options, each changing
  one real task (new date, new owner, or drop). Nothing to fix = a notice and
  no model call.
- **Reason:** the facts on the card must never be invented (rule 0); cheaper.

## 2026-11-15 · M4: a task's status is a member's own action

- **Decision:** `task` (status todo/doing/done/blocked) is written at once by
  `CoordinationStore.set_task_status`, without a card, like the profile.
- **Reason:** a member reporting their own progress is not a change proposed
  by the model (rule "the model never writes state" still holds).

## 2026-11-15 · M4: /planning from a written list

- **Decision:** the members write the tasks (web dialog, one per line;
  Discord, separated by `;`), the coordinator suggests owners with a reason and
  dates, the group confirms the whole list with one click (or rejects and
  sends a new list).
- **Alternative:** the agent asks each member in turn and collects answers
  over several messages (Matteo's first idea, open to a simpler way).
- **Reason:** one round trip, works the same on every channel, explainable;
  the "ask the members" part is the group writing the list together.

## 2026-11-15 · M4: "why did we decide X?" answers only from the log

- **Decision:** a dedicated command (`why`: web panel, Discord
  `/coordinator why`); the answer names the decisions it rests on, checked
  against the log, and the server writes the source line (who, when, card).
- **Limit:** "@coordinator why…" typed in the chat goes to the lab's main
  agent, which does not read the decision log.

## 2026-11-15 · Web app and Discord stay two separate groups

- **Decision:** the web app and Discord are two different uses on two
  platforms and are **not** linked: a web space is its own conversation
  (`web:<code>`), a Discord channel its own (`channel:<id>`). No shared
  memory, no mirrored chat, no account linking between them.
- **Supersedes:** the project instructions' section 6 "one group, one
  conversation across channels" and the 15/11 decision "all channels of a
  group share the hub's one session" (still true *within* Discord: text and
  voice of one link share a session).
- **Reason:** Matteo's decision (15/11), after Claude reported that a full
  merge would touch the lab's `runtime.py`, need account linking and could not
  be verified live before the demo.
- **Consequence:** A5/A6 no longer needed; the merge goes in the report as
  future work.

## 2026-11-15 · M5: calendar feed served by the web adapter

- **Decision:** read-only iCalendar feed per web group, private link
  (32 random bytes, stored hashed), a new link revokes the old one; built
  from the last state the adapter received; no external calendar API.
  The adapter's access log is off so feed secrets never reach a log.
- **Reason:** project instructions section 6 (Calendar row); no new service.

## 2026-11-15 · Azure region: France Central, not East US 2

- **Decision:** resource `g17-coordinator` in France Central (resource group `rg-group17-coordinator`, Standard S0).
- **Reason:** the Azure for Students subscription has a policy "Allowed resource
  deployment regions" with five regions only: Austria East, France Central,
  Poland Central, Spain Central, Switzerland North (read in the portal 15/11;
  East US 2 refused with RequestDisallowedByAzure). In the Microsoft table of
  Global Standard deployments (Europe tab, read 15/11) France Central is the only
  one of the five with `gpt-realtime-2.1-mini`.
- **Superseded:** "East US 2" in the first version of AZURE_SETUP.md.

## 2026-11-15 · Text models: gpt-6.1-sol for the coordinator, gpt-6-luna for summaries

- **Decision (Matteo, after Claude's comparison):** the cards, planning,
  check-in and "why" answers use `gpt-6.1-sol`; the lab's background summaries
  (everyday and daily) use `gpt-6-luna`; the Realtime agents keep
  `gpt-realtime-2.1-mini`; embeddings `text-embedding-3-small`.
- **Reason:** gpt-4o (the code's default) costs $2.50/$10 per million tokens;
  gpt-6.1-sol is a newer generation at $2/$10, gpt-6-luna $0.10/$0.50 (OpenAI
  list prices read 15/11; Azure prices assumed equal, NOT VERIFIED). A card is
  about 3,000 tokens in and 500 out: ~1 cent with Sol, so quality where the
  group reads the text costs a few dollars over the whole project.
- **How:** the models are chosen with environment variables
  (`MUCA_COORDINATOR_MODEL`, `MUCA_SUMMARY_MODEL`, `MUCA_DAILY_SUMMARY_MODEL`);
  without them the code behaves exactly as before. Both models priced in
  `PRICES_PER_MILLION`, so the spending limits see their cost.
- **Not verified:** strict JSON schema on Azure for the two models (probe).

## 2026-11-15 · Online: Render (server + adapter) and Vercel (site)

- **Decision (Matteo):** the web adapter and MUCA-Server run together in one
  free Render container (`deploy/render/`, branch `render` of the GitHub copy
  of mulca, with a snapshot of muca-server in `vendor/`); the site stays on
  Vercel and calls it with `VITE_ADAPTER_URL`.
- **Reason:** Vercel cannot host long-running WebSocket processes.
- **Limits:** the free plan sleeps after 15 minutes without requests (first
  request then takes about a minute) and has no persistent disk: spaces and
  memory are lost at each restart or redeploy. Fine for testing, not for data
  the group must keep. Memory 512 MB with two Python processes: NOT VERIFIED.

## 2026-11-15 · Limits closed after M5

- R3/R4 and the timed check-in remember their last time in `coord_mark`.
- Helpful = feedback only (`coord_feedback`), never a state change.
- `card` command: a client gets a card back (Discord "Why?" after a restart).
- Timed check-in: off unless `MUCA_COORDINATOR_CHECKIN_HOURS`; the router is
  the session worker so the timer stops with the session.
- Deleting a space: `DELETE /conversation` on MUCA-Server (gated on
  `MUCA_ADMIN_TOKEN`, like `/export`); the adapter closes the session first,
  and deletes the web side only if the server part succeeded.
- "@coordinator why" typed in the web chat goes to the decision log; on
  Discord it stays `/coordinator why` (the lab's `runtime.py` is not touched).

## 2026-11-16 · Linked groups: a web space shares its project with Discord / Slack channels

- **Decision (proposed by Gabriele, to confirm with Matteo):** a Discord or
  Slack channel can be linked to a web space with the space's invite code
  (`/coordinator link CODE`). Linked channels share **the project**: plan,
  tasks, decisions and proposal cards live in the web space's SQLite file,
  every open session of the group gets each card and state, and a message
  written in one channel is shown in the others (`coordinator_mirror`).
  `catchup` answers "what did I miss?" from the decision log and the other
  channels' episodic logs, with no model call.
- **What stays as on 15/11:** each channel keeps its own conversation,
  session, agent memory and member ids. The lab's `runtime.py` and
  `conversation.py` are not touched, no accounts are linked.
- **Supersedes in part:** "Web app and Discord stay two separate groups"
  (15/11). The three reasons given there do not apply to this design: no
  lab file changes, no account linking, and everything is tested offline
  (`tests/coordinator/test_coordinator_links.py`).
- **Limits:** the mirror and the shared cards reach sessions open in the same
  server process; a channel whose session is closed sees them through
  `catchup` and the next state it asks for. NOT VERIFIED live on Render.

## 2026-11-19 · K1 the observer sees the project; K2 CS7 v2 (possible dependencies)

- **Problem (tutor 18/11, audit G0):** the intervention agent judged the
  coordinator strategies from the chat alone. It could not notice that a test
  on Friday needs an API that is due Saturday, nor a decision being reopened:
  those facts are in the plan and the decision log, not in the last messages.
- **K1 decision:** when the coordinator is on, the observer's turn update
  ("Current trigger variables: …") ends with a short **Project memory**
  (`coordination/project_memory.py`): open tasks with owner, date, status and
  what they wait on; milestones of the next 14 days; the last 8 decisions with
  their reason. Built in code from the database, never by a model. Sent only
  when it changes, or again every 25 messages (the observer's context is
  trimmed). Hook: `CoalaPolicy._project_note` (policy.py, ~15 lines). With the
  coordinator off, with no bridge, with nothing saved or on any error the
  update is byte-for-byte the lab's (test
  `test_policy_turn_update_is_the_labs_when_off_unchanged_or_failing`).
  Cost: a few hundred input tokens, only on the turns where the plan changed.
- **K2 decision:** CS7 cards say how sure they are. `certainty = blocked`
  (a member said they are stuck: as before, confirming marks the task
  blocked) or `possible` (inferred from the chat and the plan: the card asks
  the group to confirm; confirming only records the dependency, never marks
  anything blocked). A possible dependency must name what it may wait on: a
  plan task or, if the plan does not have it, its title from the chat, added
  without owner on confirmation. Up to three `ways_forward` (mock data, a
  minimal version first, swap the order) are suggestions, not state changes.
  New R7: no card for a dependency the plan already records.
- **Not changed:** the lab strategies (`resolve_conflict` overlap with CS3
  stays Matteo's decision), the Realtime evaluator, R1-R6.
- **Status:** IMPLEMENTED/TESTED OFFLINE (26 tests). Whether the evaluator
  actually fires CS7 on the corpus in `CS_CORPUS.md`: NOT VERIFIED (live run).

## 2026-11-19 · K3 a reopened decision (scenario C)

- **Problem:** "let's say Marco reopens a decision. How does Cadence detect
  that this decision was already made?" (tutor 18/11, via Gabriele's report).
  Before K3 the honest answer was "it doesn't; `why` answers if asked".
- **Decision:** new strategy `cs3_reopened_decision` (kept apart from CS3:
  CS3 closes an open question, this one recalls a closed one). The observer
  judges it from the chat **and** the Project memory (K1), which lists the
  decisions with their reason. The card (kind `revisit`) recalls the
  question, the choice and the reason **copied from the log by the server**,
  says what seems new (`new_point`, "" if nothing), and offers
  Keep (option 0: nothing changes, the card records who kept it) /
  Reopen it (option 1: `coord_decision.reopened_at/by` set; the old choice and
  reason stay in the log; a later CS3 can close it again) / Not now.
  Uses the existing `choose` action: no protocol change. Migration: two
  nullable columns, added to older files at open.
- **Status:** IMPLEMENTED/TESTED OFFLINE (12 tests). Clients must render the
  `revisit` card (U3). Detection quality: NOT VERIFIED until the corpus runs live.

## 2026-11-19 · R8 no second card on the same thing

- A card still waiting for a click (same kind and topic, or for CS5/CS2 the
  same task title) stops a new draft from being posted
  (`proactive._duplicate`, `store.open_proposals`). The first card stays
  answerable; a copy would only add noise (CCLT: coordination is a cost too).
  Code, not model. Tested offline.

## 2026-11-19 · Slice C: a task made by hand has a description and what it waits on

- **Decision:** the hand command `task` accepts two more arguments:
  `description` (up to 2000 characters, "" clears it) and `depends_on` (a
  list of task ids or plan numbers; [] clears it), on add and on edit. The
  server refuses a task that waits on itself, on an unknown task, or in a
  loop (A waits on B, B on A, through any tasks), and saves nothing.
  The check runs inside the same transaction as the write
  (`store._replace_dependencies`), so two members cannot race a loop in.
- **Why:** the "+ New" popup and the dependency page need both; the table
  `coord_task_dependency` already existed but nothing by hand wrote it.
- **Protocol:** additive. Old clients send neither argument and nothing
  changes. Migration: one column with a default, added to older files at open.
  By hand = no card, no model call. Approved by the user on 2026-11-19
  (decision 2 of the plan).
- **Known gap (not fixed here):** a CS7 card that confirms "A waits on B"
  (`blocker_applier`) does not run the loop check. IDEA: reuse
  `_replace_dependencies` there, with its own decision.
- **Open question:** hand-added tasks accept a past date (the planning list
  refuses it). Left as is: a member may log work that is already late.
- **Status:** IMPLEMENTED/TESTED OFFLINE (9 tests, `test_coordinator_task_details.py`).

## 2026-11-20 · D-CS6 progress check-ins: one question at most, under one shared budget

- **Decision:** Cadence may ask ONE short question about a task's progress
  when its deadline is close (`progress.py`), as a plain chat message, never a
  card. The timer (`checkin_timer.py`, every 10 min while the session is open)
  starts an *assessment*; most assessments end in silence.
- **Deadline convention:** `due_date` has no time, so it means the end of
  that day (23:59:59) in the project time zone (`MUCA_COORDINATOR_TIMEZONE`,
  default `Europe/Rome`, `zoneinfo` handles daylight saving). The assessment
  window opens 36 h before (noon the day before: "la consegna è domani" in
  daytime, about 24 h ahead) and closes 24 h after (one gentle question about
  a task just overdue, never repeated).
- **No question when:** done; no owner (CS5's gap); no date; already asked or
  answered for this due date; status/progress changed by a member in the last
  48 h (unless blocked); "Not now" on the task (strategy `cs6_progress_checkin`);
  the task discussed in the chat in the last 12 h; a card about it is open;
  the model sees in the chat that the status was already given.
- **Budget, shared with the cards:** quiet hours 22:00-08:00 local; one
  time-driven question per group every 4 h; none within 30 min of any
  proactive card (a question does NOT move the cards' R4 marker: the owner's
  reply may need its CS7 card at once); no two questions to one member within 24 h; not
  while a card draft runs, the plan opener listens or a member's request runs;
  never right after Cadence's own message; never in a DM. The interval
  check-in card (`MUCA_COORDINATOR_CHECKIN_HOURS`) now uses the same gate and
  marker, and never runs in the same tick as a question.
- **Idempotence:** one row per (task, due date) in `coord_progress_checkin`,
  claimed with INSERT OR IGNORE before the model call: two timers, two
  workers on one file or a restart never send it twice; a moved deadline is a
  new key, so the old question cannot fire.
- **The reply:** see "Replies however late" below. The answer's `progress`
  field (on_track / behind / blocked / done / declined) is stored as
  coordinator memory only: no status, owner or date changes without a card
  and a click.
- **Switch:** on with the coordinator; `MUCA_COORDINATOR_PROGRESS_CHECKINS=off`
  disables it. Coordinator off: nothing built, unchanged.
- **Replies however late:** a member's chat message, up to 3 days after an
  unanswered question to them, is read by `ask` as "maybe" on the server
  (`router._late_progress_reply`, any channel), at most 3 of their messages
  per question; a message asking the group ("qualcuno…", "ragazzi…",
  "anyone…") is never taken as a reply. Answered once: the same line coming
  also as a web follow-up is read once, in either order. The answer is posted
  in the chat; a change it asks for is a card.
- **Wake-up:** the web hub, every 30 min, sends the additive command
  `progress` for groups whose last saved plan has an open, owned task due from
  yesterday to the day after tomorrow, so the assessment runs even if nobody
  opened the space since the adapter started (`MULCA_WEB_WAKE=off` disables).
  It opens a session (and its Realtime connection) only for those groups.
- **Openings vary:** a question may not start like one of Cadence's lines of
  the last 3 days (three words, names left out); one retry, then silence.
- **R8 in the chat:** while the plan opener listens, a "maybe" message gets
  silence without a model call (its answers belong to the opener).
- **Known limit:** Discord/Slack have no wake-up (the web hub does it); on
  Render the adapter itself must be running (free instances sleep):
  NON VERIFICATO on the deployed app.
- **Status:** IMPLEMENTED/TESTED OFFLINE (`test_coordinator_cs6_progress.py`).

## 2026-11-20 · R10 one responder per message

- **Decision:** a message the client already sent to Cadence's `ask` (web
  follow-up as "maybe") is not answered again by the lab's solicited
  strategies (`respond_when_addressed`, `remember`, `provide_summary`): the
  bridge vetoes them for 120 s on the same text (`proactive.claim`).
- **Why:** such a follow-up was also sent on as `user_text`; the lab agent
  (persona "MULCA", "Reply in English by default", no project state) could
  answer it too: two replies, the second in English, saying it has no access
  to the project. Discord/Slack are unchanged (no follow-up routing there).
- **Residual race:** the claim is recorded when the ask command arrives; the
  lab judge needs a model call before the bridge sees its strategy, so in
  practice the claim is first. Not proven under load.

## 2026-11-20 · Web follow-ups reach Cadence AND the group's chat again

- **Decision:** a web follow-up (a message within 3 min of Cadence's answer
  or of a new card) goes to `ask` as "maybe" and is ALSO sent on as
  `user_text`, as before PR #33. PR #33 had stopped the second path to avoid
  the lab agent's double reply; the side effect was that the plan opener never
  heard its answers and CS3/CS7 never saw those lines (a pending card opens
  the window), which broke `opener.spec` and the CS3 step of `plan.spec`.
  The double reply is now stopped on the server (R10). Approval needed:
  this reverses a choice made in PR #33.
