# PROGRESS — Group Coordinator

**Read this file first.** It says where the work really is. It is written so a
new chat can resume without the old one.

Last update: 15 Nov 2026, late evening. **M1–M5 built and tested offline;
the known limits closed (tested); Azure resource and deployments created;
online deploy prepared (Render + Vercel).** Nothing is DONE in the
TRACEABILITY sense until its live run is noted: the live runs need the Azure
key, which only Matteo pastes.

## Where we are

| Stage | Status |
|---|---|
| Phase 0 | DONE except the live Azure probe (needs the key) |
| M1–M5 | CODE DONE, NOT VERIFIED (live) |
| Known limits (R3/R4, Why? on Discord, Helpful, timed check-in, typed why on web, delete space) | CODE DONE + tests, NOT VERIFIED (live) |
| Azure | resource `g17-coordinator` France Central + 4 deployments; key in `.env`; **probe passed** (AZURE_SETUP.md). Budget alert: Matteo (page blocked by Cold Turkey) |
| Live run (web, local) | 15/11: kickoff, planning, check-in, typed why, CS3 card OK, ~$0.01 (DEMO_SCRIPT.md "Live runs") |
| Online | GitHub copies pushed (incl. branch `render`). Vercel **live**: https://cadence-group-coordinator.vercel.app. Render service `cadence-coordinator` (https://cadence-coordinator.onrender.com) builds; starts once Matteo sets `AZURE_OPENAI_API_KEY` and `MUCA_ADMIN_TOKEN` in its Environment |

## Next steps, in order (resume here)

1. Matteo: Render dashboard → service `cadence-coordinator` → Environment →
   add `AZURE_OPENAI_API_KEY` (KEY 1) and `MUCA_ADMIN_TOKEN` (button Generate)
   → Save, rebuild and deploy. Then open the Vercel site and run DEMO_SCRIPT.md.
2. Live runs still missing: CS5, CS2, CS7, Not now, Helpful, Delete space, Discord.
3. Updating online: commit, `bash deploy/render/update-branch.sh`, push
   `group17/coordinator` (Vercel redeploys) and `render` (Render redeploys).
4. Demo twice in a row; the report.

## Waiting on Matteo

- Azure for Students activation, deployments and `muca-server/.env`.
- Discord test server and bot, token in `mulca/prototype/.env`.
- Vercel token (`mulca/prototype/webapp/.env.vercel`, one line `VERCEL_TOKEN=...`)
  or `npx vercel deploy --prod` from his terminal in `webapp/dist`.
- Confirm two decisions taken by Claude: film cut to 4 clips / 18 s (webapp/docs/decisioni.md);
  /planning from a written list instead of the agent asking each member (DECISIONS.md, M4).

## Known limits (for the report)

- Closed on 15/11 evening (tested offline): R3/R4 across restarts, Why? on
  Discord after a restart, Helpful, timed check-in, "@coordinator why" typed in
  the web chat, Delete space on MUCA-Server too (TRACEABILITY.md).
