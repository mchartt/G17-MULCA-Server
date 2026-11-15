# DECISIONS — Group Coordinator

Newest first. Each entry: date, decision, alternatives, reason, status.

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
