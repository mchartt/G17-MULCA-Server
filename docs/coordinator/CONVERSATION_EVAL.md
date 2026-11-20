# CONVERSATION_EVAL — conversational optimization, 20 Nov 2026

Failure ledger, prompt/version history and evaluation results of the
conversational work on Cadence (routing, knowledge, dependencies, actions,
CS5/CS7). Raw outputs of every live run are in `live/` (named below).
Status labels as in CLAUDE.md. Nothing here is VERIFIED LIVE on the deployed
app: every live run used a local server and the real Azure models.

## Setup

| Item | Value |
|---|---|
| Code at start | Server `f92c5e2`, Project `c56f2ca` (both = `gitlab-dev`, 20/11) |
| Text model (ask, cards) | `MUCA_COORDINATOR_MODEL` = `gpt-6.1-sol` (Azure deployment) |
| Judge (does a strategy fire) | `gpt-realtime-2.1-mini` (Realtime, unchanged) |
| Live server | `python -m muca_server.run_server`, coordinator on, throw-away `MUCA_DB_DIR` |
| Cost / tokens | NOT MEASURED per run (the scripts do not read the usage tracker); one corpus run ≈ 52 text calls |
| Latency | corpus: 4–6 s per case including retries (from the run times below) |

## Baseline (before any change)

| Check | Result | Evidence |
|---|---|---|
| pytest server | 728 passed (744 after: +16 new tests) | run 20/11 13:28 |
| pytest client (`prototype`) | not run before the change; 362 passed after | — |
| Live e2e RASD/Matías (`scripts/live_rasd_e2e.py`, CS7 path) | **1/2**: run 1 card OK but prerequisite with no owner and Matías as "helper"; run 2 **no card** | server log 13:30–13:34 (console only, summarized in F1/F2) |
| Live ask corpus (`scripts/live_ask_corpus.py`) | 24/24 (morning of 20/11) | `live/ASK_2026-11-20.txt` |
| Natural-language actions | none: Cadence could only describe a change | code read: `plan_commands._ask` |

The new corpus (`scripts/live_conversation_corpus.py`) cannot run on the
baseline code: its action cases need the `action` field added in F5.

## Failure ledger

