# Multi-User Conversational Agent Server

MUCA-Server runs multi-party conversational agents behind one WebSocket:

- the agents and their provider connections (OpenAI Realtime);
- turn taking across several users, in text and in voice;
- cognition: when to speak and what to say;
- memory.

Clients stay thin. [MULCA](https://github.com/tmschoegje/MULCA) is the Discord client; the Unity client is another.

The wire protocol is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md). It is a superset of the Unity client's `Orchestration/Remote/OrchestrationServiceRemoteMessageReference.cs`.

```text
muca_server/
├───run_server.py: The composition root: real OpenAI agents, CoalaPolicy, SQLite memory.
├───main.py: create_app(), the FastAPI app with the WebSocket at "/".
├───agents/
│   │───conversation.py: Conversation — owns the agents, the turns, and every client message
│   │                    (the server-side port of the Unity SpeechToSpeechSession).
│   │───realtime_agent.py: Base agent; talks to no provider until a subclass overrides its hooks.
│   │───main_agent.py: The only agent that speaks to the users, so the only one that streams audio out.
│   │───auxiliary_agent.py: Agents that observe the conversation, with their own audio filters.
│   │───factory.py: Which agents a session gets (AgentFactory).
│   │───turn.py: One stretch of speaking in the conversation history.
│   │───instructions.py: Markers the agents read conversation events by.
│   │───options.py: Typed agent configuration, and the auxiliary agent types a client can ask for.
│   └───openai/: The OpenAI Realtime main and intervention agents, and the summarizer.
├───audio/
│   │───chunk.py: One audio frame as it travels on the WebSocket (float32 mono, 24 kHz).
│   └───pcm.py: float32 <-> PCM16 and resampling, matching the Unity PcmUtility.
├───cognition/
│   │───policy.py: CoalaPolicy — working memory, strategies, the management cycle.
│   │───ports.py: What cognition can ask of a conversation, and vice versa.
│   │───strategies.py, trigger_variables.py, working_memory.py, forced_intervention.py
│   │───memory/: Episodic log and snapshots: in memory, or SQLite (one file per conversation).
│   └───config/: Personas, strategies and tasks, as YAML.
├───events/
│   └───dispatcher.py: Forwards each validated client message to the conversation.
├───protocol/
│   │───incoming.py: Messages clients send.
│   └───outgoing.py: Messages the server sends.
└───sessions/
    │───session_runtime.py: One client connection: handshake, receive and send loops, errors.
    │───session_runtime_factory.py: Builds a session runtime per connection.
    │───session_components_factory.py: Builds the per-session components and wires them together.
    └───session_state.py: State class which identifies the session.
```

