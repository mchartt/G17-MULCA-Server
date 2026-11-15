# Integration map — Group Coordinator on MUCA-Server + MULCA

Phase 0, 15 Nov 2026. Every reference below comes from a file read or a
command run on the clones at:

- muca-server `gitlab-dev` @ `8699ef7` (branch `group17/coordinator`)
- mulca `gitlab-dev` @ `e200433` (branch `group17/coordinator`)

Line numbers are those of these commits. If a commit moves them, fix this
file **before** writing code that relies on it.

Legend: **VERIFIED (code)** = read in the source; **VERIFIED (run)** = seen
in a command's output; **NOT VERIFIED** = assumption, with the proof needed.

---

## 1. Path of a text message, Discord → reply

| Step | Where | What happens |
|---|---|---|
| 1 | `mulca/prototype/mulca_discord/runtime.py:928` `BotRuntime.on_message` | Discord message arrives; DMs and linked channels are recognised, others ignored. |
| 2 | `links.py:138` | A linked text channel's conversation id is `channel:<text_channel_id>`. |
| 3 | `sessions.py:150` `SessionRegistry`, `:193` `get` | One `ScopeSession` per conversation id, opened lazily (`_open`, `:385`). |
| 4 | `runtime.py:1019` / `:1034` | Text roster sent as `participants` (modality `text`). |
| 5 | `runtime.py:1086` → `muca_client.py:411` `send_user_text` | Sends `{"event":"user_text", ...}` on the WebSocket. |
| 6 | `muca-server/muca_server/sessions/session_runtime.py:199` `receive_loop` | `parse_incoming_message` (`protocol/incoming.py:262`), then `dispatch`. |
| 7 | `events/dispatcher.py:160` `case UserTextMessage()` → `:59` `route_user_text` | Forwarded to the conversation. |
| 8 | `agents/conversation.py:758` `route_user_text` → `:1761` `_judge` | `policy.on_utterance` (`cognition/policy.py:341`), then the observers (intervention agent) judge. |
| 9 | `cognition/policy.py:607` `on_intervention_signal` | Vetoes (section 2), then `_finish_turn` (`:1488`) asks the main agent to reply. |
| 10 | `agents/conversation.py:2346` | Reply streamed as `assistant_text` deltas + final frame (`protocol/outgoing.py:104`). |
| 11 | `muca_client.py:583` → `dispatch.py:91` `ReplyDispatcher` | Deltas become Discord messages, split at 2000 characters. |

**Text is always judged, whatever the listening mode** — `conversation.py:1791`:
the "window" gating applies only when `utterance.modality == "audio"`.
VERIFIED (code).

## 2. Existing vetoes (hook point for ours)

`cognition/policy.py:607` `CoalaPolicy.on_intervention_signal`:

- `:643` comment "Hard vetoes: not a request the model might ignore, the guarantee."
- `:652` cooldown veto (`wm.strategy_in_cooldown(strategy)`, message count,
  plus `WorkingMemory.SECONDARY_COOLDOWN_SECONDS` = 60 s wall clock).
- `:671` modality veto (`strategy.modalities`).
- `:832`–`:860` same two vetoes for "riders" (secondary strategies).

**Our hook:** one call after the modality veto (~`:680`) to a new
`CoordinatorVetoes.check(...)` living in `cognition/coordination/`. When the
coordinator is off, the call is skipped entirely. NOT VERIFIED (run) until
the M3 test with the flag off passes.

`policy.py` is 1702 lines: we add the hook only, no logic.

## 3. How `run_server.py` chooses task, persona and strategies

All in `muca_server/run_server.py` (the composition root):

| Line | What |
|---|---|
| `:52` | `TASK_ID = "general_moderation"` |
| `:58` | `ACTIVE_STRATEGY_IDS` (7 strategies) |
| `:75` | `PROACTIVE_STRATEGY_IDS` (removed from the toggle/transcribe catalog) |
| `:104` | `build_app()` |
| `:131` | **`client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])`** — the only OpenAI client, shared by every agent |
| `:132`–`:165` | three strategy stores (ambient, non-ambient + `always_reply`, wake_v2) and the policy's own store |
| `:155`, `:156` | personas `main_default`, `intervention_default` |
| `:167` | `load_task(TASK_ID)` |
| `:252` | `policy_factory` builds one `CoalaPolicy` per session |