| # | Failure (reproduced) | Layer / root cause | Fix | Proof | Status |
|---|---|---|---|---|---|
| F1 | RASD message got no CS7 card: the judge picked CS5 on "Bello, gli do un'occhiata stasera" first, then CS7 7 s later was vetoed by R2 "a draft is already running" | Policy (R2) + CS5 trigger over-firing | R2 priority: CS7 > CS3 > CS5 > CS2; a more urgent strategy replaces a draft still in its model call (`proactive.py` `PRIORITY`, `_preempt`). CS5 do-not-fire: a member's own small next step said in passing (`cs5_owner_and_date.yaml`) | `test_a_blocker_replaces_an_owner_card_still_being_drafted`, `test_a_less_urgent_strategy_still_waits_for_the_running_draft`; live e2e 3/3 after (CS5 did not fire) | IMPLEMENTED/TESTED OFFLINE + REAL-MODEL TESTED (preemption itself not observed live) |
| F2 | The prerequisite the CS7 card adds had no owner although the chat named Matías; Matías was put as "who could help" | Payload lacked the field; prompt | `BlockerPayload.depends_on_owner_id` (member only, never a guess, ignored for an existing task); confirm gives the new task that owner; card shows "Delivered by" | `test_asked_to_add_a_prerequisite_...`, corpus `rasd: cs7 card ...` 4/4; web `ProposalCard.u3.test.tsx` | REAL-MODEL TESTED |
| F3 | A link made by confirming a card skipped the loop/self checks the by-hand path had | Domain logic (`plan_writes.add_dependency`) | One check `check_dependencies` for every way a link is made; a loop at confirm time closes the card, nothing saved | `test_confirming_a_card_that_would_now_close_a_loop_saves_nothing` | IMPLEMENTED/TESTED OFFLINE |
| F4 | A link kept no origin: "why are these linked?" had no answer | Data model | `coord_task_dependency.source` (additive column, old rows = unknown); "card <id>: <chat line>", "by hand (<member>)", or Cadence's suggestion with its reason; shown to the ask prompt and on the Dependencies page ("Origin of this link not recorded" otherwise) | `test_a_file_from_before_link_sources_...`, `test_a_link_made_by_hand_...`, `Pages.test.tsx`; corpus `provenance` 2/2 | REAL-MODEL TESTED |
| F5 | "Aggiungi la dipendenza", "crea una task per Matías", "Sì, fallo" produced only text | Missing capability | The ask answer has an `action` (`new_task` / `dependency`), checked by the server (`_ask_action`: ids, members, dates, self link) and turned into the existing card kinds (`task_owner`, `blocker` possible). Only a member's click saves it. Server refuses: link already recorded, loop, same card already open | 7 offline tests; corpus `actions` 20/20 in run 3; live e2e `--ask` PASS | REAL-MODEL TESTED |
| F6 | "ok thanks!" offered as maybe got "You're welcome!" (2/2) after the prompt grew | Prompt not reliable for this | Deterministic: a maybe that is only an ack gets silence without a model call; a courtesy-only answer to a maybe is silence (`ACK_ONLY`, `COURTESY_ONLY`) | `test_a_maybe_that_is_only_a_thanks_...`; old ask corpus thanks 2/2 | REAL-MODEL TESTED |
| F7 | "Cadence ieri ha risposto male." / "dobbiamo migliorare Cadence?" / "...del coordinator." were sure calls to Cadence | Routing (`addressing.py`) | Two levels: sure call (`@cadence`, "Cadence,", greeting, "Cadence <question>?", "..., Cadence?") vs maybe (bare name without call punctuation: the model may stay silent); a message to the group ("secondo voi", "ragazzi", "what do you all think") never offered; full stop without comma at the end is no call | `test_a_clear_call_is_sure_...`; corpus routing 7/7 in run 3 | IMPLEMENTED/TESTED OFFLINE + REAL-MODEL TESTED |
| F8 | "Secondo voi dobbiamo migliorare Cadence?" as maybe still answered 2/3 after a prompt fix | Prompt alone unreliable | Moved to routing (F7, `TO_THE_GROUP`); prompt note kept | run `maybe` ×3 (console), then routing tests | IMPLEMENTED/TESTED OFFLINE |
| F9 | The ask answer stated a chat-only dependency as if planned (run 2 of the baseline e2e) | Prompt | ASK rule: plan facts vs things only said in chat; never present an inference as part of the plan | corpus `rasd: what does it depend on, before anything is recorded` 2/2 | REAL-MODEL TESTED |
| F11 | "Sì, fallo" typed under a proactive card never reached Cadence (the web's follow-up window opened only after an ask answer); had it reached, a second card could be drafted | Routing (client) + missing context | The web opens the follow-up window when a pending card arrives; the ask prompt lists the cards waiting for a click; the server refuses a second card on the same task and answers "C'è già una card qui sopra…: confermala con il suo pulsante" | `test_yes_under_a_card_points_to_that_card_...`; live e2e 2/2: "…è già pronta: confermala con il suo pulsante. Il collegamento non è ancora salvato nel piano." | REAL-MODEL TESTED |
| F12 | After a confirmed link, saying the same prerequisite again (other words) could add a second "data" task | Domain logic | `known_prerequisite` (same title, or same member delivering it, already linked) drops the CS7 draft (R7) and refuses the ask card; a prerequisite named exactly like a plan task links that task (`same_title_task`) | `test_saying_it_again_after_the_link_was_confirmed_...`, `test_a_prerequisite_named_like_a_plan_task_links_that_task` | IMPLEMENTED/TESTED OFFLINE |
| F13 | A passing mention offered as maybe was also sent on as `user_text`: the lab's `respond_when_addressed` could answer it a second time (spec 7.6) | Routing (client) | Like a sure call, a maybe-mention is not sent on as `user_text` | by construction (`hub.post_message`); not observed live | IMPLEMENTED (not separately tested) |
| F14 | A follow-up asked right after an answer got "The coordinator is already working on a request" | Concurrency (`plan_commands._ask`): the answer was sent before the request finished (memory write after the notice) | The notice is the last step; the busy notice follows the group's language | live e2e 3/3 after (the e2e now fails on a busy follow-up) | REAL-MODEL TESTED |
| F15 | Server sentences in English in Italian groups ("Dependency saved…", "From:"), model lists shown as raw "- " and "**" in the web chat | Response presentation | `wording.py`: click notices and the busy notice in Italian when the group chose Italian or the card/question is Italian; "Fonti:" line. Web: `AgentReply` reads paragraphs, "- " lists, **bold** and the sources line from plain text (never HTML). ASK prompt: answer first, lists one item per line, bold at most twice, no tables/links/HTML | `test_the_servers_own_sentences_follow_the_groups_language`, `agentText.test.tsx` (incl. `<script>`/`<img>` stay text); live e2e shows Italian confirms and bold | REAL-MODEL TESTED (server) / TESTED OFFLINE (web) |
| F16 | Test space 20/11 (Gabriele): "segna il prototipo al 50%" got "Non posso modificare l'avanzamento"; three tasks asked at once got "I can't save them directly" | Missing capability: the `action` had only `new_task` / `dependency` | Two more actions, checked in `ask_action.py`: `update_task` (owner, date, status, progress of one task) → a check-in card with one option; `plan` (2–8 new tasks) → the "Plan: who does what" card, tasks already in the plan left out. ASK: never "I can't" / "do it yourself", always a card to confirm. Card choice and refusals moved to `ask_cards.py` | `test_coordinator_ask_updates.py` (7 tests) | IMPLEMENTED/TESTED OFFLINE (live test by Gabriele on Cadence) |
| F17 | Test space: a second voice answered follow-ups in English without sources, once reprimanding the group's tone | Routing (client): a follow-up offered to Cadence as maybe was ALSO sent to the lab as `user_text`, so `respond_when_addressed` (and its `pleasant_conversation` rider) could reply | Like sure calls and maybe-mentions (F13), a follow-up goes to Cadence only; when Cadence stays silent the line is still kept in the chat's memory (`_ask` remembers it), so context and catch-up see the whole conversation | `test_a_follow_up_that_is_not_for_cadence_gets_silence` (line kept); project `test_web_hub` | IMPLEMENTED/TESTED OFFLINE |
| F19 | Mixed languages: an Italian group got "Stepping in because" on check-in cards; "From:" under Italian answers to short questions | Prompt (CHECKIN had no language clause) + `italian()` sampled only the question | CHECKIN: rationale and labels in the group's language; the sources line and the refusals sample question + answer | `test_the_sources_line_follows_the_answers_language` | IMPLEMENTED/TESTED OFFLINE |
| F10 | Screenshot link "Rivedere e correggere il RASD" ↔ "Create first mockup" | NOT VERIFIABLE: the group's database was not available here. Likely source in code: the dependency suggestion when tasks are added (`_dependency_cards`, CS7 "possible" card) confirmed by a click, or a link by hand | Such links now keep their origin (F4); old ones show "Origin of this link not recorded"; any member can change or remove a link (Dependencies → Change) | — | NOT VERIFIED |

Checker defects found and fixed in the corpus (not model failures): a
negative regex matching "non è ancora salvato"; Italian "analisi"/"test di
integrazione" not matched; "Matt" expected to be Matteo while it also fits
Matías (asking is the required behaviour); "no recorded" not accepted as a
refusal in `live_ask_corpus.py`. Each change is commented in the script.

## Prompt / version history

| Version | Change | Measured effect |
|---|---|---|
| P0 (`f92c5e2`) | — | e2e 1/2; ask corpus 24/24 |
| P1 | CS7 writer: `depends_on_owner_id`; "possible" for a stated prerequisite of work not stuck now. CS5 trigger: own small next steps excluded. ASK: provenance, plan vs chat, practical options without blame, `action` rules; MAYBE note covers passing mentions | corpus run 1: 45/51 (6 fails: 4 checker, 2 real F8) |
| P2 | MAYBE note: group questions and talk about the app → silence | run 2: 50/52 (1 checker, 1 thanks); maybe ×3: 16/18 (F8 persists) → routing fix |
| P3 | MAYBE note: a thanks is never answered + F6 deterministic guard + F7/F8 routing | run 3: **78/78**; old ask corpus 23/24 (one checker fix: the answer refused correctly); e2e 2/2 |
| P5 | ASK format rules (answer first, "- " lists, bold ≤ 2, no HTML) + Italian server sentences | run 5: regression 7/7, holdout 18/19 (checker defect, fixed), dev 25/26 (the model-only "secondo voi" case, filtered by routing in production); old ask corpus 12/12; e2e 3/3 after F14 |
| P4 | ASK: cards waiting for a click are listed; do not draft what a card already proposes (F11) | e2e with typed yes 2/2; corpus run 4: **52/52** (regression 7/7, dev 26/26, holdout 19/19) |
| P6 | ASK: `update_task` and `plan` actions, never "I can't". CHECKIN: group's language (F16, F17, F19; an off-topic rule was tried and left out, 20/11) | not measured here: live test on Cadence by the group |

## Results after (P3)

Corpus: 52 cases (7 regression, 26 dev, 19 holdout written before the prompt
iterations; 2 holdout expectations were corrected after run 1 because they
were wrong, never to fit an answer). Run 3: regression and holdout twice each.

| Split | Run 1 (P1) | Run 3 (P3) |
|---|---|---|
| Regression (RASD/Matías family) | 6/7 | 14/14 |
| Dev | 23/26 | 26/26 |
| Holdout | 16/18 | 38/38 |

| Metric (run 3, n = runs) | Result | Target |
|---|---|---|
| Sure-call routing precision/recall (deterministic, 15 phrasings in tests) | 15/15 | ≥ 95% |
| Maybe decisions: silence when not for Cadence / answer when it is (model) | 7/7 in run 3, but "Secondo voi… Cadence?" was 1/3 under P2: unstable at the model level, now filtered by routing before the model | ≥ 90% |
| Action drafted with correct kind, ids, direction, owner | 20/20 | ≥ 90% |
| Dependency inference on cards (CS7 drafts) | 7/7 (holdout 4/4) | ≥ 90% |
| Grounded knowledge / documents / decisions answers | 24/24 | ≥ 90% |
| Entity resolution (Mathias→Matías, Giuli→Giulia, Matt→asks, Federico→not invented) | 5/5 | — |
| Unauthorized state changes | 0 (every change needs a click; tested offline) | 0 |
| Accepted cycles / self links | 0 (server refuses; tested offline) | 0 |
| Duplicate cards on a repeated request | 0 (tested offline) | 0 |
| Live e2e RASD/Matías, CS7 path with confirm | 8/8 after (1/2 before); the last 5 with "Sì, fallo." typed under the card | all critical pass |
| Live e2e RASD/Matías, direct request path | 3/3 | — |

Sample sizes are small (1–2 runs per case, 52 cases): these numbers say the
known failures are gone on these cases, not that Cadence is reliable in
general. The judge (Realtime) was exercised only in the 4 e2e runs.

## Not verified / limits

- Deployed app (Render/Vercel): not touched. The Project's `vendor/muca-server`
  is re-synced to this work with `deploy/render/update-vendor.sh` (local commits
  on the `claude/cadence-conversational-optimization-*` branches, not pushed):
  it reaches Render only when that branch is merged into `gitlab-dev`. From the
  vendor copy: server 745 passed, client 362 passed, Playwright 16/16.
- Web UI: Vitest 149/149 + `tsc`; Playwright 16/16 on the offline fake stack (SCRIPTED model: proves the UI path only) with this worktree's server code. Not checked by hand in a browser.
- Removing a link, "la API è pronta" (status) and "generate the whole graph"
  are not actions of `ask`: a member does them on the Dependencies/Tasks pages
  (Change, status). Batch graph generation is not built.
- CS3 and CS6 were not changed; CS6 cases only check that answers do not read
  silence as delay.
- Discord and Slack have no routing by name: Cadence is asked there with the
  `/ask` command, and free mentions go to the lab's agent (unchanged on
  purpose: it would change the shared Discord runtime).
