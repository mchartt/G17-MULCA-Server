# INDICE — `tests/coordinator/`

Tests of the Group Coordinator. No network and no real model: the model is a
`FakeChatClient`. Run with `python -m pytest tests/coordinator -q`.

Every file name starts with `test_coordinator_` so it cannot clash with the
lab's test files of the same name (pytest needs unique names).

| File | What it proves |
|---|---|
| `__init__.py` | Makes the folder a package, so tests can import the shared fakes. |
| `conftest.py` | Shared fakes: `GOOD_CHARTER` (a valid model answer), `FakeChatClient` (answers in order, or raises), `FakeConversation` (id, DM flag, roster), fixture `store` (a fresh SQLite file), `drain(queue)`. |
| `test_coordinator_store.py` | Empty state; profiles saved and updated; proposals round-trip; **resolve applies once and only while pending**; **three simultaneous clicks apply exactly once**; a failing change rolls back and leaves the proposal pending; charter replaced; tasks, dependencies, decisions read back; **our tables share the lab's SQLite file without disturbing it** (assumption A4). |
| `test_coordinator_proposals.py` | Every life-cycle step: create changes no state; confirm applies charter and milestones; reject changes nothing; non-member refused; missing refused; second click refused; expired closed as expired; unknown action refused; **a payload that became invalid is not applied**; two confirms at once apply once. |
| `test_coordinator_writer.py` | Same call shape as the lab's summaries (strict JSON schema); the assignment goes in as quoted data; cost recorded; invalid answer retried once; two failures give None without raising; empty goal refused. |
| `test_coordinator_client_config.py` | Without Azure variables the client is plain OpenAI as before; one variable alone does not switch; both variables point REST and Realtime at the Azure resource; endpoint must be https. |
| `test_coordinator_router.py` | Forwards everything else to the Conversation; kickoff → card → confirm saves the charter; non-member click refused; kickoff without text; model down → "not available"; profile saved, bad profile explained, non-member refused; state with request id; DMs and ephemeral sessions refused; unexpected error never breaks the chat. |
| `test_coordinator_protocol.py` | Exact JSON of the five events; defaults; invalid messages refused; existing messages still parse. |
| `test_coordinator_switch.py` | **Switch off by default**; off builds no coordinator; off the router is the plain `Conversation`; off the new events are ignored without error; **on, kickoff and confirm work over a real WebSocket** and are saved in the conversation's SQLite file. |
