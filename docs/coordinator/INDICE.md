# INDICE — `docs/coordinator/`

The Group Coordinator's project memory. Every file is updated in the same
change that affects it. Read `PROGRESS.md` first.

| File | What it is for | When it changes |
|---|---|---|
| `INDICE.md` | This list. | A file is added or removed here. |
| `PROGRESS.md` | Real status of every stage (Phase 0, M1–M5), the baseline test results, the plan of each stage (files, hook points, protocol events, tables, tests, closing proof), and the assumptions not yet confirmed. **First file to read.** | End of every work item. |
| `INTEGRATION_MAP.md` | How the existing MUCA-Server and MULCA code works, with **file and line**: path of a text message, existing vetoes, how `run_server.py` picks task/persona/strategies, events in both directions and what happens to unknown ones, summaries with structured output, every model name, SQLite per conversation, tests without a provider, sessions per group, persistent buttons and slash commands, participants, listening modes. Each claim is marked VERIFIED (code), VERIFIED (run) or NOT VERIFIED. | Before writing code that relies on a line that moved. |
| `AZURE_SETUP.md` | Region, deployments (names = model names), portal steps one by one, budget alert, environment variable names (never values), how the client will reach Azure, results of the probe script. | When Azure is set up or the probe is run. |
| `DECISIONS.md` | Dated decisions with alternatives and reasons, newest first; superseded decisions stay, marked as such. | Every decision. |

Still to be created (planned, not yet written):

| File | Stage |
|---|---|
| `TRACEABILITY.md` — status and proof of every FR, use case and strategy in scope | M1 |
| `DEMO_SCRIPT.md` — exact steps and messages of every demo scenario | M5 |

Related code outside this folder:

- `scripts/azure_probe.py` — calls each model once on Azure (see `scripts/INDICE.md`).
