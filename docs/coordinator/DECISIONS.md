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
