# TRACEABILITY — Group Coordinator

For every requirement, use case and strategy in the demo scope: status and
proof. Status: **DONE** (works channel → database → back on every planned
channel, tests green, live run noted), **PARTIAL**, **NOT DONE**,
**NOT VERIFIED (live)** (built and tested, live run missing), **OUT OF SCOPE**.

Requirement ids (N1–N4, G1–G6, FR, UC, CS) come from the technical spec v3
(Claude artifact "Documentazione tecnica v3"). Only items of the demo scope
(project instructions, section 7) are listed.

Last update: 15 Nov 2026.

## Platform rules (project instructions, section 4.3)

| Rule | Status | Proof |
|---|---|---|
| The model never writes state; a member's confirmation applies a proposal after re-validation | PARTIAL (charter only), tested; an old charter card cannot overwrite a newer charter (`test_an_old_charter_card_cannot_overwrite_a_newer_charter`) | `proposals.py` `respond`; tests `test_coordinator_proposals.py` (all), `test_coordinator_router.py::test_kickoff_then_confirm_saves_the_charter` |
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
| CS5 owner and date | NOT DONE | M3 |
| CS2 delegate a simple task | NOT DONE | M3 |
| CS3 close a decision | NOT DONE | M3 |
| UC4 /planning | NOT DONE | M4 |
| CS6 / UC6 /checkin | NOT DONE | M4 |
| CS7 blockers and dependencies | NOT DONE | M4 |
| UC9 "why did we decide X?" with source | NOT DONE | decisions table exists (`coord_decision.source`); M4 |
| G5 Why? and Not now on every proactive card | NOT DONE | M3 |
| UC9 project panel always visible | PARTIAL | server: `coordinator_state`; Discord `/coordinator plan` (private embed). Missing: web app panel (M2) |
| N3 calendar feed | NOT DONE | M5 |

## Out of the demo (shown with the clickable prototype)

Majority vote on the charter, reflection report, CS1, CS4, CS8–CS11: OUT OF
SCOPE (project instructions, section 7). Slack: deferred, only if M4 is closed
by 29/11 (DECISIONS.md, 15/11).