## Running

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[server,openai,storage,dev]"
.venv/Scripts/python -m muca_server.run_server
```

`uvicorn muca_server:app` also starts, but with agents that reach no provider. It's useful for protocol work only.

| Environment variable | Default | |
|---|---|---|
| `OPENAI_API_KEY` | — (required) | Read from the environment or a `.env` file. |
| `MUCA_SERVER_HOST` / `MUCA_SERVER_PORT` | `0.0.0.0` / `8000` | |
| `MUCA_DB_DIR` | `data/` beside the package | One SQLite file per `conversationId`. |
| `MUCA_SILENCE_TIMEOUT_MS` | `400` | How long without audio frames counts as silence. |
| `MUCA_LOG_LEVEL` | `DEBUG` | Only the `muca_server` logger tree -- the root (and third-party libraries like httpx/openai) always stays at `INFO`. Set to `INFO` for a quiet run; the `[voice-turn]`-tagged lines (every tick/event in the turn/barge-in machinery) are `debug`-level and otherwise flood normal operation. |
| `MUCA_DEBUG_PROVIDER_TTL_SECONDS` | unset | Testing only: end every provider connection after this many seconds. |

OpenAI ends every Realtime connection after 60 minutes. When a provider connection ends on its own, the server saves the conversation's memory and closes that client's session with code 1012. The client reconnects, and the new session reloads the memory. `MUCA_DEBUG_PROVIDER_TTL_SECONDS` triggers the same thing early, to test it without waiting an hour.

## Tuning

`CoalaPolicy.MANAGEMENT_CYCLE_INTERVAL` (`cognition/policy.py`) controls two coupled things: how many raw messages stay fully verbatim in `WorkingMemory` before being folded into the running `history_summary`, and how often the summarization LLM call runs. Currently `24`, sized for this project's actual target deployment: 10 teams x 6 tasks x ~30 min sessions each, one persistent `conversation_id` per team across all of them (nothing resets between tasks). A rough ~2-5 messages/minute estimate for an active group discussion puts one session at ~60-150 turns; `24` keeps several minutes of recent back-and-forth fully verbatim (not relying on the compressed summary for anything just said) without resending a large block on every single triggered reply, since that per-turn resend — not the summarization call itself — is the bigger cost driver (replies fire far more often than management cycles do).

The summarizer's own output budget (`agents/openai/main_agent.py`'s `ChatCompletionsSummarizer`, "Target roughly 1500 words") is a separate, *cumulative* target — each cycle revises the whole-conversation-so-far summary, not a summary of just that cycle's chunk, so it isn't mechanically coupled to `MANAGEMENT_CYCLE_INTERVAL`'s value. Sized against total deployment volume, not just one cycle: a team's conversation_id persists across all 6 of its tasks, so by the last task this summary is standing in for everything since the first -- a rough estimate puts that at 3,600-18,000 words of cumulative raw dialogue per team over the study. A small (e.g. 250-300 word) summary would mean an 8:1-36:1 compression ratio by the final session, severe enough to lose specific facts/decisions from early sessions entirely. 1500 words is a stopgap chosen to hold both task-process detail (who said what, why a decision was made) and domain-specific specifics (agreed-on design choices, so people don't have to repeat themselves) without yet being rigorously sized -- the prompt also tells the model an explicit priority order for what to keep if it has to compress something away (key decisions and outcomes, then process information, then task-specific details), rather than leaving that an implicit judgment call -- not live-verified against real multi-hour conversations, and not a fix for the structural issue that a fixed-size cumulative summary degrades over a long enough conversation regardless of its target size. `provide_summary`/`needs_history` retrieval (reads the full persisted episodic log directly, not this summary) is the actual mechanism for recovering specific facts this summary has compressed away.

If `MANAGEMENT_CYCLE_INTERVAL` is changed again, also check `WorkingMemory.transcript()` (`cognition/working_memory.py`): it used to have its own independent `limit` default, separate from and smaller than this interval, which silently re-truncated both the recent-context block and the summarizer's own input once the interval grew past it. That's been removed (`transcript()` now renders all of `wm.history`, whose size is controlled entirely by `MANAGEMENT_CYCLE_INTERVAL`/`apply_management_cycle`) specifically so the two can't drift out of sync again, but worth knowing why that coupling existed if it gets touched.

`WorkingMemory.SECONDARY_COOLDOWN_SECONDS` (`cognition/working_memory.py`, currently `10.0`) is a flat wall-clock floor on top of each strategy's own `cooldown_messages`, applying to the same strategies (none of it touches a strategy with `cooldown_messages: 0`). Found live: `participation_equality` refiring far more often, in real time, during a voice call than it felt like it should -- not a message-counting bug (one increment per completed turn, text or voice, via the one shared `on_utterance` path, confirmed identical for both), but a real consequence of voice producing more completed turns per real second than text, since people talk faster than they type. The same "N turns" cooldown contract was being honored correctly; it just permitted much more frequent real-time firing in the faster modality. The wall-clock gate is what actually bounds that, independent of how fast messages/turns are accumulating.

### In Docker

```bash
docker build -t muca-server .
docker run -p 8000:8000 -e OPENAI_API_KEY=... -v muca-data:/data muca-server
```

The image keeps its SQLite files in `/data`, so mount a volume there. It also reads any Docker secret under `/run/secrets/` as the environment variable of the same name, so `OPENAI_API_KEY` can come from a secret. The swarm deployment, together with MULCA, is in MULCA's `deploy/README.md`.

## Testing

```bash
.venv/Scripts/python -m pytest
```

The tests use agents that reach no provider. For a live check against OpenAI, start the server and run:

```bash
.venv/Scripts/python scripts/smoke_client.py --wav question.wav --out reply.wav
```

It asks one text question and one spoken question (a 16-bit PCM WAV of you asking MULCA something), then saves the spoken reply to `reply.wav`. Listen to that file: clean speech means the audio path works in both directions.
