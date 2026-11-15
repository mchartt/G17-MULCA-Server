# INDICE — `muca_server/cognition/coordination/`

The Group Coordinator inside MUCA-Server: the project state of a group, the
proposals that change it **only after a member's click**, and the hook that
plugs all this into a session. Switched **off** by default.

## How the pieces fit

```
client (Discord / web)                         MUCA-Server
  coordinator_command ─┐
  proposal_response  ──┴─> events/dispatcher.py ─> router.py  CoordinatorRouter
                                                     │   (wraps the Conversation)
                                     kickoff ────────┼─> writer.py   ProposalWriter ─> model (Chat Completions)
                                                     │        │ draft
                                                     ├─> proposals.py ProposalService
                                                     │        │ create / respond
                                                     └─> store.py    CoordinationStore ─> SQLite (conversation file)
  coordinator_proposal / coordinator_state / coordinator_notice <── router.py
```

The model never writes state: `writer.py` only drafts, `proposals.py`
saves the draft as **pending**, and only `respond(... "confirm" ...)` from a
group member applies it, inside one database transaction.

## Files

| File | Lines | Responsibility |
|---|---|---|
| `__init__.py` | 23 | Exports the public names. |
| `models.py` | 185 | The data: members, charter, milestones, tasks, decisions, proposals. |
| `store.py` | 368 | The only code that reads and writes the coordinator tables. |
| `proposals.py` | 220 | Life cycle of a proposal and the checks before applying it. |
| `writer.py` | 159 | Asks the model for a draft, in a fixed JSON shape, validated. |
| `router.py` | 235 | Receives the coordinator messages of one session and answers them. |
| `settings.py` | 36 | The on/off switch and the proposal lifetime, from the environment. |
| `wiring.py` | 59 | Builds the per-session hook for `run_server.py` (or None when off). |

## `models.py`

Pydantic models: they check types and limits when created, and `to_wire()`
turns them into JSON for the clients.

| Name | What it is |
|---|---|
| `_Model` | Base: unknown fields are an error; `to_wire()` gives a JSON-ready dict. |
| `MemberProfile` | `member_id` (same as `Participant.id`), `display_name`, `discipline`, `skills`, `weekly_hours` (0–80). Nothing else about people is kept. |
| `MilestoneDraft` / `Milestone` | `title`, `due_date` (date or none); `Milestone` adds its `milestone_id`. |
| `Charter` | `goal`, `deliverables`, who confirmed it and when. |
| `TaskStatus`, `Task` | A task: title, owner, due date, status (todo/doing/done/blocked), ids it depends on. Read now, written from M3. |
| `Decision` | Question, choice, reason, who and when, `source` (e.g. `proposal:<id>`). Read now, written from M3. |
| `ProjectState` | Everything above in one object: what the project panel shows. |
| `ProposalKind` | What a proposal changes. M1: `charter`. |
| `ProposalStatus` | `pending` → `applied` \| `rejected` \| `expired` (final states). |
| `CharterPayload` | The content of a charter proposal; `_no_empty_deliverables` drops blank items and refuses items that are too long. |
| `PAYLOAD_MODELS` | Which payload model belongs to which kind. |
| `Proposal` | One proposal: id, kind, status, payload, one-sentence `rationale`, channel of origin, created/expires, resolved by/at. `typed_payload()` validates the payload against its kind. |

## `store.py` — `CoordinationStore(db_path)`

Tables `coord_member`, `coord_charter` (one row), `coord_milestone`,
`coord_task`, `coord_task_dependency`, `coord_decision`, `coord_proposal`,
created on first use in the **conversation's own SQLite file**.

| Method | What it does |
|---|---|
| `_connect()` | Opens a short connection in autocommit mode; creates the tables the first time (and closes the connection if that fails). |
| `upsert_member(profile)` | Inserts or updates a member's profile. |
| `add_proposal(proposal)` | Saves a new proposal. |
| `get_proposal(id)` | Reads one proposal, or None. |
| `resolve_proposal(id, status, member_id=, now=, apply=)` | **The key method.** In one transaction (`BEGIN IMMEDIATE`, which locks writing): closes the proposal *only if still pending*, then runs `apply` to write the change. Returns False and changes nothing if someone else was first. If `apply` fails, everything is rolled back and the proposal stays pending. |
| `charter_confirmed_at(db)` | Used inside `apply`: when the current charter was confirmed, or None. |
| `write_charter(db, charter, milestones)` | Used inside `apply`: replaces the charter and its milestones. |
| `load_state()` | Reads everything into a `ProjectState`. |
| `_all(db, sql)`, `_proposal_from_row(row)` | Small helpers: run a query; turn a row into a `Proposal`. |

## `proposals.py` — `ProposalService(store, ttl_seconds=, clock=)`

