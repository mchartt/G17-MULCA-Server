"""
The text path end to end, with agents that reach no provider.

This is the shape the Discord adapter talks to: a user utterance goes in, the
cognition policy decides to answer, and the reply streams back out.
"""

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient

from muca_server import create_app
from muca_server.agents import (
    Conversation,
    MainAgent,
    MainAgentOptions,
)
from muca_server.cognition import Utterance
from muca_server.sessions import (
    DefaultSessionComponentsFactory,
    SessionState,
)


class ScriptedMainAgent(MainAgent):
    """Answers every instruction with the same two deltas."""

    def __init__(self, *, options: MainAgentOptions) -> None:
        super().__init__(options=options)
        self.instructions: list[str] = []

    async def _trigger_response(
        self,
        *,
        max_tokens,
        instructions,
        add_to_conversation,
        **_,
    ) -> None:
        self.instructions.append(instructions)

        await self._stream_text("Hello ")
        await self._stream_text("there.")
        await self._stream_text("", is_final=True)


class ScriptedAgentFactory:
    def __init__(self) -> None:
        self.main: ScriptedMainAgent | None = None

    def build_main(self, options):
        self.main = ScriptedMainAgent(options=options)
        return self.main

    def build_auxiliary(self, options):
        from muca_server.agents import AuxiliaryAgent

        return AuxiliaryAgent(options=options)


class AlwaysAnswer:
    """Cognition that answers every utterance without consulting observers."""

    def __init__(self) -> None:
        self.utterances: list[Utterance] = []
        self.replies: list[str] = []
        self._port = None

    async def start(self, port) -> None:
        self._port = port

    async def close(self) -> None:
        pass

    async def on_utterance(self, utterance: Utterance) -> bool:
        self.utterances.append(utterance)
        await self._port.instruct_main_agent(
            f"Answer {utterance.label()} directly.",
        )
        return False

    async def on_intervention_signal(self, agent_guid, signal) -> None:
        pass

    async def on_assistant_output(self, text, *, interrupted) -> None:
        self.replies.append(text)

    async def wait_for_turn_to_finish(self) -> None:
        pass

    def observer_catchup_text(self) -> str:
        return ""


def make_conversation():
    state = SessionState(session_id=uuid4())
    state.is_connected = True
    outbound: asyncio.Queue = asyncio.Queue()
    policy = AlwaysAnswer()
    factory = ScriptedAgentFactory()

    conversation = Conversation(
        state=state,
        outbound_queue=outbound,
        main_agent_options=MainAgentOptions(),
        agent_factory=factory,
        policy=policy,
    )

    return conversation, policy, factory, outbound


def user_text(speaker: int, text: str, name: str = ""):
    from muca_server.protocol import parse_incoming_message

    return parse_incoming_message(
        {
            "event": "user_text",
            "speaker": speaker,
            "speakerName": name,
            "text": text,
        }
    )


async def drain(outbound: asyncio.Queue) -> list:
    messages = []
    while not outbound.empty():
        messages.append(outbound.get_nowait())

    return messages


# ----------------------------------------------------------------------
# Conversation level
# ----------------------------------------------------------------------


async def test_a_text_utterance_is_a_whole_turn():
    conversation, _, _, _ = make_conversation()
    await conversation.start()

    await conversation.route_user_text(
        user_text(1, "morning", name="Ada"),
    )

    await conversation.wait_until_idle()

    assert len(conversation.turns) == 1
    assert conversation.turns[0].speaker_name == "Ada"
    assert not conversation.turns[0].is_open
    assert conversation.current_speaker == -1
    assert conversation.last_speaker == 1


async def test_an_empty_text_turn_is_ignored():
    conversation, _, factory, _ = make_conversation()
    await conversation.start()

    sent: list[str] = []

    async def record(text: str, *, role: str) -> None:
        sent.append(text)

    factory.main._send_text = record

    await conversation.route_user_text(user_text(1, "  \n", name="Ada"))
    await conversation.wait_until_idle()

    assert conversation.turns == []
    assert sent == []


async def test_the_utterance_reaches_the_main_agent_labelled():
    conversation, _, factory, _ = make_conversation()
    await conversation.start()

    sent: list[tuple[str, str]] = []

    async def record(text: str, *, role: str) -> None:
        sent.append((text, role))

    factory.main._send_text = record

    await conversation.route_user_text(
        user_text(1, "morning", name="Ada"),
    )

    await conversation.wait_until_idle()

    assert sent == [("Ada: morning", "user")]


