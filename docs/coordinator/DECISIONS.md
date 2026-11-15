# DECISIONS — Group Coordinator

Newest first. Each entry: date, decision, alternatives, reason, status.

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
