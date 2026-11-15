# TRACEABILITY — Group Coordinator

For every requirement, use case and strategy in the demo scope: status and
proof. Status: **DONE** (works channel → database → back on every planned
channel, tests green, live run noted), **PARTIAL**, **NOT DONE**,
**NOT VERIFIED (live)** (built and tested, live run missing), **OUT OF SCOPE**.

Requirement ids (N1–N4, G1–G6, FR, UC, CS) come from the technical spec v3
(Claude artifact "Documentazione tecnica v3"). Only items of the demo scope
(project instructions, section 7) are listed.

Last update: 15 Nov 2026 (M3).

## Platform rules (project instructions, section 4.3)

| Rule | Status | Proof |
|---|---|---|
| The model never writes state; a member's confirmation applies a proposal after re-validation | NOT VERIFIED (live) — tested for charter, task_owner, delegation, decision (`test_coordinator_m3_proposals.py`: stale task, owner left the group, wrong option, first click wins); an old charter card cannot overwrite a newer charter (`test_an_old_charter_card_cannot_overwrite_a_newer_charter`) | `proposals.py` `respond`; tests `test_coordinator_proposals.py` (all), `test_coordinator_router.py::test_kickoff_then_confirm_saves_the_charter` |
| First valid click wins | NOT VERIFIED (live) — tested | `store.py` `resolve_proposal`; `test_coordinator_store.py::test_two_simultaneous_clicks_apply_exactly_once` |
| Command or click from a non-member refused | NOT VERIFIED (live) — tested | server: `test_coordinator_router.py::test_a_non_member_cannot_run_any_command`, `test_coordinator_proposals.py::test_a_click_from_outside_the_group_is_refused`; Discord: `test_coordinator_discord.py::test_kickoff_card_and_confirm_end_to_end` (outsider click) |
| Protocol extended, not changed | NOT VERIFIED (live) — tested | `protocol/coordinator.py`; `test_coordinator_protocol.py`; full lab suite green (475 passed, 1 pre-existing failure) |
| Switch off = behaviour unchanged | NOT VERIFIED (live) — tested | `test_coordinator_switch.py` (4 tests off); lab suite green with the switch off |
| Model error or slow model never breaks the chat | NOT VERIFIED (live) — tested | `router.py` `route_coordinator`, kickoff in the background with a 30 s per-call limit (`test_kickoff_runs_in_the_background_and_only_once_at_a_time`); `test_..._router.py::test_an_unexpected_error_never_breaks_the_chat`, `test_kickoff_when_the_model_fails_says_unavailable` |
| Structured output validated with pydantic, one retry then silence | NOT VERIFIED (live) — tested | `writer.py`; `test_coordinator_writer.py` |
| Azure OpenAI through the existing client | NOT VERIFIED (live) | `client_config.py`; `test_coordinator_client_config.py`; probe not run yet |

## Demo scope

| Item | Status | Proof / what is missing |
|---|---|---|
| UC1 create/join space, data notice, profile | PARTIAL | `/coordinator profile` on Discord → server → `coord_member` (tests `test_coordinator_router.py`, `test_coordinator_discord.py`). Missing: web app (M2), live run |
| UC2 kickoff: assignment → deliverables and deadlines → charter card | PARTIAL | `/coordinator kickoff` → charter card in the channel → Confirm → saved; card redrawn as Confirmed (`test_coordinator_discord.py::test_kickoff_card_and_confirm_end_to_end`). Missing: "at most two questions", web app, live run with Azure |
| CS5 owner and date | NOT VERIFIED (live) — built and tested on server, Discord, web | `cs5_owner_and_date.yaml`; `proactive.py`, `proactive_writer.py`, `appliers.task_applier`; tests `test_coordinator_m3_*.py` (server), `test_coordinator_discord_m3.py::test_task_owner_card_*`, Vitest `ProposalCard.m3.test.tsx`. Missing: live run with Azure (the trigger is judged by the real intervention agent) |
| CS2 delegate a simple task | NOT VERIFIED (live) — built and tested | `cs2_delegate_simple_task.yaml`; Playwright `proactive.spec.ts` CS2 (card for two members, Why?, I'll take it, plan updated; trigger scripted on the offline stack); Discord `test_delegation_card_*`. Missing: live run |
| CS3 close a decision | NOT VERIFIED (live) — built and tested | `cs3_close_decision.yaml`; `appliers.decision_applier` (reason + `source`); Playwright CS3; Discord e2e `test_choose_on_discord_records_the_decision_end_to_end` (real local server). Missing: live run |
| UC4 /planning | NOT DONE | M4 |
| CS6 / UC6 /checkin | NOT DONE | M4 |
| CS7 blockers and dependencies | NOT DONE | M4 |
| UC9 "why did we decide X?" with source | NOT DONE | decisions table exists (`coord_decision.source`); M4 |
| G5 Why? and Not now on every proactive card | NOT VERIFIED (live) — tested | Why? from the card's `context` (strategy, topic, evidence copied by the server); Not now = status `snoozed` + `coord_snooze` 30 min (`test_not_now_snoozes_*`, `test_r6_*`, Playwright "Not now", axe 0 violations with Why? open). Helpful button (FR21) not built |
| UC9 project panel always visible | PARTIAL | server: `coordinator_state`; Discord `/coordinator plan` (private embed). Missing: web app panel (M2) |
| N3 calendar feed | NOT DONE | M5 |

## M3 deterministic rules (proactive.py)

| Rule | Status | Proof |
|---|---|---|
| R1 never in a DM | tested | `test_r1_never_in_a_dm` |
| R2 one draft at a time | tested | `test_r2_one_draft_at_a_time` |
| R3/R4 ≥ 10 messages and ≥ 5 minutes between cards (NFR3) | tested | `test_r3_r4_*`, `test_a_failed_draft_does_not_silence_the_group` |
| R5 never right after the agent (GL4) | tested | `test_r5_never_right_after_the_agent` |
| R6 Not now per strategy + topic, 30 min (GL10) | tested | `test_r6_a_snoozed_topic_never_gets_a_card` |
| R7 no CS5 for a task that has an owner | tested | `test_r7_*`, writer `task_id` check |
| Coordinator off: strategies not offered, policy unchanged | tested | `test_offered_to_the_model_only_when_the_coordinator_is_on`, `test_without_a_bridge_the_policy_is_unchanged` |

## Out of the demo (shown with the clickable prototype)

Majority vote on the charter, reflection report, CS1, CS4, CS8–CS11: OUT OF
SCOPE (project instructions, section 7). Slack: deferred, only if M4 is closed
by 29/11 (DECISIONS.md, 15/11).
