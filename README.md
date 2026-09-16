# Multi-User Conversational Agent Server

```text
muca_server/
├───agents/
│   │───conversation.py: Conversation — owns the agents, the turns, and every client message.
│   │───realtime_agent.py: Base agent; talks to no provider until a subclass overrides its hooks.
│   │───main_agent.py: The only agent that speaks to the users, so the only one that streams audio out.
│   │───auxiliary_agent.py: Agents that observe the conversation, with their own audio filters.
│   │───turn.py: One stretch of speaking in the conversation history.
│   │───instructions.py: Markers the agents read conversation events by.
│   └───options.py: Typed agent configuration.
├───audio/
│   └───chunk.py: One audio frame as it travels on the WebSocket.
├───events/
│   └───dispatcher.py: Recognizes the incoming user message type and forwards it to the correct server component.
├───protocol/
│   │───incoming.py: Class definition of incoming messages.
│   └───outgoing.py: Class definition of outgoing messages.
└───sessions/
    │───session_runtime.py: Receives and sends message to user, starts all parallel workers.
    │───session_runtime_factory.py: Factory class which builds all the components used by session_runtime.
    │───session_components_factory.py: Builds the per-session components and wires them together.
    └───session_state.py: State class which identifies the session.
```

The WebSocket contract is the one the Unity client documents in
`Orchestration/Remote/OrchestrationServiceRemoteMessageReference.cs`.

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[server,dev]"
.venv/Scripts/python -m pytest
.venv/Scripts/python -m uvicorn muca_server:app
```
