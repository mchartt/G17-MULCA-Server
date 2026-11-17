# DEMO_SCRIPT — Group Coordinator ("Cadence"), Demo Madness 2 Dec 2026

Exact steps and messages for each scenario. Live run = real Azure models; the
intervention agent decides by itself whether a strategy fires, so the
messages below are written to make the situation obvious. If a card does not
appear, see "If something does not fire" at the end. Rehearsal without cost:
the offline stack (`mulca/prototype/webapp/e2e/fake_stack.py`), where a message
with "font" fires CS2 and one with "think-aloud" fires CS3 (scripted).

Status: **NOT VERIFIED (live)** until it has been run twice in a row with
Azure without manual fixes (PROGRESS.md, M5). Note date and cost here.

## 0. Before the demo (10 minutes)

1. Azure ready (AZURE_SETUP.md) and `scripts/azure_probe.py` all OK.
2. `cd mulca/prototype/webapp && npm ci && npm run build` (once).
3. From the folder with both clones: `bash mulca/prototype/webapp/scripts/demo-up.sh`.
4. Two browser windows side by side (normal + private): **Ada** and **Bruno**.
   Each one creates an account first (**Create account**: email, password,
   display name); the profile is filled once and follows the person into
   every space.
5. **Three spaces**, one per proactive scenario. The rules allow at most one
   unsolicited card every 10 messages and 5 minutes per group (NFR3), so each
   proactive scenario runs in its own space. Create them in Ada's window, join
   each with Bruno (invite code), and fill the profiles (Edit my profile):
   - Ada: Computer Engineering · Python, FastAPI, testing · 8 h
   - Bruno: Interaction Design · Figma, user research, writing · 6 h
6. Discord (optional): a test server with the bot in a channel
   (`mulca/prototype/TRYING_ON_DISCORD.md`); Slack (optional):
   `mulca/prototype/SLACK_SETUP.md`. Step 10 links them to space "Demo A".

## 1. Kickoff — UC2 (space "Demo A")

Ada: **Paste the assignment** →
`Build a web app that coordinates student project groups. Deliverables: report with UNG model and scenarios, presentation, working prototype with source code, 2-minute pitch video. Demo Madness on 2 December 2026, final tutoring on 14 December 2026.`
→ charter card (goal, deliverables, milestones 2 Dec and 14 Dec). Show
"nothing changes before a click": the panel still says "No goal yet".
Bruno: **Confirm** → both panels show the goal; Ada's card turns "Confirmed".

## 2. Planning — UC4 (space "Demo A")

Ada: **Plan tasks** →
```
Server API with FastAPI
UI screens in Figma
User test with 5 people
Report chapter on scenarios
```
→ plan card with a suggested owner and a reason per task (from the profiles).
Bruno: **Confirm** → the four tasks are in the panel.
Ada sets "Server API with FastAPI" to **doing** with the status menu.
Then **Add to my calendar** under Milestones → copy the link (show it in
Google Calendar: Other calendars → From URL, if there is time).

## 3. Scenario A — CS5 owner and date (space "Demo A", after ≥ 10 messages)

Chat (alternate Ada/Bruno), the last two set it up:
- Ada: `ok the plan looks good`
- Bruno: `I'll start the Figma screens tonight`
- Ada: `nice, I keep going with the API`
- Bruno: `what about the ethics form for the user test?`
- Ada: `right, the professor said we need it signed before testing`
- Bruno: `yes, someone has to prepare the consent form for the user test`
- Ada: `agreed, it must be ready before the test`
- Bruno: `ok`
- Ada: `let's see the API docs then`
- Bruno: `sure`

→ card "Owner and date": task "Prepare the consent form…", suggested owner,
date. Press **Why?**: the strategy and the two messages behind it.
Bruno: **I'll take it** → task in the panel with Bruno as owner.

## 4. CS2 — delegate a simple task (space "Demo B")

- Ada: `Arial or Helvetica for the report?`
- Bruno: `Helvetica, and margins at 2.5 cm`
- Ada: `no, 2 cm, the template says so`
- Bruno: `and the font size of captions?`
- Ada: `10 pt maybe, or 9`
- Bruno: `let's check the template again`

→ card "One person can do this" (never names an owner), "Then back to" the
open complex topic. **Why?** shows the messages. Ada: **I'll take it**.

## 5. Scenario "Not now" (space "Demo B", later) — G5

If CS2 fires again on a topic you are discussing on purpose, press
**Not now**: the card says it is silenced on that topic for 30 minutes.

## 6. CS3 — close a decision (space "Demo C")

- Ada: `for the user test: think-aloud or the SUS questionnaire?`
- Bruno: `think-aloud shows why people get stuck`
- Ada: `SUS is faster and gives a number we can compare`
- Bruno: `but a number alone doesn't tell us what to fix`
- Ada: `true, but think-aloud takes much longer per person`
- Bruno: `we keep going round in circles on this`

→ card "Close the decision" with one button per option and what was said for
each. Bruno: **Think-aloud** → the decision log in the panel shows it with
its reason.

