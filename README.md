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
| `MUCA_ADMIN_TOKEN` | unset | Shared secret for the `set_model` message, which changes the realtime model at runtime. Unset disables it. The choice is kept in `_runtime_config.json` in `MUCA_DB_DIR`. |
| `MUCA_MAX_SESSIONS` | `50` | Sessions open at once. Each holds two OpenAI Realtime connections (main and intervention agent); one more is refused with close code 1013. |
| `MUCA_LOG_DIR` | `logs/` in `MUCA_DB_DIR` | Daily log files (`muca-server.log`), 14 days kept. Every response's tokens and estimated cost are logged there. |
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

**`participation_equality_voice` is currently left out of `run_server.py`'s `ACTIVE_STRATEGY_IDS`.** It was briefly active (and live-verified firing correctly, roster and all), but the decision was reverted: voice should stay mention-only (`respond_when_addressed`/`provide_summary`/`remember`, all solicited), not also get an unsolicited participation nudge. The strategy, its yaml, and its voice-roster plumbing are all still in the codebase intentionally -- just not loaded -- in case that decision changes again.

`respond_when_addressed` takes priority over `participation_equality_text`/`_voice` whenever both would otherwise apply to the same message -- answer a direct question now, and let an unequal-participation nudge wait for a later message if it's still relevant then. This is a prompt-level steer (both strategies' `trigger_description`s state the priority, from each side, so the observer doesn't have to find the rule in only one place), not a server-enforced veto: unlike cooldown or modality, the server has no independent ground truth for "was this message also a direct address" when the model's one pick was something else, so there's nothing to check the model's choice against the way cooldown/modality are checked. A fully deterministic version would need the intervention agent to report an `addressed` signal independently of its one strategy pick, so the server could veto `participation_equality_*` whenever that's true regardless of what else was selected -- not built, since a compound "answer and invite in the same reply" strategy isn't needed yet (the participation nudge can simply wait a turn). Live-verified once: a scripted conversation built genuine imbalance (`gini_index=0.44`, cooldown just cleared) and then directly addressed MULCA in the same turn -- the model picked `respond_when_addressed`, not `participation_equality_text`, and folded an invitation to the quiet participant into that one reply on its own initiative. One pass isn't a guarantee across every phrasing, but it's a positive real data point.

**`respond_when_addressed`'s inferred-address case was too broad, found live.** Its original wording ("a short follow-up that plausibly responds to your own immediately preceding message") let a message clearly meant for the *group* fire it too, whenever that message happened to continue a topic MULCA had just mentioned -- e.g. MULCA answers a question about automation tools, then someone tells the group "everyone should look into automation tools more," and the old wording let that read as a reply to MULCA. Tightened to the actual principle: MULCA must be the **sole intended recipient** -- explicitly told not to fire for a message directed at the group even if it's on the same subject or would make sense as an answer to MULCA's own question if addressed to someone else. Live-verified: a group-directed comment on the same topic correctly got `strategy_id=None`, while a genuine one-on-one follow-up ("could you clarify what you meant by that?") still correctly fired `respond_when_addressed`.

**Tightened further: no inference at all, only two explicit cases.** Even "sole intended recipient" left room for the model to judge a reply as addressed to MULCA from tone/flow alone. Rewritten to exactly two conditions, nothing else: (1) an explicit @-mention or name/role mention ("MULCA", "assistant", "bot", ...), or (2) a reply to MULCA's own immediately preceding message that itself explicitly says "you" or names MULCA again. A reply to MULCA's message containing neither -- even one that's clearly continuing the same exchange -- no longer qualifies. Live-verified: after MULCA answered a direct question, a plain follow-up reply with no name or "you" ("let's go with rolling then") correctly got `strategy_id=None`, while "are you sure rolling is safe for us here?" (contains "you") correctly fired `respond_when_addressed`.

**`remember`** (`cognition/config/strategies/remember.yaml`) is cosine-similarity retrieval over a conversation's own episodic log, for "tell me something about X"-style questions -- distinct from `provide_summary`'s recency window, which answers "what's happened lately" rather than "what did we say about this one specific thing," and from `respond_when_addressed`'s `needs_history`, which still just resends the same recency window.

