# CS corpus — realistic group chats for the live check (G3)

Each case: the saved state (Project memory), the chat, and what Cadence must
do. Run them on a fresh space with the real model; write the result in
`DEMO_EVIDENCE.md` (date, commit, model, card or silence, cost).
Pass rule proposed (to approve before measuring): every **positive** gets the
expected card, at most one **negative** gets any card, ambiguous cases are
reported, not scored. Names follow the FigJam scenarios (Marco, Sara, Giulia).

## CS7 · blockers and dependencies

Common state unless said otherwise (today Thursday 19 Nov):
- "Usability test" · Sara · due Fri 20 Nov · todo
- "Booking API" · Marco · due Sat 21 Nov · doing
- "Icons" · Giulia · due Mon 23 Nov · doing
- Milestone "Demo" Wed 2 Dec

### Positive (a card is expected)

P1 · stated blocker → `certainty: blocked`
> Sara: I can't prepare the test tasks until the booking flow works
> Marco: yeah it still crashes on submit
> Giulia: the new icons are in figma btw

P2 · possible dependency, plan + chat → `possible`, waits on "Booking API",
helper Marco, ways forward like "test with mock data"
> Sara: booked five testers for tomorrow at 10!
> Giulia: great
> Marco: API is halfway, should be done over the weekend

P3 · possible dependency on work not in the plan → `possible`,
`depends_on_title` ≈ "consent form"
> Sara: tests are friday morning
> Giulia: do we have the consent form? the ethics thing said we need one
> Sara: no idea who's doing it
(CS5 may compete here: either card is acceptable, not both.)

P4 · late task, owner says not done, milestone close → `blocked`
State: "Booking API" due Tue 17 Nov (late).
> Marco: API not done yet, auth is a mess
> Sara: test is tomorrow though

### Negative (silence is expected)

N1 · dependency already recorded and both dates fit
State: "Usability test" due Wed 25 Nov, waits on "Booking API" due Sat 21 Nov.
> Sara: testers booked for wednesday
> Marco: API on track for saturday

N2 · solved in the chat already
> Sara: can't test until the API works
> Marco: pushed a mock endpoint, use /mock/bookings
> Sara: perfect, works

N3 · silence or slowness alone, no date at risk
> Giulia: haven't heard from Marco in two days
> Sara: he said he's busy with another exam

N4 · personal reasons
> Marco: sorry I'm sick, API will be late
(Cadence never asks about health; a dependency card that names the reason is a failure.)

### Ambiguous (report what happens)

A1 · related-sounding tasks, nothing says one needs the other
> Sara: writing the test script today
> Marco: refactoring the API models

A2 · date unclear ("next week")
> Sara: I'd like to test next week
> Marco: API should be done soonish

## K3 · a decision reopened (`cs3_reopened_decision`)

Common state: decision "App navigation → Tabs, because five sections fit and
testers missed the side menu" (decided 12 Nov).

### Positive
R1 · proposed again, reason forgotten → card: recalls Tabs + reason, `new_point` ≈ "looks cleaner"
> Marco: what if we switch to a side menu? looks cleaner
> Sara: hmm why did we choose tabs?
> Giulia: don't remember tbh

R2 · new information → card with a real `new_point`
> Marco: the client now wants 8 sections, tabs won't fit
> Giulia: so side menu?

### Negative
N1 · decision applied, not reopened
> Giulia: we said tabs, so I'm drawing the tab bar today

N2 · reminded and closed by a member
> Marco: side menu?
> Sara: we chose tabs because testers missed the menu, let's keep it
> Marco: ok fair

N3 · same words, different question
> Sara: browser tabs keep crashing on my laptop

### Ambiguous
A1 · a half-joke
> Marco: honestly side menus are the best thing ever invented lol
