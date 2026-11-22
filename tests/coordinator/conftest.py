"""
Shared fakes for the Group Coordinator tests. No network, no real model.

FakeChatClient  - stands in for AsyncOpenAI: `client.chat.completions.create`
                  returns the scripted answers in order (a str is the JSON
                  content, an Exception is raised).
FakeConversation - the few things CoordinatorRouter reads from a Conversation.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from muca_server.cognition.coordination.store import CoordinationStore

GOOD_CHARTER = {
    "goal": "Build a group coordinator web app for peer student teams.",
    "deliverables": ["Report", "Prototype", "Pitch video"],
    "milestones": [
        {"title": "Demo Madness", "due_date": "2026-12-02"},
        {"title": "Final tutoring", "due_date": None},
    ],
    "rationale": "Stepping in because you pasted the assignment.",
}


class FakeChatClient:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=50,
                prompt_tokens_details=SimpleNamespace(cached_tokens=10),
            ),
        )


@dataclass
class FakeConversation:
    conversation_id: str = "web:G42"  # a web space: the only place a project is created (22/11)
    is_private: bool = False
    is_ephemeral: bool = False
    roster: dict[int, str] = field(default_factory=lambda: {1: "Ada", 2: "Bruno", 3: "Chiara"})
    # Something only the Conversation has, to check the wrapper forwards it.
    listening_mode: str = "ambient"

    def participants(self) -> dict[int, str]:
        return dict(self.roster)

    async def route_user_text(self, message) -> None:
        self.texts = [*getattr(self, "texts", []), message.text]


@pytest.fixture(autouse=True)
def _no_welcome_delay(monkeypatch):
    """The welcome question to a channel newcomer waits a few seconds in real use; not in tests."""
    from muca_server.cognition.coordination import router

    monkeypatch.setattr(router, "WELCOME_DELAY_SECONDS", 0.0)


@pytest.fixture
def store(tmp_path) -> CoordinationStore:
    return CoordinationStore(tmp_path / "group.db")


async def drain(queue: asyncio.Queue) -> list:
    """Everything put on the queue so far."""
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items
