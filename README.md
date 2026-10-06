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

`WorkingMemory.SECONDARY_COOLDOWN_SECONDS` (`cognition/working_memory.py`, currently `60.0`) is a flat wall-clock floor on top of each strategy's own `cooldown_messages`, applying to the same strategies (none of it touches a strategy with `cooldown_messages: 0`). Found live: `participation_equality` refiring far more often, in real time, during a voice call than it felt like it should -- not a message-counting bug (one increment per completed turn, text or voice, via the one shared `on_utterance` path, confirmed identical for both), but a real consequence of voice producing more completed turns per real second than text, since people talk faster than they type. The same "N turns" cooldown contract was being honored correctly; it just permitted much more frequent real-time firing in the faster modality. The wall-clock gate is what actually bounds that, independent of how fast messages/turns are accumulating.

`Strategy.modalities` (`cognition/strategies.py`, a YAML `modalities:` list, defaulting to `["text"]` when omitted) restricts a strategy to only the modality/modalities it's configured for. Enforced as a hard veto in `on_intervention_signal`, the same way the cooldown veto is, not just a steer in the observer prompt (though `on_utterance` also tells the observers which strategies apply to this turn's modality, so they don't bother picking one that will just be vetoed). Text and voice still share one `conversation_id`/memory/`CoalaPolicy` instance per team -- there's no separate policy or strategy catalog per modality, so this has to be a per-utterance check keyed off `Utterance.modality`, not something decided once per connection. `respond_when_addressed`/`provide_summary` are configured `[text, audio]` (both are solicited -- being addressed directly, or asking for a summary); everything else defaults text-only, since talking over a live conversation uninvited reads far more intrusive in voice than an unsolicited reply does in text, where a message is easy to scan past.

Participation equality is split into `participation_equality_text` and `participation_equality_voice` rather than one strategy covering both, because text and voice have entirely separate rosters and separate turn counts -- someone present in the voice call who never types has no text participation to measure, and vice versa. Each has its own roster (`ConversationPort.participants()` vs `.voice_participants()`, populated by a `ParticipantsMessage.modality` field the client tags "text" or "audio") and its own `gini_index`/`participant_message_counts` (computed from only that modality's slice of `wm.history`, merged into one `wm.trigger_variables` dict with the voice numbers `voice_`-prefixed, since every strategy shares that one dict regardless of which modality it's scoped to). On the Discord side, `BotRuntime.roster_text()`/`roster_voice()` replaced one `roster()` that used to return the *union* of text-channel and voice-channel members for both -- that union meant a voice-only participation strategy was really being judged against the text channel's membership, not who was actually on the call.

`respond_when_addressed` takes priority over `participation_equality_text`/`_voice` whenever both would otherwise apply to the same message -- answer a direct question now, and let an unequal-participation nudge wait for a later message if it's still relevant then. This is a prompt-level steer (both strategies' `trigger_description`s state the priority, from each side, so the observer doesn't have to find the rule in only one place), not a server-enforced veto: unlike cooldown or modality, the server has no independent ground truth for "was this message also a direct address" when the model's one pick was something else, so there's nothing to check the model's choice against the way cooldown/modality are checked. A fully deterministic version would need the intervention agent to report an `addressed` signal independently of its one strategy pick, so the server could veto `participation_equality_*` whenever that's true regardless of what else was selected -- not built, since a compound "answer and invite in the same reply" strategy isn't needed yet (the participation nudge can simply wait a turn). Live-verified once: a scripted conversation built genuine imbalance (`gini_index=0.44`, cooldown just cleared) and then directly addressed MULCA in the same turn -- the model picked `respond_when_addressed`, not `participation_equality_text`, and folded an invitation to the quiet participant into that one reply on its own initiative. One pass isn't a guarantee across every phrasing, but it's a positive real data point.

**`remember`** (`cognition/config/strategies/remember.yaml`) is cosine-similarity retrieval over a conversation's own episodic log, for "tell me something about X"-style questions -- distinct from `provide_summary`'s recency window, which answers "what's happened lately" rather than "what did we say about this one specific thing," and from `respond_when_addressed`'s `needs_history`, which still just resends the same recency window.