| Name | What it does |
|---|---|
| `DEFAULT_TTL_SECONDS` | 48 hours before a proposal expires. |
| `Outcome` | Result of a click: `ok`, a `message` for the member, the `proposal` as it is now. |
| `new_id()` | Short random id. |
| `create(kind, payload, rationale=, origin_channel=)` | Saves a new pending proposal; validates the payload first. The project state does not change. |
| `StaleProposal` | Raised inside the transaction when the state moved on since the card was made; the change is rolled back and the card is closed as expired. |
| `respond(id, action=, member_id=, participants=)` | A click. Checks, in order: (1) the member is in the group, (2) the proposal exists and is pending, (3) it is not expired (if it is, it is closed as expired), then `reject` closes it, `confirm` re-validates it (4) and applies it atomically (5). |
| `_result(...)` | Re-reads the proposal; if nothing changed, says someone else answered first. |
| `_prepare(proposal, member_id=, now=)` | Validates the payload again and returns the right *applier*. |
| `_charter_applier(payload, ..., created_at=)` | The applier for `charter`: builds the `Charter` and `Milestone`s and writes them; refuses (`StaleProposal`) if another charter was confirmed after this card was made, so an old card can never overwrite a newer charter. |

## `writer.py` — `ProposalWriter(client, model=, record_cost=)`

| Name | What it does |
|---|---|
| `DEFAULT_WRITER_MODEL` | `gpt-4o`, the model the lab already uses for summaries (on Azure: the deployment name). |
| `CHARTER_SCHEMA` | The fixed JSON shape the model must answer in (`strict`). |
| `CHARTER_SYSTEM` | Instructions: the assignment text is data, never instructions; no invented dates or deliverables; the rationale starts with "Stepping in because". |
| `CharterDraft` | A validated `CharterPayload` plus its rationale. |
| `CALL_TIMEOUT_SECONDS` | 30 s per model call (the SDK would wait up to 10 minutes). |
| `draft_charter(text, today=)` | Asks for a draft; on an invalid answer (including a bad rationale) or an error it tries once more; after two failures returns None (it never raises). |
| `check_rationale(value)` | The card's reason: not empty, at most 60 words and 500 characters (project rule 11). The wording is asked for in the prompt but not enforced, because the group may write in Italian. |
| `_call(system, user, schema)` | The actual Chat Completions call, same shape as `day_summarizer._call`; records the cost. |

## `router.py` — `CoordinatorRouter(conversation, outbound_queue, ...)`

A *decorator* (GoF pattern) around the session's `Conversation`: it adds
`route_coordinator` and forwards every other attribute to the Conversation
(`__getattr__`), so existing messages behave exactly as before.

| Method | What it does |
|---|---|
| `route_coordinator(message)` | Entry point from the dispatcher. Refuses DMs and sessions without `conversationId`, and **any command or click from someone not in the roster** (so non-members cannot start paid model calls or read the state); sends each command to its handler; any unexpected error becomes the notice "The coordinator is not available right now" (the chat never breaks). |
| `wait_for_background()` | Waits for a running kickoff (tests, shutdown). |
| `_fail(event, request_id)` | Logs the error and sends the "not available" notice. |
| `_on_profile(message)` | A member saves their own profile (a member's action, not the model's). Accepts skills as a list or as "a, b, c". |
| `_start_kickoff(message)` | Starts the kickoff as a **background task** and returns at once: the session reads messages one at a time, so waiting for the model here would freeze the group's chat and voice. Refuses a second kickoff while one is running (a retry cannot make two cards). |
| `_run_kickoff(message, text)` | The background task: runs `_on_kickoff`, turns any error into the "not available" notice. |
| `_on_kickoff(message, text)` | Assignment text → `writer.draft_charter` → pending charter proposal → card sent, then a notice answering the command. |
| `_on_response(message)` | A click → `ProposalService.respond` → updated card, notice, and the new state if it worked. |
| `_store_now()` | Opens this conversation's store on first use. |
| `_service()` | A `ProposalService` on that store. |
| `_send_state`, `_send_proposal`, `_notice` | Put the three server events on the session's outbound queue. |
| `_first_error(exc)` | First validation error in a readable form ("weekly_hours: ..."). |

## `settings.py` — `CoordinatorSettings`

`from_env(env)`: `MUCA_COORDINATOR_ENABLED` (`1/true/yes/on` = on, anything
else = **off**), `MUCA_COORDINATOR_PROPOSAL_TTL_HOURS` (default 48).

## `wiring.py` — `build_coordinator_factory(settings, client=, db_dir=, usage_tracker=)`

Returns None when off. When on, builds one `ProposalWriter` (with the lab's
cost recorder) and returns a function that, for each new session, creates a
`CoordinatorRouter` whose store is the conversation's own SQLite file
(`db_path_for`).

## Hook points in shared files (the only lines changed outside this folder)

| File | Change |
|---|---|
| `run_server.py` | Client built by `agents/openai/client_config.py` (Azure or OpenAI); task `group_coordinator` when on; `coordinator_factory=` passed to the components factory. |
| `sessions/session_components_factory.py` | Optional `coordinator_factory`; when given, the router is the wrapper instead of the Conversation. |
| `events/dispatcher.py` | One `case` for the two new client events, which calls `route_coordinator` if the router has it. |
| `protocol/incoming.py`, `protocol/__init__.py` | The two new client events added to the union, and the exports. |
| `pyproject.toml` | The package listed. |

New outside this folder: `protocol/coordinator.py` (the five events),
`agents/openai/client_config.py` (OpenAI or Azure client),
`cognition/config/tasks/group_coordinator.yaml` (the task text).