Catalog per listening mode — `agents/openai/factory.py:76` `build_auxiliary`:
`wake_v2` → wake_v2 store (`:83`); `toggle`/`transcribe` → non-ambient store
(`:85`); everything else (`ambient`, `wake`) → full store. So coordinator
strategies added to the full store are visible in `ambient` and `wake`.
VERIFIED (code).

Strategy format — `cognition/strategies.py:17` `Strategy` dataclass: `id`,
`trigger_description`, `prompt_template`, `trigger_variables`,
`overlay_template`, `trigger_threshold`, `cooldown_messages` (default 0),
`modalities` (default `{"text"}`).

## 4. Server → client events, and how `muca_client.py` sorts them

Outgoing messages: `protocol/outgoing.py` — `StateUpdateMessage :16`,
`AgentMessage :87`, `AgentGuidMessage :98`, `AssistantTextMessage :104`,
`ErrorMessage :136`, `ListeningStateMessage :152`, `TurnSkippedMessage :164`,
`UsageMessage :176`. Base `OutgoingMessage :5` has `extra="forbid"`.

Client side `mulca_discord/muca_client.py:572` `_handle_raw`: `if/elif` on
`event` (`assistant_text :583`, `state_update :590`, `listening_state :593`,
`usage :596`, `turn_skipped :599`, `agent_guid :602`, `error :604`).
**Unknown event → `:612` `else`: logged at debug and ignored.** VERIFIED (code).
So a new server event (`coordinator_proposal`, `coordinator_state`) does
not break the existing client or other lab clients that use this file.

Client → server: `protocol/incoming.py:224` discriminated union on `event`.
**Unknown event → `parse_incoming_message` raises →
`session_runtime.py:205` `handle_protocol_error` (`:285`) only logs a
warning and the loop continues.** No error frame is sent back. VERIFIED (code).
So new client events (`proposal_response`, `coordinator_command`) must be
added to the union; an old server simply ignores them.

## 5. Summaries with Chat Completions and structured output

`agents/openai/day_summarizer.py:210` `_call`:
`client.chat.completions.create(model=..., messages=[system,user],
response_format={"type":"json_schema","json_schema": schema})`, schema with
`"strict": True` (`:141`), result `json.loads(...)`. Cost via `record_cost`
(`main_agent.py`). Models tried in order `[daily_model, model]` (`:193`),
falling back on error. **Our proposal writer copies this pattern** (same
client, same model, pydantic validation of the JSON).

## 6. Model names and the OpenAI client

| Constant | File:line | Model | Used for |
|---|---|---|---|
| `DEFAULT_REALTIME_MODEL` | `agents/openai/main_agent.py:156` | `gpt-realtime-2.1-mini` | main agent (Realtime) |
| `DEFAULT_MODEL` | `agents/openai/intervention_agent.py:37` | `gpt-realtime-2.1-mini` | intervention agent (Realtime) |
| `SUMMARY_MODEL` | `main_agent.py:157` | `gpt-4o` | running + day summaries |
| `DAILY_SUMMARY_MODEL` | `day_summarizer.py:24` | `gpt-5` | daily summary (env `MUCA_DAILY_SUMMARY_MODEL`) |
| `TRANSCRIPTION_MODEL` | `main_agent.py:158` | `gpt-4o-transcribe` | voice only — not needed by us |
| `EMBEDDING_MODEL` | `agents/openai/embeddings.py:13` | `text-embedding-3-small` | `remember` |
| `DEFAULT_WEB_SEARCH_MODEL` | `agents/openai/web_search.py:27` | `gpt-4o-mini` | Responses API + `web_search` tool |

Realtime models can also be changed at runtime (`model_settings.py`,
`set_model` message, admin token) and per session (`ConnectAgentMessage.
main_model` / `intervention_model`, `incoming.py:144`).

Realtime connections: `intervention_agent.py:119`, `main_agent.py:397`,
`transcription_agent.py:106`, all `self._client.realtime.connect(model=...)`.
Session settings for the intervention agent: `output_modalities: ["text"]`,
tools, `server_vad` with `create_response: False` (`intervention_agent.py:126`).

