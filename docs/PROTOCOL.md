# MUCA-Server WebSocket protocol

This is the contract between MUCA-Server and a client: a Discord bot (MULCA), the Unity client, or any future text or voice service. A client handles its own platform: capturing and playing audio, and posting messages. MUCA-Server does the rest: the agents, turn taking, cognition, memory and the provider connections.

It is a superset of the Unity client's `Orchestration/Remote/OrchestrationServiceRemoteMessageReference.cs`. A client written against that file works unchanged.

## Connection

- **Transport:** WebSocket at `/`, for example `ws://localhost:8000/`. Every frame is one JSON object in a text frame.
- **One connection is one session**, and a session is one conversation. Open one connection per scope: per group, per DM, and so on.
- **First frame:** the client must send `connect` (or `connect_main_assistant`) within **5 s**. Otherwise the server closes the connection with code `1008`. If the main agent's provider connection fails, the server closes with `1011`.
- **Ending the session:** send `disconnect` with no `targetAgentGuid`. Dropping the connection also ends it.
- **Reconnecting:** if the connection drops, open a new one and replay `connect`, then your `session.update` if you send one, then `create_auxiliary_agent`, then `participants`. A session that gave a `conversationId` gets its memory restored from the server's store.

A minimal lifecycle:

```text
client                                   server
  | connect {conversationId, isDm}  ---->  |  main agent connects
  | create_auxiliary_agent          ---->  |
  |                                 <----  | agent_guid {guid}
  | participants [...]              ---->  |
  | user_text / audio_stream ...    ---->  |
  |                                 <----  | state_update / assistant_text ...
  | disconnect                      ---->  |  memory saved, socket closed
```

## Client → server

Every field name is camelCase. The server ignores fields it doesn't know, so the Unity client, which sends every field on every frame, is fine. `targetAgentGuid` set to empty or absent addresses the main agent.

