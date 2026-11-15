# PROGRESS — Group Coordinator

**Read this file first.** It says where the work really is.

Last update: 15 Nov 2026, M1 code done on server and Discord; waiting for Azure + a Discord test bot for the live closing run.

## Where we are

| Stage | Due | Status |
|---|---|---|
| Phase 0 · verify, no source changes | 16/11 | **DONE except the live Azure probe** (waiting for deployments + `.env`) |
| M1 · Azure + project state on Discord | 20/11 | **CODE DONE, NOT VERIFIED (live)** — server + Discord built and tested; closing live run needs Azure and a Discord test bot |
| M2 · web app | 23/11 | NOT STARTED |
| M3 · proactive strategies CS5, CS2, CS3 | 26/11 | NOT STARTED |
| M4 · planning, check-in, CS7, decision questions | 29/11 | NOT STARTED |
| M5 · calendar, demo (Slack only if ahead on 29/11) | 01/12 | NOT STARTED |

Next exact step (resume here in a new chat): **M2, crea-sito phase 0**.
The web app notebook exists at `mulca/prototype/webapp/docs/stato.md`
(project "Ctrl+Alt+Design", guided mode, constraints recorded). Run the
`cinematic-web-builder` skills in order, starting with `setup-iniziale`,
then the design interview (`intervista-idea`, Mobbin, reference sites).
In parallel with the design phases, the web adapter in the MULCA process
(FastAPI + WebSocket, same `ScopeSession`) can be built test-first.

Waiting on Matteo for the M1 live run: (1) Azure deployments +
`muca-server/.env`, then `scripts/azure_probe.py`; (2) a Discord test server
and bot (mulca `prototype/TRYING_ON_DISCORD.md`), token in
`mulca/prototype/.env`. Live run: both processes with
`MUCA_COORDINATOR_ENABLED=true` / `MULCA_COORDINATOR_ENABLED=true`,
`/mulca link mode:ambient`, then `/coordinator profile`, `/coordinator
kickoff`, Confirm on the card, `/coordinator plan`; note the cost.

Where things are: work on `group17/coordinator` in both clones on Matteo's
PC (`WebstormProjects/group-coordinator`), mirrored to GitHub
(`mchartt/G17-MULCA-Server`, `mchartt/G17-MULCA-Project`); GitLab untouched.

## M1 log

- 15/11 · Server side: `protocol/coordinator.py` (5 events),
  `cognition/coordination/` (models, store, proposals, writer, router,
  settings, wiring), `agents/openai/client_config.py`, task
  `group_coordinator.yaml`; hooks in `run_server.py`,
  `session_components_factory.py`, `dispatcher.py`, `incoming.py`,
  `protocol/__init__.py`, `pyproject.toml`; docs/PROTOCOL.md section.
  Tests: 55 new, all green; lab suite 475 passed + the 1 known failure;
  coverage of new modules 99–100 % (`coverage run --source=muca_server -m
  pytest tests/coordinator`). Cost: 0 (no model call). Live: NOT VERIFIED.
- 15/11 · `proposal_response` gains `requestId`; kickoff also answers with a
  notice (Discord needs an answer to every command).
- 15/11 · Discord side: `mulca_discord/coordinator/` (commands, bridge,
  cards); hooks in `muca_client.py`, `sessions.py`, `__main__.py`.
  Tests: 21 new (one end to end against a real local MUCA-Server with the
  coordinator on); MULCA suite 257 passed; coverage 98–100 %.