- **The topic, not the whole message, gets embedded and searched.** `cognition/query_extraction.py`'s `extract_query` is plain code (no model call) that strips a direct-address prefix ("MULCA, ...") and a small set of known wrapper phrasings ("tell me something about X", "what do you know about X", "remind me about X", ...), falling back to the whole message if nothing matches. Embedding "please tell me something about the espresso machine" whole would dilute similarity search with words the rest of the conversation never uses.
- **More wrapper-phrase variants are not worth adding, measured live, not assumed.** Querying the real embeddings API with the same target fact ("Grace brought a new espresso machine...") against increasingly verbose phrasings: a bare topic and a short wrapper both landed the correct match with a large margin (~0.42-0.44 over the second-best); a long-but-still-literal wrapper ("...mentioned something about the espresso machine situation...") was still correct, margin just thinner (0.18); but a genuine paraphrase with no literal topic words ("...somebody brought up something kitchen related...") **flipped to the wrong fact entirely** (margin 0.014, essentially a coin flip). The conclusion: cosine similarity already tolerates wrapper verbosity fine on its own, as long as the query's actual words overlap with the fact's -- more regex patterns would only target a case that was never actually broken. The real gap (a paraphrase sharing no vocabulary with what was said) can't be fixed by stripping known phrases at all; it would need a fundamentally different fix (e.g. an LLM reformulation step with access to recent conversation context) -- a bigger feature, not a regex tweak, and not built.
- **No vector DB.** `EpisodicStore.search_similar()` is brute-force cosine similarity (`cognition/memory/similarity.py`, plain `numpy`) over every embedding in one team's conversation. Sized for this project's actual scale (10 teams x 6 tasks over a 4-week study -- each team's whole log is at most a few hundred to roughly a thousand rows): measured at ~7-9ms at 1,000 candidates (~85-100ms even at 10,000, 10x beyond this project's real scale) -- negligible next to the embeddings API call itself (~200ms-3.7s measured live), nowhere near where an ANN index/vector DB would earn its cost. Candidates are passed through as numpy arrays end-to-end (`_unpack_embedding` doesn't round-trip through a Python list) -- measured ~25x faster than converting from nested Python lists at 10,000 candidates, which is what a naive implementation would do.
- **Embedded once, at log time**, not on every search: `SqliteEpisodicStore`/`InMemoryEpisodicStore` take an optional `Embedder` (`cognition/memory/ports.py`; `OpenAIEmbedder` in `agents/openai/embeddings.py`, `text-embedding-3-small`, reusing the same `AsyncOpenAI` client the summarizer does) and embed each turn's content as it's logged, stored in a side table (`episodic_embedding`) keyed off `episodic_log`'s row id -- the RAG extension point that table's docstring used to flag as "not built here." A failed embedding call is logged and skipped rather than losing the turn itself; a turn with no embedding is just never findable by `remember` until re-embedded.
- **No relevance threshold**: `search_similar` always returns its top `limit` (`CoalaPolicy.SIMILARITY_RETRIEVAL_LIMIT`, `8`) whenever any candidates exist for that conversation, even if none are a good match -- ranking, not filtering. "Nothing found" is only reported when the conversation has no logged content at all yet.
- **A failed search degrades instead of crashing the turn**: `_retrieve_similar` catches a `search_similar` exception (a misconfigured/missing embedder, a provider error) and tells the main agent to say it couldn't check, rather than letting the exception propagate -- found live, when an ephemeral (no `conversationId`) session's in-memory store had no embedder wired up at all and the whole turn silently died with no reply. Fixed by also giving `CoalaPolicy`'s ephemeral-session fallback store an embedder, not just the persisted path.
- Live-verified once: three unrelated facts logged (budget, a vendor deadline, an espresso machine), then "MULCA, please tell me something about the espresso machine" -- `remember` fired and pulled exactly the espresso-machine fact out of the three, correctly ignoring the other two.

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