| `event` | Fields | Meaning |
|---|---|---|
| `connect` / `connect_main_assistant` | `conversationId?`, `isDm=false`, `linkedConversationId?`, `mainModel?`, `interventionModel?` | Must be the first frame.<br>• `conversationId` names the conversation, so its memory outlives the session. Without it the session is ephemeral: nothing is persisted.<br>• `isDm`: a private conversation is never written to long-term memory.<br>• `linkedConversationId` (DMs only): the group this DM may read the memory of.<br>• `mainModel` / `interventionModel`: realtime models for this session instead of the server's. |
| `set_model` | `adminToken`, `model`, `includeIntervention=false` | Admin. Sets the server's main realtime model (and the intervention agent's too with `includeIntervention`), saves it, and ends every live session with 1012 so clients reopen on it. A wrong token, or no `MUCA_ADMIN_TOKEN` set on the server, answers `error`. |
| `create_auxiliary_agent` | `auxiliaryAgentType` | Adds an observer. Use `intervention_detection` (Unity's `InterventionDetectionAgentOpenAI` is also accepted) to decide when the assistant speaks. The server answers with `agent_guid`, or `error` for an unknown type. |
| `remove_auxiliary_agent` | `targetAgentGuid` | Removes an observer. |
| `participants` | `participants: [{id, name}]`, `modality="text"` | Everyone present, including people who haven't spoken. Send it again whenever it changes. `modality` ("text" or "audio") says which roster this is -- the server keeps text and voice rosters completely separate, since who's in one is not reliably who's in the other. Omit it (or send "text") for a text-channel roster; a voice client must send "audio" explicitly. |
| `user_text` | `speaker`, `speakerName?`, `text`, `mentionsAssistant?`, `attachments?` | One user's whole text message. It is answered in text. `mentionsAssistant`: the client saw its own @mention in it. `attachments`: `[{name, text, chars, readable}]`, the text the client read from attached files -- MULCA gets it in full, the observers a short note. |
| `audio_stream` | `speaker`, `isNotifying`, `playbackTimeMs`, `audio` | One tick of voice. See [Voice](#voice). |
| `playback_frame` | `audio` | Assistant audio the users just heard, echoed back for the observers. |
| `text_message` | `text`, `role="system"`, `targetAgentGuid?` | Injects text into one agent's context. It is not a user utterance. |
| `trigger_response` | `maxTokens?`, `interrupt=true`, `instructions=""`, `targetAgentGuid?` | Makes an agent respond now. A `maxTokens` value ≤ 0 or > 4096 means no limit. |
| `interrupt` / `interrupt_response` | `targetAgentGuid?` | Stops an agent. For the main agent, the server also cuts its reply at `playbackTimeMs`. |
| `disconnect` | `targetAgentGuid?` | Without a target, ends the session. With a target, disconnects one observer. |
| *(no `event`)* `session.update` | `type: "session.update"`, `session: {instructions?, output_modalities?}` | Configures the main agent, like the first JSON messages dea-multiuser sends after connecting.<br>• `instructions` replaces its whole instructions (the server's persona and task). Send it right after `connect`, before any turn.<br>• `output_modalities` (or dea-multiuser's `modalities`) without `"audio"` makes every reply text, spoken turns included, until a later `session.update` includes `"audio"` again. The assistant keeps listening. It can be sent at any time.<br>Nothing else is applied: the server owns the rest of the provider session (turn detection, audio, transcription). With a `targetAgentGuid` it is ignored. |
| *(no `event`)* | anything else | A provider passthrough frame, such as Unity's `SendAnythingAsync`. It is ignored, because the server owns every provider session's configuration. |

## Server → client

| `event` | Fields | Meaning |
|---|---|---|
| `state_update` | `isConnected`, `currentSpeaker`, `lastSpeaker`, `isAnswering`, `assistantSpokeLast`, `clearAudioQueue`, `audio` | Turn-taking state, and the assistant's speech.<br>• `currentSpeaker` is `-1` when nobody holds the floor; `0` is the assistant.<br>• `isAnswering` is true from when the reply becomes audible until playback drains.<br>• `clearAudioQueue` means stop playing now and drop everything queued. It is applied before this frame's `audio`.<br>• `audio`, if not empty, is more of the reply to queue for playback. |
| `assistant_text` | `text`, `isFinal`, `modality`, `interrupted` | What the assistant says.<br>• Deltas stream as they are produced; one frame with `isFinal: true` closes the reply.<br>• `modality: "text"` means this is the reply itself.<br>• `modality: "audio"` means this is the transcript of a spoken reply whose audio travels in `state_update`.<br>• `interrupted` (on the final frame only) means the reply was cut short. |
| `agent_guid` | `guid` | The guid of the observer just created. |
| `agent_message` | `guid?`, `text` | A raw provider event, as JSON, from the main agent (no `guid`) or an observer. Useful for debugging only. |
| `error` | `message`, `targetAgentGuid?` | A request failed. The session stays open. |
| `usage` | `agent`, `freshText`, `cachedText`, `freshAudio`, `cachedAudio`, `outText`, `outAudio`, `costUsd` | One provider call this session made, with its tokens and the server's cost estimate in USD, for a client that limits spending. `agent` is what made the call: `main`, `intervention`, `transcription`, `summary` or `embedding`. Clients that don't need it can ignore it. |

## Voice

### Audio format

On the wire, audio in both directions is **base64 of raw IEEE float32 little-endian samples, mono, 24 kHz**. That is the Unity client's format, and it matches `PcmUtility.cs`. The server converts to and from the provider's PCM16 itself.

Outgoing audio arrives split into frames of at most 3000 samples, so every frame stays under the Unity client's 16 KB read buffer.

### Sending `audio_stream`

Send `audio_stream` every 100–200 ms:

- **`speaker`:** the id of the one user holding the floor. MUCA assumes one speaker at a time, so the client picks the speaker and sends only that user's audio for the tick.
- **When nobody speaks, keep sending.** Use `speaker: -1` and `audio: ""`. These heartbeats keep the server's view of your playback exact. Clients that only send while someone talks, like the Unity client, still work: after `MUCA_SILENCE_TIMEOUT_MS` (400 ms) without frames the server treats the floor as free, and estimates playback from the audio it sent.
- **`isNotifying`:** true while you still have assistant audio queued to play, meaning more than one playback frame.
- **`playbackTimeMs`:** milliseconds of assistant audio played since the current continuous stretch of playback began. It resets to 0 whenever your playback queue runs dry. The server uses it to cut an interrupted reply at what the users actually heard.

### How a spoken turn is handled

1. The main agent's provider detects and transcribes speech. When a user's turn ends (they go silent), their transcript goes through cognition like a text message.
2. If cognition decides the assistant should reply, the reply is spoken. Its audio streams in `state_update.audio`, and its transcript in `assistant_text` with `modality: "audio"`.
3. The assistant's turn starts when you report playing it (`isNotifying: true`). It ends once generation is done and your playback drains.
4. **Barge-in:** if a user speaks over the assistant, the server stops the reply, cuts it at `playbackTimeMs`, and sends `clearAudioQueue: true`.

Text and voice can share one session, as with a Discord text channel and its voice channel. They share its memory. Each reply comes back in the medium its turn arrived in.

## Group Coordinator

Added for the AUI Group Coordinator (Group 17). Nothing above changes. The
server handles these events only when it runs with
`MUCA_COORDINATOR_ENABLED=true`; otherwise it logs the client events and
drops them. Clients that do not know the server events ignore them.
Code: `muca_server/protocol/coordinator.py`.

A proposal changes nothing until a member confirms it. The first valid click
wins; every later click is refused and every channel gets the updated card.

### Client → server

| Event | Fields | Meaning |
|---|---|---|
| `proposal_response` | `proposalId`, `action` (`confirm` \| `reject` \| `take` \| `choose` \| `not_now`), `option` (integer, only for `choose`), `memberId` (a `participants` id), `channel` (`discord` \| `web` \| `slack` \| `calendar`, default `discord`), `requestId` (echoed in the notice) | A member clicked a card button. Refused if `memberId` is not in the session's text roster. |
| `coordinator_command` | `command` (`profile` \| `kickoff` \| `state` \| `planning` \| `checkin` \| `task` \| `why` \| `ask` \| `language` \| `charter` \| `milestone`), `memberId`, `args` (object), `channel`, `requestId` (echoed back) | `profile`: `args` = `display_name?`, `discipline`, `skills` (list or comma text), `weekly_hours`. `kickoff`: `args.text` = the assignment text; answered with a charter card. `state`: answered with `coordinator_state`. M4: `planning`: `args.text` = tasks, one per line → a `plan` card. `checkin`: no args → a notice "on track", or a `checkin` card. `task`: `args.task` (task id or its number in the plan), `args.status` (`todo` \| `doing` \| `done` \| `blocked`) → applied at once (a member's own action), new state. `why`: `args.question` → a notice with the answer and its source (the decisions it rests on). `ask`: `args.question` (+ `args.maybe: true` when the message may not be for Cadence: an empty notice text = it stays silent) → a notice with Cadence's answer from the project (plan, decisions, documents, recent chat) and a `From:` line. When the member asks for a change ("crea una task per…", "aggiungi … come prerequisito", "sì, fallo"), the server may also send a pending `coordinator_proposal` (`task_owner` for one new task, `plan` for several, `blocker` with `certainty: possible` for a link, `checkin` with one option for a new owner, date, status or progress of a task): nothing changes until a member confirms it. `language`: `args` = `language` (`en` \| `it` \| `auto`): the language of everything the coordinator writes for the group; answered with a notice and the state (`state.language`, `""` = follows the chat). `charter`: `args` = `goal`, `deliverables?` (list, or text with one per line): a member edits the charter; no model call, answered with a `charter` card that is saved when a member confirms it (milestones unchanged). By hand (a member's own action, no card, no model): `task` also takes `owner` (member id, or `""` for nobody), `due` (`YYYY-MM-DD` or `""`), `title`, `remove: true`, or `add` (the title of a new task, with `owner`/`due`); `milestone` takes `add` (+ `due`), or `milestone` (its id) with `title`, `due` or `remove: true`. `planning` also takes `items` = `[{title, owner_id?, due_date?}]`: owners the members chose are kept, the rest are suggested; all chosen means no model call. `profile` also takes `background` (free text, up to 600 characters). |

### Server → client

| Event | Fields | Meaning |
|---|---|---|
| `coordinator_proposal` | `proposal`: `proposal_id`, `kind` (`charter` \| `task_owner` \| `delegation` \| `decision`), `status` (`pending` \| `applied` \| `rejected` \| `expired` \| `snoozed`), `payload`, `rationale`, `origin_channel` (`group` for proactive cards: show it in every channel), `created_at`, `expires_at`, `resolved_by`, `resolved_at` | A card to draw, or a new status for one already drawn. A `charter` payload has `goal`, `deliverables` (list), `milestones` (list of `title`, `due_date` ISO date or null). Proactive payloads (below) have a `context`: `strategy_id`, `topic`, `evidence` (list of `speaker`, `text`, `at_ms`), shown under "Why?". |
| `coordinator_state` | `state`: `charter`, `members`, `milestones`, `tasks` (each with `depends_on` and `dependency_sources`: where each link came from), `decisions`; `requestId` | The whole project state, after a change or when asked. |
| `coordinator_notice` | `ok`, `text`, `requestId` | A short answer to a command or a click: done, or why not. |

Proactive cards (M3), posted by the coordinator on its own when a strategy
fires. Buttons and the `action` they send:

| `kind` | Strategy | Payload besides `context` | Buttons |
|---|---|---|---|
| `task_owner` | `cs5_owner_and_date` | `task_id` (existing task or null), `title`, `owner_id` (suggested member or null), `due_date` | Confirm (`confirm`, only with a suggested owner), I'll take it (`take`), Reject, Not now |
| `delegation` | `cs2_delegate_simple_task` | `title`, `due_date`, `return_to` (topic to go back to); never an owner | I'll take it (`take`), Reject, Not now |
| `decision` | `cs3_close_decision` | `question`, `options` (2–3 of `label`, `argument`) | one per option (`choose` + `option` index), Reject, Not now |
| `blocker` (M4) | `cs7_blockers_dependencies` | `task_id` (plan task or null), `title`, `depends_on_task_id`, `depends_on_title`, `depends_on_owner_id` (who delivers new prerequisite work, or null), `helper_id`, `question`, `certainty` (`blocked` \| `possible`), `ways_forward` | Confirm (`blocked`: marks it blocked; both: records the dependency, adding the prerequisite task with that owner if new; a link that would make a loop is refused), Reject, Not now |

Cards answering a command (M4), no Why?/Not now:

| `kind` | Payload | Buttons |
|---|---|---|
| `plan` | `items` (1–15 of `title`, `owner_id` suggested or null, `reason`, `due_date`) | Confirm (adds every task), Reject |
| `checkin` | `findings` (computed by the server, not the model; for a task update asked in words, the member's message), `options` (1–3 of `label`, `task_id`, `new_due_date`, `new_owner_id`, `new_status`, `new_progress`, `drop`) | one per option (`choose` + `option`), Reject |

`not_now` closes the card as `snoozed` and silences that strategy on that
topic for 30 minutes. "Why?" needs no event: the card already carries the
strategy, the topic and the messages behind it.

These events are only for group conversations: in a DM or a session without
`conversationId` every command is answered with a refusing notice.

### Voice call (27/11)

A Discord voice call linked to a group (the client's `/coordinator voice`,
listening mode `ambient`) is part of the project:

- Every spoken turn, once transcribed (`gpt-4o-transcribe`, the Realtime
  session's own transcription), is kept in the channel's log as
  `"<name> (voice)"`. Cadence's `ask`, the catch-up and the card writers read it.
- In a linked group the web space gets it as `coordinator_mirror` with
  `channel` = `"discord_voice"` (shown as "Discord voice").
- The proactive strategies (CS2, CS3, CS5, CS7) list the `audio` modality: a
  task or a decision said aloud becomes a card, posted in the text channel and
  in every linked channel. The lab's unsolicited strategies stay silent in the
  call (R12); Cadence speaks aloud only when addressed.
- `coordinator_command` `transcript`: `args.minutes` (1–1440, default 60) → a
  notice with the spoken lines of that window, `HH:MM Name: text`, no model call.

Members across channels have one name each in a linked group (`names` on
cards and state): `"Ada (Discord)"` and `"Ada (Web)"` are two people, `"Luca 2"`
the second Luca of one channel. `/coordinator me` makes one person one member.
