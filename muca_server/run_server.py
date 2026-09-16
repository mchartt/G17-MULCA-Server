"""
Boots MUCA-Server for real, against real OpenAI agents.

The composition root: the one place allowed to pick a task, a persona, and
which strategies are active, mirroring MULCA's old run_discord_bot.py. Every
other module stays generic; nothing outside this file knows what a "general
moderation" task is.

Run with: python -m muca_server.run_server
"""

from __future__ import annotations

import os

import uvicorn
from dotenv import load_dotenv
from openai import AsyncOpenAI

from .agents import MainAgentOptions
from .agents.openai import OpenAIAgentFactory
from .cognition import CoalaPolicy
from .cognition.loader import load_persona, load_strategy_store, load_task
from .cognition.memory import InMemoryEpisodicStore, InMemorySnapshotStore
from .main import create_app
from .sessions.session_components_factory import DefaultSessionComponentsFactory

TASK_ID = "general_moderation"
ACTIVE_STRATEGY_IDS = [
    "participation_equality",
    "respond_when_addressed",
    "non_offensive",
    "provide_summary",
]


def build_app():
    load_dotenv()

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    strategy_store = load_strategy_store(ACTIVE_STRATEGY_IDS)
    main_persona = load_persona("main_default")
    intervention_persona = load_persona("intervention_default")
    task_knowledge = load_task(TASK_ID)

    agent_factory = OpenAIAgentFactory(
        client=client,
        strategies=strategy_store,
        intervention_persona=intervention_persona,
        task_knowledge=task_knowledge,
    )

    # One CoalaPolicy per session, each with its own in-memory stores: state
    # lives for the process's lifetime, not across restarts.
    def policy_factory(context) -> CoalaPolicy:
        return CoalaPolicy(
            strategy_store=strategy_store,
            main_persona=main_persona,
            main_task=task_knowledge,
            episodic_store=InMemoryEpisodicStore(),
            snapshot_store=InMemorySnapshotStore(),
        )

    components_factory = DefaultSessionComponentsFactory(
        agent_factory=agent_factory,
        policy_factory=policy_factory,
        main_agent_options=MainAgentOptions(),
    )

    return create_app(components_factory=components_factory)


def main() -> None:
    host = os.environ.get("MUCA_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("MUCA_SERVER_PORT", "8000"))
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
