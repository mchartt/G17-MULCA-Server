# Multi-User Conversational Agent Server

```text
muca_server/
├───audio/
│   └───router.py
├───events/
│   └───dispatcher.py: Recognizes the incoming user message type and forwards it to the correct server component.
├───protocol/
│   │───incoming.py: Class definition of incoming messages.
│   └───outgoing.py: Class definition of outgoing messages.
└───sessions/
    │───session_runtime.py: Receives and sends message to user, starts all parallel workers.
    │───session_runtime_factory.py: Factory class which builds all the components used by session_runtime.
    └───session_state.py: State class which identifies the session.
```