- 15/11 · Fresh-eyes review of M1 (sub-agent): 11 findings, all fixed or
  recorded — kickoff no longer blocks the session (background task, 30 s per
  call, one kickoff at a time); bots added later get coordinator events
  (hook on the `SessionRegistry` class) and card buttons (registered on the
  posting bot's client); an old charter card cannot overwrite a newer one
  (checked inside the transaction); every command and click needs a member
  of the roster; rationale checked inside the retry loop (≤ 60 words);
  "did not answer" no longer invites a retry; Azure vs voice documented in
  AZURE_SETUP.md; TRACEABILITY statuses corrected (no DONE without a live
  run). Server suite 481 passed + the known failure; MULCA 258 passed.

## How to measure coverage

`pytest --cov=<module>` clashes with numpy here ("cannot load module more
than once"); use `coverage run --source=muca_server -m pytest tests/coordinator`
then `coverage report -m`.

## Baseline tests (15 Nov 2026, before any change)

Run on Linux (device shell), Python 3.11.17, deps resolved that day
(fastapi 0.163.1, starlette 1.10.0, pydantic 2.14.3, openai 2.54.0,
pytest 8.4.2, pytest-asyncio 0.26.0, discord.py 2.7.1).

| Repo | Command | Result |
|---|---|---|
| muca-server @ `8699ef7` | `python -m pytest -q` | **419 passed, 2 failed** |
| mulca @ `e200433` | `cd prototype && python -m pytest -q` | **236 passed** |

Pre-existing failures (not ours, not hidden):

1. `tests/test_agents.py::test_the_assistant_takes_a_turn_once_the_client_plays_it`
   fails every time: `main_agent_state` is `INACTIVE`, expected `ANSWERING`.
   Voice turn-taking: outside our scope, we do not touch voice.
2. `tests/test_model_settings.py::test_models_in_the_handshake_reach_the_agents_the_session_builds`
   **flaky**: failed once in the full run (`CancelledError`), passed 5/5 alone.

Rule: after each slice these two may still fail; any **other** new failure
blocks the slice.

## Plan of the stages

### M1 · Azure and project state (by 20/11)

- **Hook points** (INTEGRATION_MAP.md): `run_server.py:131` client; strategy
  stores `run_server.py:132`–`:165`; policy factory `:252`.
- **Files (new):**
  - `muca_server/agents/openai/client_config.py` — builds OpenAI or Azure client from env.
  - `muca_server/cognition/coordination/` — `models.py` (pydantic: Charter,
    MemberProfile, Milestone, Task, Decision, Proposal), `store.py` (SQLite
    tables in the conversation file, the only writer), `proposals.py`
    (lifecycle pending → applied | rejected | expired, re-validation before
    apply), `writer.py` (Chat Completions + json_schema, pydantic, one retry
    then silence), `settings.py` (the on/off switch), `INDICE.md`.
  - `muca_server/cognition/config/tasks/group_coordinator.yaml`.
  - `mulca_discord/coordinator/` — `commands.py` (`/coordinator profile`,
    `kickoff`, `plan`), `cards.py` (persistent buttons, `discord.ui.DynamicItem`), `INDICE.md`.
- **Protocol events (added, none changed):** server → client
  `coordinator_proposal`, `coordinator_state`; client → server
  `proposal_response`, `coordinator_command`. Documented in `docs/PROTOCOL.md`.
- **Tables:** `coord_charter`, `coord_member`, `coord_milestone`, `coord_task`,
  `coord_task_dependency`, `coord_decision`, `coord_proposal`.
- **Tests:** store CRUD, every proposal transition, apply re-validation
  (owner not a participant, task missing, expired, already applied), protocol
  contract tests for the 4 events, writer with a fake client (valid JSON,
  invalid JSON → retry → silence), **switch off = today's behaviour**
  (existing suites green + a dedicated test), coverage ≥ 90 % on new modules.
- **Closing proof:** live on a Discord test server with Azure: `/coordinator
  kickoff` → charter card → Confirm → state saved; cost noted.

### M2 · web app (by 23/11)

- Design interview first (crea-sito, Mobbin, reference sites), cinematic
  style only on entry pages.
- **Files (new):** `mulca_discord/web/` (FastAPI + WebSocket adapter in the
  MULCA process, same `ScopeSession` as Discord), `webapp/` (React +
  TypeScript + Vite), `INDICE.md` in each.
- Invite codes, data notice, profile, chat with channel badge, project panel,
  cards, one-time link codes for channel accounts, agent settings, delete
  group data. Accessibility (WCAG 2.1 AA), loading/empty/error/disconnected
  states.
- **Tests:** Vitest on card and panel, one Playwright end-to-end test.
- **Closing proof:** four browser windows + Discord on the same group.

### M3 · proactive strategies (by 26/11)

- YAML: `cs5_owner_and_date`, `cs2_delegate_simple_task`,
  `cs3_close_decision`, each with "when NOT to fire".
- Vetoes (hook `policy.py` after `:671`): Not now 30 min per strategy+topic;
  ≥ 10 member messages since the last unsolicited intervention; never two
  unsolicited in a row; no CS5 if the task already has an owner.
- Why? / Not now on every card, first valid click updates the card everywhere.
- **Tests:** every veto, every card action, test conversations (scenario A,
  Claudio's formatting, Sofia's test method). Live runs noted.

### M4 · planning, check-in, dependencies, questions (by 29/11)

- `/coordinator planning`, `/coordinator checkin` (CS6), `cs7_blockers_dependencies`,
  answers about past decisions with source.
- **Tests + live:** scenario B, Giulia's check-in, /planning, scenario C.

### M5 · calendar and demo (by 01/12)

- iCalendar feed, read-only, revocable token, served by the web adapter.
- `DEMO_SCRIPT.md`, demo setup script, README section, full demo twice in a
  row without manual fixes, cost noted.
- Slack (Socket Mode, Block Kit) **only if M4 is closed by 29/11** (decision
  15/11, DECISIONS.md).

## Assumptions not yet confirmed

| # | Assumption | Proof needed | When |
|---|---|---|---|
| A1 | The student subscription has quota for `gpt-realtime-2.1-mini` in East US 2 | probe output | before M1 live |
| A2 | Key auth works for Realtime on Azure (not only REST) | probe `realtime` line OK | before M1 live |
| A3 | Strict json_schema works on Azure `gpt-4o` | probe `chat+schema` line OK | before M1 writer live |
| A4 | Adding our tables to the conversation SQLite file does not disturb `sqlite.py` | **confirmed by test** `test_shares_the_conversation_file_with_the_episodic_store` | M1 |
| A5 | A FastAPI server can run in the same asyncio loop as discord.py | small spike in M2 | M2 |
| A6 | Web-app members can be given stable int ids for `Participant.id` | design in M2 | M2 |
| A7 | Slack app creation works in a free workspace | try it | only if Slack is kept |