- "@coordinator why…" typed on **Discord** still goes to the lab's agent: use
  `/coordinator why` there (changing the lab's `runtime.py` was not worth it).
- If Azure is unreachable, a session cannot start (the lab's main agent connects
  at session start), so every coordinator command answers "not available".
- Online (Render free): sleeps after 15 minutes idle, no persistent disk.
- The text of a "why" answer is the model's, grounded on the log; the source line is the server's.
- Any member can change any task's status (peer group, no owner-only rule).
- Web and Discord are separate groups by decision.
- Voice on Azure is not verified (AZURE_SETUP.md); our groups use text only.

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
  Commits of 15/11 night are local; the GitHub mirror is updated with
  `git push github group17/coordinator` in each clone (GitLab never).
- M3–M5 commits: muca-server `4ddaec3`, `5bfb579`, `f4974dc`, `76d5516` + this
  update; mulca `6dbad2c`, `484519a`, `b481a5e`, `b71ebb2`.
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

## Test status (15/11, late evening, after the limits)

| Suite | Command | Result |
|---|---|---|
| MUCA-Server | `python -m pytest -q` | 590 passed + the 1 known voice failure; coordinator modules 98 % covered (new ones 100 %) |
| MULCA | `cd prototype && python -m pytest -q` | 303 passed |
| Web app unit | `npm test` | 31 passed |
| Web app end to end | `npm run e2e` (starts `e2e/fake_stack.py`) | 8 passed: + typed why, Delete space; CS2 now also clicks Helpful |
| Render start script | `deploy/render/start.sh` run locally | both processes up, `/api/health`, CORS for the Vercel origin, WebSocket hello, Delete space; agent connection fails with a fake endpoint, as expected |
| Accessibility | `npm run axe` (fake stack running) | 0 WCAG 2.1 AA violations on landing, console with card, proactive card with Why? open, planning dialog, plan with status selects |

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

## M5 log

- 15/11 · Multichannel: Claude reported the risk of a full web–Discord merge
  (lab `runtime.py`, account linking, no live check possible); Matteo decided
  web and Discord stay separate groups (DECISIONS.md). Nothing built for it.
- 15/11 · Calendar feed in the web adapter (`web/calendar.py`, routes
  `/api/calendar`), "Add to my calendar" in the plan panel; access log off so
  feed secrets never reach a log. Tests: 4 pytest, 1 Vitest, 1 Playwright.
- 15/11 · `DEMO_SCRIPT.md` (exact messages per scenario, three spaces because
  of NFR3), `webapp/scripts/demo-up.sh` (MUCA-Server + adapter, checks `.env`
  and the build), README run command fixed (`python -m muca_server.run_server`).
- Fresh-eyes review (self, section 14 list) of M4–M5: no state written by the
  model (plan, check-in, blocker only after a click; task status and calendar
  link are members' own actions); every command and click checked against the
  roster first; flag off unchanged (switch tests green); protocol only
  extended (`planning`, `checkin`, `task`, `why`, new kinds, new actions).
  Fixed during the review: feed secret in the access log.

## M4 log

- 15/11 · Server: commands `planning`, `checkin`, `task`, `why` (protocol
  extended), `plan_commands.py` (one model command at a time, in the
  background), `plan_writer.py` (plan, check-in options, answers with the
  decisions they rest on), `checkin.py` (findings in code), kinds plan,
  checkin, blocker; strategy `cs7_blockers_dependencies` through the M3 bridge
  (R7: no card for a task already blocked). Tests: 30 new; server suite 571
  passed + the known voice failure; new modules 94–100 % covered. Cost 0.
- 15/11 · Discord: `/coordinator planning` (tasks separated by `;`), `checkin`,
  `task` (number from `/coordinator plan`), `why`; plan, checkin, blocker
  cards. Web: Plan tasks dialog, Check-in button, status select per task and
  "Ask why" in the panel; cards plan, checkin, blocker. MULCA 290 passed,
  Vitest 27, Playwright 5, axe 0 violations (also planning dialog and status selects).
- 15/11 · Found and fixed: a web frame that did not validate (e.g. an unknown
  command) left the button waiting forever; now its request is answered with
  a refusing notice (test).

## M3 log

- 15/11 · Server: strategies `cs5_owner_and_date`, `cs2_delegate_simple_task`,
  `cs3_close_decision` (YAML, each with "Do NOT fire for"), offered only with
  the coordinator on. Hook in `policy.py` after the existing vetoes →
  `ProactiveBridge` (rules R1–R7) → `ProactiveWriter` (numbered chat lines,
  evidence copied by the server) → pending proposal → card. New kinds
  task_owner, delegation, decision; buttons take, choose, not_now (protocol
  additive); appliers write tasks, decisions, snoozes inside the click
  transaction (`appliers.py`, `plan_writes.py`). Tests: 61 new; server suite
  542 passed + the known voice failure; new modules 96–100 % covered. Cost 0.
- 15/11 · Discord: embeds and persistent buttons per kind (I'll take it, one
  button per option, Why? privately, Not now); 9 new tests incl. an end to end
  "choose" against a real local server. Web: `ProposalCard` per kind with Why?
  and Not now; plan panel shows tasks and decisions before the charter;
  adapter forwards the new buttons. Vitest 23, Playwright 4 (charter; CS2 card
  + Why? + I'll take it; CS3 choice; Not now), axe 0 violations (also with
  Why? open). MULCA suite 284 passed.
- 15/11 · Found by the new e2e and fixed: a broadcast could reach a new socket
  before its hello, so an old member list overwrote the new one (per-group
  lock in `web/hub.py`, regression test that fails without it).
- Fresh-eyes review (self, section 14 list): no state written without a click
  (Not now is a member's click); non-members refused before any button; flag
  off = strategies not offered and policy unchanged (tests); protocol only
  extended. Recorded limits: R3/R4 counters reset on server restart; "Why?"
  on Discord after a bot restart shows only the reason; the "Helpful" button
  of FR21 is not built.
- Screens: `webapp/docs/screens/console-m3-delegation.png`, `card-m3-delegation.png`.

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
