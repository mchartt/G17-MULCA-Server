# INDICE — `scripts/`

Hand-run tools. None of them is imported by the server.

| File | Origin | What it does |
|---|---|---|
| `smoke_client.py` | existing (lab) | Live check against OpenAI: one text question and one spoken question, saves the spoken reply to a WAV. See the main README, "Testing". |
| `azure_probe.py` | **Group Coordinator, Phase 0** | Calls each model once on Azure OpenAI, exactly the way the server does, and prints outcome, latency and tokens. Proof for `docs/coordinator/AZURE_SETUP.md`. |

## `azure_probe.py`, part by part

Run: `python scripts/azure_probe.py [--only realtime|summary|embedding|web_search]`.
Needs `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` in the environment or `.env`.

| Part | What it does |
|---|---|
| constants `REALTIME_DEPLOYMENT`, `SUMMARY_DEPLOYMENTS`, `EMBEDDING_DEPLOYMENT`, `WEB_SEARCH_DEPLOYMENT` | The model names the server uses; on Azure they are also the deployment names. |
| `build_client()` | Builds one `AsyncOpenAI` pointed at Azure: `base_url` = `<endpoint>/openai/v1/` for REST calls, `websocket_base_url` = `wss://.../openai/v1` for Realtime. This is the same code M1 will put in `run_server.py`. |
| `report(name, ok, started, detail)` | Prints one line: OK/FAIL, latency in ms, details (tokens or the error). |
| `probe_realtime(client)` | Opens a Realtime session in **text only** (like the agents do), sends "Say ok.", reads events until `response.done`, prints input/output tokens and the reply. |
| `probe_summary(client, deployment)` | Chat Completions with a strict `json_schema`, the same call shape as `day_summarizer._call`. Run for `gpt-4o` and `gpt-5`. |
| `probe_embedding(client)` | One embedding of a short text; prints tokens and vector size. |
| `probe_web_search(client)` | Responses API with the `web_search` tool. **Optional**: the coordinator does not need it, a failure is only noted. |
| `main(only)` | Loads `.env`, runs the probes in order (or just one), each with a time limit. |

Every probe catches its own error and prints it, so one failing model does
not hide the results of the others. The key is never printed.

## Live evaluation scripts (real model, Azure credit: Matteo's OK)

| Script | What it does |
|---|---|
| `live_ask_corpus.py` | 12 cases of Cadence's answers (ask path). |
| `live_conversation_corpus.py` | LIVE (real model, Azure credit): 52 conversational cases (regression / dev / holdout) on the ask path and the CS7/CS5 card writer; deterministic checks on actions (ids, direction, owner). See `docs/coordinator/CONVERSATION_EVAL.md`. |
| `live_cs7_corpus.py` | LIVE (real model, Azure credit): CS7 implicit dependencies, 26 cases (dev / holdout of other domains, minimal pairs IT/EN). `writer` level: `ProactiveWriter.draft` + the server's R7 on every case, as if the judge had fired. `judge` level: end to end against a running server, judge picks per turn read from the server log (`--log`). See `docs/coordinator/CONVERSATION_EVAL.md`, "CS7 implicit dependencies". |
| `live_rasd_e2e.py` | LIVE end-to-end against a running server: the RASD / Matías scenario (card, confirm, state = what the Dependencies board draws, follow-ups, prerequisite done). `--ask` for the direct-request path. |
