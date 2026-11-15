# PROGRESS — Group Coordinator

**Read this file first.** It says where the work really is. It is written so a
new chat can resume without the old one.

Last update: 15 Nov 2026, late evening. M1 and M2 code done and tested offline;
both wait for Azure for the live closing run. Cadence interface v2 (soft,
Apple-like) built; **Vercel deploy prepared but not published** (see "Deploy").

## Where we are

| Stage | Due | Status |
|---|---|---|
| Phase 0 · verify, no source changes | 16/11 | **DONE except the live Azure probe** (waiting for deployments + `.env`) |
| M1 · Azure + project state on Discord | 20/11 | **CODE DONE, NOT VERIFIED (live)** — server + Discord built and tested; closing live run needs Azure and a Discord test bot |
| M2 · web app "Cadence" | 23/11 | **CODE DONE, NOT VERIFIED (live)** — film, web adapter, landing + console v2; offline e2e, Vitest, axe pass; live run needs Azure |
| M2b · online preview on Vercel | 16/11 | **PREPARED, NOT PUBLISHED** — static build ready, needs a Vercel token or a login from Matteo's terminal |
| M3 · proactive strategies CS5, CS2, CS3 | 26/11 | NOT STARTED |
| M4 · planning, check-in, CS7, decision questions | 29/11 | NOT STARTED |
| M5 · calendar, demo (Slack only if ahead on 29/11) | 01/12 | NOT STARTED |

## Next steps, in order (resume here)

1. **Publish the Vercel preview** (section "Deploy"): first thing of the next chat.
2. **Live run of M1 and M2** as soon as Azure is ready (`mulca/prototype/webapp/README.md`,
   "Run it"): MUCA-Server with the coordinator on, `npm run build`,
   `python -m mulca_discord.web`, two browsers: create, join, paste the
   assignment, Confirm; then Discord `/coordinator kickoff` + Confirm. Note date
   and cost in TRACEABILITY.md and AZURE_SETUP.md.
3. **M3**: YAML strategies `cs5_owner_and_date`, `cs2_delegate_simple_task`,
   `cs3_close_decision` (each with "when NOT to fire"), vetoes in `policy.py`
   after `:671`, new proposal kinds (owner/date, delegation, decision) drawn on
   web cards and Discord embeds, **Why? / Not now** on every proactive card
   (Not now = 30 min silence per strategy + topic). Web: extend
   `ProposalCard.tsx` (KIND_TITLES, fields per kind) and `ClickFrame.action`.
4. Multichannel: verify A5 (adapter in the Discord process loop) and decide if a
   Discord channel and a web group share one conversation (today: `web:<code>`).

## Waiting on Matteo

- Azure deployments + `muca-server/.env`, then `scripts/azure_probe.py`.
- Discord test server and bot (`mulca/prototype/TRYING_ON_DISCORD.md`), token in `mulca/prototype/.env`.
- Vercel: token file `mulca/prototype/webapp/.env.vercel` (one line `VERCEL_TOKEN=...`;
  the file was not found on 15/11, check it is not saved as `.env.vercel.txt`) — or run the deploy himself.
- Confirm two decisions taken by Claude (webapp/docs/decisioni.md): film cut to
  4 clips / 18 s; web groups as their own conversations until A5/A6.

## Deploy (Vercel preview)

What goes online: only the **static site** (landing with the film, entry,
console UI). MUCA-Server and the web adapter are long-running processes with
WebSockets and stay on the demo laptop; on the preview the entry asks
`/api/health`, gets no answer and says that spaces run on the coordinator
server (forms disabled). Vercel Hobby plan, free; requested explicitly by
Matteo on 15/11 (exception to section 8 recorded in webapp/docs/decisioni.md).

- Build: `cd mulca/prototype/webapp && npm ci && npm run build` → `dist/` (31 MB:
  app + `film/desktop` 436 frames + `vercel.json` with SPA rewrites; `/api/*`
  deliberately not rewritten).