**Azure change = only line `run_server.py:131`.** See AZURE_SETUP.md.

## 7. SQLite stores per conversation

`cognition/memory/sqlite.py` (644 lines): `db_path_for(conversation_id,
base_dir)` turns the id into a safe file name; `SqliteEpisodicStore`
(episodic log + `episodic_embedding` side table), `SqliteSnapshotStore`
(working-memory snapshots). Built in `run_server.py` `store_factory`.
`MUCA_DB_DIR`, default `data/` beside the package. **Our tables go in the
same file** (new module, own `CREATE TABLE IF NOT EXISTS`). `sqlite.py`
opens one `aiosqlite.connect(self._db_path)` per call (`:289`, `:336`,
`:362`, ...): VERIFIED (code). Our store does the same, so no shared
connection to coordinate.

## 8. Tests without a provider and with a fake Discord

- Server: `tests/test_agents.py:19` `make_conversation()` builds a
  `Conversation` with no provider; `tests/test_text_conversation.py:101`
  `ScriptedAgentFactory`, `:257` `create_app(components_factory=
  DefaultSessionComponentsFactory(agent_factory=ScriptedAgentFactory(),
  policy_factory=lambda context: AlwaysAnswer()))` + FastAPI `TestClient`
  over the WebSocket. Shared fixture: `tests/conftest.py:6`
  `_cancel_leftover_tasks`.
- MULCA: plain fakes in the tests, e.g. `tests/test_bots.py:22` `FakeUser`,
  `:28` `FakeRegistry`, `:38` `FakeGatewayClient`.

## 9. MULCA: one session per group, channels merged

- `sessions.py:53` `Scope(conversation_id, is_dm, linked_conversation_id,
  bot_name, listening_mode, channel_name, course)`.
- Text and voice of one link share `channel:<text id>` (README "Voice").
- **Two connections with the same `conversationId` on the server:**
  `main.py:64` builds a new runtime per WebSocket and
  `session_components_factory.py:68` a new `CoalaPolicy` per session; there
  is no registry by conversation id. Two connections would be two
  independent policies writing the same SQLite file. **Not supported** —
  VERIFIED (code). Decision: every channel of a group (Discord, web,
  Slack) goes through the one `ScopeSession` the MULCA hub opens.

## 10. Persistent buttons and slash commands

- `commands.py:1296` `ListenView(discord.ui.View)`: `timeout=None` and a fixed
  `custom_id` (`:1310`); registered once with `client.add_view(...)` (`:1357`)
  so buttons keep working after a restart.
- `commands.py:1340` `MulcaGroup(app_commands.Group)`; `@tree.command` at
  `:1389`, `:1419`, `:1449`, `:1458`; subgroups `:1853`, `:1895`.
- Installed by `install_commands(manager)` (`__main__.py:80`).
- `commands.py` is 2009 lines: `/coordinator` goes in a **new file**
  (`coordinator_commands.py`), registered with one line next to
  `install_commands`.

## 11. Participants

`protocol/incoming.py:76` `Participant(id: int, name: str)`, `:86`
`ParticipantsMessage(participants, modality: "text"|"audio")`, pushed by the
client (`runtime.py:1034`). Member identity for proposal clicks will use
these ids. NOT VERIFIED: how web-app members (no Discord id) get an int id —
decided in M2 (DECISIONS.md).

## 12. Text-only listening mode

`ConnectAgentMessage.listening_mode` (`incoming.py:159`):
`ambient | toggle | transcribe | wake | wake_v2`, server default
`MUCA_LISTENING_MODE` = `wake` (`run_server.py`). `NON_AMBIENT_LISTENING_MODES =
("toggle","transcribe","wake")` (`conversation.py:83`). For text-only groups
any mode works (section 1); we link coordinator groups with `mode: ambient`
to make the intent explicit and leave voice untouched.

---

## Known traps found in Phase 0

- `mulca/prototype/requirements-dev.txt` installs `-e ../../MUCA-Server`:
  works on Windows (case-insensitive), fails on Linux with the clone
  named `muca-server`. VERIFIED (run).
- Windows clones use CRLF; a Linux shell sees every file as modified unless
  `core.autocrlf=true` is set in the local config. VERIFIED (run).
- Baseline: 2 server tests fail before any change (see PROGRESS.md).
