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
| `MUCA_LOG_LEVEL` | `INFO` | |

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