- Server sentences now follow the group's language (F15); the web UI's own
  labels (buttons, card headings such as "Delivered by") are still English,
  as the whole web UI is.
- The R2 preemption was never observed live (CS5 stopped firing on the casual
  lines); it is proven offline only.

## How to rerun

```
python scripts/live_conversation_corpus.py <path to .env> [dev|regression|holdout|all] [name part] [repeats]
python scripts/live_rasd_e2e.py ws://localhost:<port>/ --confirm [--ask]
python scripts/live_ask_corpus.py <path to .env> [repeats]
```

Stopping rule used: at most 3 prompt iterations; stop when regression and
holdout pass twice and no old corpus case regresses.

## Awareness and reliability (20 Nov 2026, evening: Matteo's screenshots)

Root causes found in code, each with a regression test. Fake model only:
these prove routing, prompt contents and delivery, **not** how a real model
answers. Real-model runs: NOT RUN (need Matteo's OK for tokens).

| # | Screenshot failure | Root cause (layer) | Fix | Test |
|---|---|---|---|---|
| A | "Controlla le mie task" → "which Matteo Cesandri, Software Engineer or Design?" | prompt: the asker was never named; task owners by name only, and two member records share the name (why the space has two: NON VERIFICATO) | `<asker>` block built by the server from the member id: name, their tasks by owner id, a same-name record told apart; owners as "Name (id N)" | `test_coordinator_awareness.py` A tests |
| B | after "Frontend Application", "sono al 30%" → "which task?" | no conversational state: the task in focus lived only in the chat text | per-member focus (task id, 30 min, this session only) from the answer's action or its one named task; given to the model | `test_coordinator_awareness.py` B tests |
| C | "Oii Cadence, sono al 30%..." never answered | routing (client): "Oii" was not a greeting, and **a raw 0x08 byte** in `addressing.py` (a mangled `\b`) disabled every "greeting + Cadence" sure call; the message went to the lab's agent | stretched greetings; `\b` restored; the name anywhere is offered as "maybe" (the model stays silent on talk ABOUT Cadence) | `prototype/tests/test_addressing.py` |
| C2 | a second message while the first was answered → "already working" / nothing | server refused concurrent asks (BUSY) | asks wait their turn (FIFO lock), every one answered | `test_two_messages_sent_at_once_are_both_answered_in_order` |
| C3 | answer after 60 s dropped | web/Discord/Slack forgot the request id at the timeout | late answers posted when they come; web gives up visibly after 180 s more | `prototype/tests/test_web.py`, `test_cadence_chat_channels.py` |
| D | progress not applied | (not a separate bug: never reached the ask) | existing update_task → card → click path | `test_coordinator_ask_updates.py` |
| F | `**30%**` on the card | the card's reason (= Cadence's answer) rendered as plain text | same safe inline parser as the chat; no "Fonti:" line under a card | `webapp/src/console/Awareness.test.tsx` |