- **The topic, not the whole message, gets embedded and searched.** `cognition/query_extraction.py`'s `extract_query` is plain code (no model call) that strips a direct-address prefix ("MULCA, ...") and a small set of known wrapper phrasings ("tell me something about X", "what do you know about X", "remind me about X", ...), falling back to the whole message if nothing matches. Embedding "please tell me something about the espresso machine" whole would dilute similarity search with words the rest of the conversation never uses.
- **More wrapper-phrase variants are not worth adding, measured live, not assumed.** Querying the real embeddings API with the same target fact ("Grace brought a new espresso machine...") against increasingly verbose phrasings: a bare topic and a short wrapper both landed the correct match with a large margin (~0.42-0.44 over the second-best); a long-but-still-literal wrapper ("...mentioned something about the espresso machine situation...") was still correct, margin just thinner (0.18); but a genuine paraphrase with no literal topic words ("...somebody brought up something kitchen related...") **flipped to the wrong fact entirely** (margin 0.014, essentially a coin flip). The conclusion: cosine similarity already tolerates wrapper verbosity fine on its own, as long as the query's actual words overlap with the fact's -- more regex patterns would only target a case that was never actually broken. The real gap (a paraphrase sharing no vocabulary with what was said) can't be fixed by stripping known phrases at all; it would need a fundamentally different fix (e.g. an LLM reformulation step with access to recent conversation context) -- a bigger feature, not a regex tweak, and not built.
- **No vector DB.** `EpisodicStore.search_similar()` is brute-force cosine similarity (`cognition/memory/similarity.py`, plain `numpy`) over every embedding in one team's conversation. Sized for this project's actual scale (10 teams x 6 tasks over a 4-week study -- each team's whole log is at most a few hundred to roughly a thousand rows): measured at ~7-9ms at 1,000 candidates (~85-100ms even at 10,000, 10x beyond this project's real scale) -- negligible next to the embeddings API call itself (~200ms-3.7s measured live), nowhere near where an ANN index/vector DB would earn its cost. Candidates are passed through as numpy arrays end-to-end (`_unpack_embedding` doesn't round-trip through a Python list) -- measured ~25x faster than converting from nested Python lists at 10,000 candidates, which is what a naive implementation would do.
- **Embedded once, at log time**, not on every search: `SqliteEpisodicStore`/`InMemoryEpisodicStore` take an optional `Embedder` (`cognition/memory/ports.py`; `OpenAIEmbedder` in `agents/openai/embeddings.py`, `text-embedding-3-small`, reusing the same `AsyncOpenAI` client the summarizer does) and embed each turn's content as it's logged, stored in a side table (`episodic_embedding`) keyed off `episodic_log`'s row id -- the RAG extension point that table's docstring used to flag as "not built here." A failed embedding call is logged and skipped rather than losing the turn itself; a turn with no embedding is just never findable by `remember` until re-embedded.
- **No relevance threshold**: `search_similar` always returns its top `limit` (`CoalaPolicy.SIMILARITY_RETRIEVAL_LIMIT`, `8`) whenever any candidates exist for that conversation, even if none are a good match -- ranking, not filtering. "Nothing found" is only reported when the conversation has no logged content at all yet.
- **A failed search degrades instead of crashing the turn**: `_retrieve_similar` catches a `search_similar` exception (a misconfigured/missing embedder, a provider error) and tells the main agent to say it couldn't check, rather than letting the exception propagate -- found live, when an ephemeral (no `conversationId`) session's in-memory store had no embedder wired up at all and the whole turn silently died with no reply. Fixed by also giving `CoalaPolicy`'s ephemeral-session fallback store an embedder, not just the persisted path.
- Live-verified once: three unrelated facts logged (budget, a vendor deadline, an espresso machine), then "MULCA, please tell me something about the espresso machine" -- `remember` fired and pulled exactly the espresso-machine fact out of the three, correctly ignoring the other two.

**Redundant context resends, found during a real cost investigation.** The Realtime API is stateful -- both the main agent's and the intervention agent's live connections natively accumulate the whole conversation as it happens, turn by turn, for the life of that connection. Two places were resending content the live session already had, on every single triggered reply rather than once:
- `_render_prompt`'s `recent_block` (this conversation's own recent tail, verbatim) used to go out on *every* triggered reply. Now gated by `self._own_tail_sent`, sent once per connection only -- it exists purely to cover the one case the live session genuinely lacks it (freshly reconnected, `wm.history` restored from a snapshot the new session was never live for), not to repeat itself to a session that was there the whole time.
- A DM's mirrored read of its linked group (`summary_block`/`linked_tail_block` -- genuinely foreign content, a different conversation this session was never part of) used to be resent unconditionally on every DM reply. Now gated by `DM_FULL_CONTEXT_INTERVAL_SECONDS` (`cognition/policy.py`, currently `3600.0`, a judgment call not a measurement): re-sent only if more than an hour has passed since the last time, not on every reply in an active back-and-forth. Keyed on `MemorySnapshot.full_context_sent_at`, durably per-participant (one DM's own `conversation_id`, e.g. `dm:<user_id>`, never shared with any other DM or group) -- it has to persist across a reconnect, or the gate would silently reset every time a fresh `CoalaPolicy` is built (`session_components_factory.py` builds one per session), defeating the hourly interval entirely. DMs otherwise aren't written to long-term memory at all; this one field is the sole exception, since it carries no conversation content, just a timestamp.
- Separately, `observer_catchup_text()` (what a *reconnecting* intervention agent gets caught up with) was missing `history_summary` entirely -- only ever sent the raw recency tail, so a reconnecting observer had zero knowledge of anything a management cycle had already compressed away. Fixed to include both, matching what the main agent already gets.

Live-verified the main risk (does skipping the resend break recall, relying on the live session's own memory for something no longer explicitly repeated): told MULCA a fact on the first triggered reply, had two more unrelated triggered replies in between (each confirmed *not* to carry the tail/summary a second time), then asked it to recall the original fact -- it did, correctly, using only the live session's own native memory.

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