async def test_the_policy_sees_the_utterance_and_answers():
    conversation, policy, factory, outbound = make_conversation()
    await conversation.start()

    await conversation.route_user_text(
        user_text(7, "hello?", name="Grace"),
    )

    await conversation.wait_until_idle()

    assert [u.content for u in policy.utterances] == ["hello?"]
    assert factory.main.instructions == [
        "Answer Grace directly.",
    ]

    texts = [
        message.text
        for message in await drain(outbound)
        if message.event == "assistant_text"
    ]
    assert "".join(texts) == "Hello there."


async def test_a_completed_reply_is_reported_to_the_policy():
    conversation, policy, _, _ = make_conversation()
    await conversation.start()

    await conversation.route_user_text(user_text(1, "hi"))

    await conversation.wait_until_idle()

    assert policy.replies == ["Hello there."]


async def test_participants_name_people_who_have_not_spoken():
    from muca_server.protocol import parse_incoming_message

    conversation, policy, _, _ = make_conversation()
    await conversation.start()

    await conversation.route_participants(
        parse_incoming_message(
            {
                "event": "participants",
                "participants": [{"id": 9, "name": "Alan"}],
            }
        )
    )

    await conversation.route_user_text(user_text(9, "hi"))

    await conversation.wait_until_idle()

    assert policy.utterances[0].speaker_name == "Alan"


# ----------------------------------------------------------------------
# Over a real WebSocket
# ----------------------------------------------------------------------


def build_app():
    return create_app(
        components_factory=DefaultSessionComponentsFactory(
            agent_factory=ScriptedAgentFactory(),
            policy_factory=lambda context: AlwaysAnswer(),
        )
    )


def test_a_user_message_is_answered_over_the_socket():
    client = TestClient(build_app())

    with client.websocket_connect("/") as websocket:
        websocket.send_json(
            {
                "event": "connect",
                "conversationId": "channel-7",
                "isDm": False,
            }
        )
        websocket.send_json(
            {
                "event": "user_text",
                "speaker": 1,
                "speakerName": "Ada",
                "text": "are you there?",
            }
        )

        frames = []
        while True:
            message = websocket.receive_json()
            if message["event"] != "assistant_text":
                continue

            frames.append(message)
            if message["isFinal"]:
                break

        websocket.send_json({"event": "disconnect"})
        assert websocket.receive()["type"] == "websocket.close"

    assert "".join(f["text"] for f in frames) == "Hello there."


# ----------------------------------------------------------------------
# Attachments
# ----------------------------------------------------------------------


def test_attachments_are_given_in_full_or_as_a_note():
    from muca_server.agents.conversation import _with_attachments
    from muca_server.protocol import Attachment

    attachments = [
        Attachment(name="message.txt", text="PRIMARY USERS children", chars=22),
        Attachment(name="long.pdf", text="abc", chars=10),
        Attachment(name="photo.png", readable=False),
    ]

    full = _with_attachments("review this", attachments, full=True)
    assert full.startswith("review this")
    assert "[Attached message.txt, 22 characters:]\nPRIMARY USERS children" in full
    assert "[Attached long.pdf -- only the first 3 of 10 characters were received" in full
    assert "say that you only read the first 3 characters" in full
    assert full.endswith("you didn't get:]\nabc\n\n[Attached photo.png: could not be read as text]")
    assert "[Attached photo.png: could not be read as text]" in full

    note = _with_attachments("", attachments[:1], full=False)
    assert note == (
        '[Attached message.txt, 22 characters; MULCA reads it. '
        'First words: "PRIMARY USERS children..."]'
    )
    cut_note = _with_attachments("", attachments[1:2], full=False)
    assert "MULCA reads it (the first 3)." in cut_note


async def test_mulca_and_the_log_get_an_attachment_the_observers_a_note():
    from muca_server.protocol import parse_incoming_message

    conversation, policy, factory, _ = make_conversation()
    await conversation.start()
    to_main, to_observer = [], []

    async def main_text(text, *, role="system"):
        to_main.append(text)

    class Observer:
        listen_to_assistant = False

        def listens_to_user(self, user_id):
            return True

        async def send_text(self, text, *, role="system"):
            to_observer.append(text)

    factory.main.send_text = main_text
    conversation._auxiliary_agents[uuid4()] = Observer()

    await conversation.route_user_text(
        parse_incoming_message(
            {
                "event": "user_text",
                "speaker": 1,
                "speakerName": "Delia",
                "text": "",
                "attachments": [
                    {"name": "message.txt", "text": "PRIMARY USERS " + "x" * 500, "chars": 514}
                ],
            }
        )
    )
    await conversation.wait_until_idle()

    assert "x" * 500 in to_main[0]
    assert "x" * 500 not in to_observer[0]
    assert to_observer[0].startswith("Delia: [Attached message.txt, 514 characters; MULCA reads it.")
    assert 'First words: "PRIMARY USERS' in to_observer[0]
    assert "x" * 500 in policy.utterances[0].content