Also: "Cadence is thinking…" (web banner after 0.7 s, Discord typing, Slack
placeholder line replaced by the answer); Discord and Slack now route
messages for Cadence to the coordinator like the web app (one rule,
`addressing.for_cadence`); a message to Cadence is still mirrored to the
group's linked channels (`args.said`).

After review: Discord says "unavailable" when the server fails (it said the
answer would come); Discord and Slack give up visibly on a late answer after
180 s, like the web; the web banner is reset by the server on reconnect
(`hello.thinking`); the web answers in the background, one group's answers in
order, so a message typed under the banner is shown at once.

A "maybe" Cadence stays silent on now goes on as ordinary chat (the
conversation logs it, the proactive judge reads it). Before, every message for
180 s after an answer or a card skipped CS3/CS5/CS7: the e2e specs
`plan.spec` (CS3 "Think-aloud or SUS?") and `opener.spec` failed on HEAD
for this reason; both pass now.

Proof on this branch: pytest server 764, client 379; Vitest 167; Playwright
e2e 16/16 (HEAD: 3 failing, same stack); axe 0 violations on 5 screens
(`e2e/axe.mjs` opened the landing for the sign-up form, now on /login).

Known limits (decisions for Matteo):
- Discord/Slack: an @mention of the bot or a sure call goes to the
  coordinator only, so it no longer reaches the lab agent (its web search
  included), and a message Cadence answers is not judged by CS5/CS7 too.
- Silence on talk ABOUT Cadence rests on the prompt (MAYBE_NOTE): NOT VERIFIED
  with a real model. Earlier corpora showed the prompt alone did not always
  hold such rules.

NOT IMPLEMENTED (fit the existing action → card → click pattern): milestone
reschedule by chat, dependency removal by chat, cancelling a pending card by
chat ("annulla la proposta"), reverting the last confirmed change ("annulla
l'ultima modifica"). Need a data decision: soft delete / restore of tasks. Pre-existing flaky test:
`test_a_blocker_replaces_an_owner_card_still_being_drafted` (sleep 0.05 s)
fails only under load; 12/12 sequential on HEAD and on this branch.