- Publish, option A (token): put `VERCEL_TOKEN=...` in `webapp/.env.vercel`, then
  `bash scripts/deploy-vercel.sh` (reads the token, never prints it).
- Publish, option B (from Matteo's terminal on Windows): `cd ...\webapp\dist`
  then `npx vercel deploy --prod` (browser login).
- Why not from the Claude shell with `vercel login`: the device-code login needs
  the CLI to keep running while Matteo approves, and the shell stops background
  processes at the end of each command (tried 15/11: approval lost). The Vercel
  MCP upload passes files through the chat, impossible for 31 MB.
- Vercel account: team "Matteo Cesandri's projects" (`team_99jBeqMuGqvTtcTgivvrX19s`);
  suggested project name `cadence-group-coordinator`.
- Later, to make the preview live: build with `VITE_ADAPTER_URL=https://<adapter>`
  and start the adapter with `MULCA_WEB_ALLOWED_ORIGINS=https://<vercel url>`;
  needs a public address for the adapter (a tunnel = new service: ask first).

## Where things are

- Work on branch `group17/coordinator` in both clones on Matteo's PC
  (`C:\Users\mcesa\WebstormProjects\group-coordinator`), mirrored to GitHub
  (`mchartt/G17-MULCA-Server`, `mchartt/G17-MULCA-Project`); GitLab untouched.
  All commits are **local**: nothing pushed in this session.
- Last commits: muca-server `0c606a9` (PROGRESS M2) + this update; mulca
  `f0ae328` (Cadence v2) + this update.
- Web app: `mulca/prototype/webapp/` (README.md first). Adapter:
  `mulca/prototype/mulca_discord/web/` (INDICE.md first). Film:
  `webapp/film/` (`lavori.json` = every Higgsfield job, check and decision).
  Build notebook: `webapp/docs/stato.md`; brief, design, decisions in `webapp/docs/`;
  screenshots `webapp/docs/screens/`.
- Claude project docs: `claude/contesto-group-coordinator.md` (same state, in Italian).

## How to rebuild the work environment (Claude app shell on Matteo's PC)

The shell is a Linux VM; only the connected folder survives between sessions.
1. Connect `WebstormProjects/group-coordinator` (device folder access); allow
   deletions in it (git needs to delete its lock files).
2. Python 3.11: `uv venv -p 3.11 ~/venv311`, then
   `VIRTUAL_ENV=~/venv311 uv pip install -e "muca-server[server,openai,storage,dev]" coverage`
   and the lines of `mulca/prototype/requirements*.txt` except `-e ../../MUCA-Server`.
3. Node: `npm ci` is very slow on the mounted Windows folder: copy the webapp
   (src, e2e, scripts, public, config files) to `~/build/webapp`, symlink
   `film` to the real one, `npm ci` there, build, copy `dist/` back.
4. Playwright Chromium needs `libXdamage.so.1`: `apt-get download libxdamage1`,
   `dpkg -x` into `~/libs/root`, `export LD_LIBRARY_PATH=~/libs/root/usr/lib/x86_64-linux-gnu`.
5. Never `pkill -f <pattern>` with a pattern that appears in your own command
   (it kills the command itself): start background processes with `& SP=$!` and `kill $SP`.
6. Copying files from the cloud workspace to the PC: give every new version a
   new file name (the same staged name may deliver a stale copy).

## Test status (15/11, late evening)

| Suite | Command | Result |
|---|---|---|
| MUCA-Server | `python -m pytest -q` | 481 passed + the 1 known voice failure |
| MULCA | `cd prototype && python -m pytest -q` | 274 passed (web adapter 16, coverage 98 %) |
| Web app unit | `npm test` | 17 passed |
| Web app end to end | `npm run e2e` (starts `e2e/fake_stack.py`) | 1 passed: two members, chat, kickoff card, Confirm, plan |
| Accessibility | `npm run axe` (fake stack running) | 0 WCAG 2.1 AA violations on landing and console |

## Cadence interface v2 (15/11, Matteo: "more premium, softer, Apple-like, no AI fonts")

- Font: SF Pro through the system stack on Apple devices (legal there), Geist
  (Google Fonts, self-hosted) elsewhere. Inter rejected: closest to SF but the
  most common "AI" font. Atkinson Hyperlegible (v1) rejected as not premium.
- Palette: #f5f5f7 mist, #1d1d1f ink, #636366 secondary, sage #2f6f5e actions,
  pastel tints; radii 22 px, pill buttons, soft shadows, glass nav and dialogs.
- Motion: Lenis smooth scroll; film titles scrubbed by the scroll (first visible
  at the top, last stays); headline lit word by word (grey #86868b → ink, AA
  for large text); one-time reveals with cubic-bezier(0.2, 0.8, 0.2, 1);
  messages and cards rise in. All off with prefers-reduced-motion.
- Icons: Font Awesome Free (CC BY 4.0, credited in the footer).
- Screens: `webapp/docs/screens/` (film, sections, entry, console, dialog, mobile).

## M2 log

- 15/11 · Landing film (Higgsfield, free trial): v1 stylised 3D rejected by
  Matteo ("less AI slop"), v2 photographic; 4 chained Seedance 2.0 clips, the
  5th (close-up) cut for artefacts. Higgsfield stretches the uploaded start frame
  by ~2 % vertically: measured with ECC and corrected on the tail of each clip
  (`webapp/film/raccorda.py`, no dissolves); 45 repeated frames removed. Final:
  436 frames, 18 s, 30 MB, continuity check PASS. Cost: 0. Log: `webapp/film/lavori.json`.
- 15/11 · Web adapter `mulca_discord/web/` (store, hub, app, runner): its own
  `SessionRegistry`, coordinator hook on the instance (Discord's class hook
  untouched, tested), `ReplyDispatcher` reused via `WebChannel`, channel "web".
  Tests: 15 (one end to end against a real in-process MUCA-Server), coverage 98 %.
- 15/11 · Frontend `webapp/` (React 19 + TS + Vite): landing (scroll film, 3
  sections, data notice, create / join), console (members, invite code, chat
  with agent label, proposal cards Confirm / Reject, plan panel, kickoff and
  profile dialogs, loading / disconnected / unavailable states). Vitest 15,
  Playwright e2e 1 (two members on the offline stack `webapp/e2e/fake_stack.py`),
  axe-core WCAG 2.1 AA: 0 violations on landing and console.
- 15/11 · Fresh-eyes review (self, against section 14 list): 1 defect fixed —
  chat frames handled in parallel could reach MUCA-Server out of order; now
  sequential (test added). MULCA suite 273 passed; server 481 + the known failure.
- 15/11 · Interface v2 (soft, Apple-like, see above); adapter gains
  `/api/health` and optional CORS (`MULCA_WEB_ALLOWED_ORIGINS`); entry shows the
  offline notice when the adapter is not there; Vercel config + deploy script.
  Found and fixed: dialogs shared one title id (accessibility), lit headline
  grey failed contrast (axe), first film title half-faded at the top.
- Limits: web groups are their own conversations (`web:<code>`), not yet the
  same as a Discord channel (A5/A6); deleting a group deletes only the web side
  (no protocol event for server memory); Why? / Not now come with M3.

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
| A5 | A FastAPI server can run in the same asyncio loop as discord.py | spike; today the web adapter runs as its own process (`python -m mulca_discord.web`) | before multichannel |
| A6 | Web-app members can be given stable int ids for `Participant.id` | **confirmed by test**: ids in [1e8, 1e9), the server accepts them (e2e confirm by a web member) | M2 |
| A7 | Slack app creation works in a free workspace | try it | only if Slack is kept |