## 7. Scenario C — "why did we decide X?" — UC9 (space "Demo C")

Panel → **Ask why the group decided something**: `Why did we choose think-aloud?`
→ answer from the decision log, with the source line (who, when, which card).

## 8. Scenario B — CS7 blocker (space "Demo A", ≥ 10 messages after step 3)

- Bruno: `I can't finish the UI screens until the API endpoints are ready`
- Ada: `the API needs two more days`
- Bruno: `but the user test is planned before that`
→ card "Something is blocked": task, question, who could help. **Mark as blocked**.

## 9. Check-in — UC6 (space "Demo A")

**Check-in** → card with what is late or at risk (computed by the server: the
blocked UI task, the milestone if it is within 48 h) and up to three
options. Choose one → the plan changes, the card says Confirmed.

## 10. Cross-channel: one project on web, Discord and Slack (space "Demo A")

Since 16/11 a Discord or Slack channel can be linked to a web space
(DECISIONS.md, 16/11). All channels must talk to the same MUCA-Server.

1. Web, Ada: copy the line in the sidebar block **Discord and Slack**
   (`/coordinator link <invite code>`).
2. Discord test channel: paste it → "Linked to the web space: plan, tasks, decisions and cards
   are now shared."
   Slack (if the app is installed, `mulca/prototype/SLACK_SETUP.md`): the same.
3. Discord: `/coordinator plan` → the goal, milestones and tasks made on the
   web in steps 1 and 2. **Say it: same project, three places.**
4. Discord, Carla (a third person): `I can take the user test recruiting`
   → the message shows in the web chat with a "Discord" tag.
5. Web, Bruno: **Check-in** (or any card) → the card also appears on Discord;
   click **Confirm** on Discord → the web card turns "Confirmed" for everyone
   (first valid click wins).
6. Web, Ada: **Catch up** → the decisions since her last visit and what was
   written on Discord and Slack, read from disk with no model call.

Each channel keeps its own chat and its own agent memory; what is shared is
the project (plan, decisions, cards). **NOT VERIFIED (live)**: tested offline
with a fake Discord and a fake Slack against a real local server.

## 11. Commands typed in the chat

- Discord and Slack: `/coordinator kickoff | planning | checkin | task | why |
  plan | profile | link | unlink | catchup`.
- Web chat: type `/` to see the list (`/kickoff`, `/planning`, `/checkin`,
  `/catchup`, `/task 2 done`, `/why …`, `/plan`); a `/` line is never posted
  to the group. `@coordinator` with a question asks the coordinator directly.

## Plan B: the demo without Azure, Render, Discord or Slack

If the network, Azure or Render fails, run everything on one laptop:

```sh
cd mulca/prototype/webapp && npm ci && npm run build && cd ..
python webapp/e2e/fake_stack.py        # http://127.0.0.1:8090
```

What is real: accounts, spaces, the chat, cards, clicks, the plan, the
decision log, Why?, Not now, check-in, calendar feed, catch-up. What is
scripted: the model's drafts (fixed texts) and the two proactive triggers:
a message with **font** fires CS2, one with **think-aloud** fires CS3
(steps 4 and 6 work as written). CS5 and CS7 do not fire offline. Discord and
Slack are not part of it. **Say on stage that the model is scripted.**

## If something does not fire

- A proactive card needs ≥ 10 messages and ≥ 5 minutes since the last card in
  that space, never right after the agent's message, and not a topic silenced
  with Not now: use a fresh space.
- The intervention agent judges the conversation: make the situation explicit
  (two options named, "someone has to…", "I can't … until …").
- "The coordinator is not available right now": check the MUCA-Server window
  for the error line and the Azure quota (AZURE_SETUP.md).

## Live runs

| Date | Scenario | Result | Cost (USD) | Notes |
|---|---|---|---|---|
| — | — | not run yet | — | waiting for Azure |

## Live runs

### 15 Nov 2026, web, local stack, Azure (gpt-6.1-sol / gpt-realtime-2.1-mini)

| Scenario | Result |
|---|---|
| Profiles (2 members) | saved |
| Kickoff → charter card → Confirm | goal + milestones Demo Madness 2 Dec, tutoring 14 and 16 Dec, rationale "Stepping in because…"; confirmed by the other member |
| /planning (3 tasks) | owners chosen from the profiles with a reason each (backend → Ada, Figma and user test → Bruno); confirmed |
| /checkin | on track, no model call, owners' report |
| "@coordinator why…" typed in the chat | answered from the (empty) decision log: "No decision has been recorded yet." |
| CS3 proactive card (6 chat messages, think-aloud vs SUS) | decision card with question, 2 options, Why? evidence; no reply from the lab's agent |

Observed cost: about $0.01 for the whole run (card drafts $0.0027–0.0039 each,
intervention agent ~$0.0005 per message). Observation for tuning: the CS3 card
came after the 2nd message of the debate, so its option arguments rest on two
messages only; consider a stricter `trigger_description` if it fires too early
in the demo.
Not run live yet: CS5, CS2, CS7, Not now, Helpful, Delete space, Discord.
