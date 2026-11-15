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

M3, proactive cards:
  user_text -> Conversation -> intervention agent picks cs5/cs2/cs3
            -> policy.py on_intervention_signal -> proactive.py ProactiveBridge.offer
                 rules R1-R5 -> background: proactive_writer.py draft -> rules R6-R7
                 -> proposals.py create (pending) -> coordinator_proposal to every channel
```

The model never writes state: `writer.py` only drafts, `proposals.py`
saves the draft as **pending**, and only `respond(... "confirm" ...)` from a
group member applies it, inside one database transaction.

## Files

| File | Lines | Responsibility |
|---|---|---|
| `__init__.py` | 23 | Exports the public names. |
| `models.py` | 258 | The data: members, charter, milestones, tasks, decisions, proposals. |
| `store.py` | 389 | The only code that reads and writes the coordinator tables. |
| `proposals.py` | 272 | Life cycle of a proposal, the buttons, and the checks before applying. |
| `appliers.py` | 135 | The change each kind of card makes, inside the transaction (M3). |
| `plan_writes.py` | 109 | SQL writes of tasks, decisions and "Not now" used by the appliers (M3). |
| `writer.py` | 163 | Asks the model for a draft, in a fixed JSON shape, validated. |
| `proactive_writer.py` | 249 | Drafts CS5/CS2/CS3 cards, grounded on numbered chat lines (M3). |
| `proactive.py` | 186 | The bridge from a fired strategy to a card, with rules R1-R7 (M3). |
| `checkin.py` | 90 | M4: plan against reality, computed in code (late, blocked, due soon, no owner, milestone, dependency). |
| `plan_writer.py` | 230 | M4: model drafts for /planning, /checkin options, and answers to "why did we decide X?" with the decisions they rest on. |
| `plan_commands.py` | 175 | M4: the commands planning, checkin, task, why (one model command at a time, in the background). |
| `router.py` | 271 | Receives the coordinator messages of one session and answers them. |
| `settings.py` | 50 | The on/off switch, the proposal lifetime, the timed check-in interval and the text model, from the environment. |
| `card_requests.py` | 60 | Helpful (feedback on a proactive card, FR21) and the `card` command (a card sent back to the asker). No model, no state change. |
| `checkin_timer.py` | 90 | The check-in on time (FR15): every `MUCA_COORDINATOR_CHECKIN_HOURS`, card only when something is late or at risk. |
| `wiring.py` | 83 | Builds the per-session hook for `run_server.py` (or None when off). |

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
| `ProposalKind` | What a proposal changes: `charter` (M1), `task_owner` (CS5), `delegation` (CS2), `decision` (CS3). |
| `ProposalStatus` | `pending` → `applied` \| `rejected` \| `expired` \| `snoozed` (final states; snoozed = "Not now"). |
| `CharterPayload` | The content of a charter proposal; `_no_empty_deliverables` drops blank items and refuses items that are too long. |
| `Evidence` | One chat message behind a proactive card (speaker, text, time), copied by the server, never written by the model. Shown under "Why?". |
| `ProactiveContext` | Strategy id, topic (the key of "Not now") and 1–5 `Evidence`. |
| `TaskAssignmentPayload` | CS5 and CS2: `task_id` (existing task or None), `title`, `owner_id` (CS5 suggestion; CS2 always None), `due_date`, `return_to` (CS2). |
| `DecisionOption`, `DecisionPayload` | CS3: the question and 2–3 options, each with what the chat said for it. |
| `PlanItem`, `PlanPayload` | M4 /planning: tasks with suggested owner, reason, date; confirmed all together. |
| `CheckinOption`, `CheckinPayload` | M4 /checkin: findings from `checkin.py` + 1–3 options, each changing one task (date, owner, or drop; at least one change, checked by a validator). |
| `BlockerPayload` | M4 CS7: blocked task (or a new one), what it waits on, who could help, the question to ask. |
| `PAYLOAD_MODELS` | Which payload model belongs to which kind. |
| `Proposal` | One proposal: id, kind, status, payload, one-sentence `rationale`, channel of origin, created/expires, resolved by/at. `typed_payload()` validates the payload against its kind. |

## `store.py` — `CoordinationStore(db_path)`

Tables `coord_member`, `coord_charter` (one row), `coord_milestone`,
`coord_task`, `coord_task_dependency`, `coord_decision`, `coord_proposal`,
`coord_snooze`, `coord_mark` (last proactive card and last timed check-in,
`mark` / `save_mark`), `coord_feedback` (Helpful, `add_feedback`),
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
| `SNOOZE_SECONDS` | 30 minutes: how long "Not now" silences a strategy on a topic (spec FR21). |
| `ACTIONS` | The buttons: `confirm`, `take` ("I'll take it"), `choose` (+ `option`), `reject`, `not_now`. |
| `Outcome` | Result of a click: `ok`, a `message` for the member, the `proposal` as it is now. |
| `new_id()` | Short random id. |
| `create(kind, payload, rationale=, origin_channel=)` | Saves a new pending proposal; validates the payload first. The project state does not change. |
| `respond(id, action=, member_id=, participants=, option=)` | A click. Checks, in order: (1) the member is in the group, (2) the proposal exists and is pending, (3) it is not expired, (4) the button belongs to this kind of card, (5) re-validation on today's state, (6) applied atomically. |
| `_snooze(...)` | "Not now": only on proactive cards; status `snoozed` and a row in `coord_snooze`, in one transaction. |
| `_result(...)` | Re-reads the proposal; if nothing changed, says someone else answered first. |
| `_prepare(proposal, payload, action=, option=, ...)` | Which change this click makes: charter → confirm; task_owner → confirm (suggested owner) or take; delegation → take only (S2 never assigns); decision → choose. The owner must still be in the group. |

## `appliers.py` — the change of each kind of card

| Name | What it does |
|---|---|
| `StaleProposal` | Raised inside the transaction when the state moved on since the card was made; rolled back, card closed as expired. |
| `charter_applier(...)` | Replaces charter and milestones; refuses if a newer charter was confirmed after the card was made. |
| `task_applier(payload, owner_id=, new_task_id=)` | New task with owner and date; or an existing task gets its owner, only if it still exists and still has none. |
| `decision_applier(payload, option=, ...)` | The chosen option goes to the decision log with its argument as the reason and `source = proposal:<id>`. |
| `snooze_applier(strategy_id=, topic=, until=)` | Writes the "Not now" row. |
| `plan_applier(payload, new_id=)` | M4: every task of the /planning list enters the plan. |
| `checkin_applier(option)` | M4: the chosen option changes one task or drops it; stale if the task is gone. |
| `blocker_applier(payload, new_task_id=)` | M4: task marked blocked (added if missing) and the dependency recorded. |

## `plan_writes.py` — writes used inside the transaction

`topic_key(topic)` (lower case, letters and digits, single spaces, ≤ 60
characters: the deterministic key of "Not now"), `task_owner`, `add_task`,
`assign_task`, `update_task`, `delete_task`, `add_dependency`, `add_decision`,
`add_snooze`. None of them commits. `store.set_task_status` is the one direct
write outside a proposal: a member updating a task (their own action).

## `proactive_writer.py` — `ProactiveWriter(proposal_writer)`

| Name | What it does |
|---|---|
| `STRATEGY_KINDS` | `cs5_owner_and_date` → task_owner, `cs2_delegate_simple_task` → delegation, `cs3_close_decision` → decision, `cs7_blockers_dependencies` → blocker (M4). |
| `ChatLine` | One recent message (speaker, text, time) as the server saw it. |
| `ProactiveDraft` | A validated payload + its one-sentence rationale. |
| `SCHEMAS`, `COMMON_RULES`, `TASK_RULES` | Strict JSON shape and instructions per kind. The chat is numbered and quoted between `<chat>` tags: data, never instructions. |
| `draft(strategy_id, chat=, members=, profiles=, tasks=, silenced_topics=, today=)` | One call (+ one retry); None when it fails twice. |
| `_context(...)` | The user message: today, members with profiles, plan, silenced topics, chat. |
| `_validate(...)` | Evidence numbers → real messages; owner must be a member; task_id must be an open task without owner; no date in the past. Anything wrong = retry. |

## `proactive.py` — `ProactiveBridge(conversation, writer=, store=, service=, send_proposal=)`

| Name | What it does |
|---|---|
| `Verdict` | `not_mine` (a lab strategy: normal path), `vetoed` (silence), `taken` (silence now, card later). |
| `offer(strategy_id, history=, message_count=, assistant_id=)` | Called by `CoalaPolicy`; answers at once, drafts in the background. |
| `_veto(...)` | R1 group only, R2 one draft at a time, R3 ≥ 10 messages and R4 ≥ 5 minutes since the last card posted, R5 never right after the agent. |
| `_post(...)` | Draft, then R6 (topic silenced by "Not now") and R7 (CS5 on a task that already has an owner; CS7 on a task already blocked), then a pending proposal and the card. Any error is logged, the chat goes on. |
| `wait()` | Waits for a running draft (tests). |

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
| `call_json(system, user, schema)` | The actual Chat Completions call, same shape as `day_summarizer._call`; records the cost. Shared with `proactive_writer.py`. |

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
else = **off**), `MUCA_COORDINATOR_PROPOSAL_TTL_HOURS` (default 48),
`MUCA_COORDINATOR_CHECKIN_HOURS` (default 0 = check-in only on command),
`MUCA_COORDINATOR_MODEL` (default `gpt-4o`; on Azure the deployment name).

## `wiring.py` — `build_coordinator_factory(settings, client=, db_dir=, usage_tracker=)`

Returns None when off. When on, builds one `ProposalWriter` (with the lab's
cost recorder) and one `ProactiveWriter`, and returns a function that, for
each new session, creates a `CoordinatorRouter` whose store is the
conversation's own SQLite file (`db_path_for`), then `attach_proactive`
plugs the router's bridge into the session's `CoalaPolicy` (read as
`conversation._policy`, so the shared file is not changed).

## Hook points in shared files (the only lines changed outside this folder)

| File | Change |
|---|---|
| `cognition/policy.py` | M3: `set_proactive_bridge()` and one block in `on_intervention_signal`, after the existing vetoes: a coordinator strategy goes to the bridge and the turn ends in silence. Without a bridge (coordinator off) the block is skipped. |
| `run_server.py` | `COORDINATOR_STRATEGY_IDS` added to the ambient catalog and the policy store only when on. Client built by `agents/openai/client_config.py` (Azure or OpenAI); task `group_coordinator` when on; `coordinator_factory=` passed to the components factory. |
| `sessions/session_components_factory.py` | Optional `coordinator_factory`; when given, the router is the wrapper instead of the Conversation, and takes its worker slot (start/close, so the check-in timer stops with the session). |
| `main.py` | `DELETE /conversation` (gated on `MUCA_ADMIN_TOKEN`, like `/export`): removes a conversation's SQLite file. |
| `agents/openai/main_agent.py` | Prices of `gpt-6.1-sol` and `gpt-6-luna` added. |
| `events/dispatcher.py` | One `case` for the two new client events, which calls `route_coordinator` if the router has it. |
| `protocol/incoming.py`, `protocol/__init__.py` | The two new client events added to the union, and the exports. |
| `pyproject.toml` | The package listed. |

New outside this folder: `protocol/coordinator.py` (the five events),
`agents/openai/client_config.py` (OpenAI or Azure client),
`cognition/config/tasks/group_coordinator.yaml` (the task text).
