"""
Boots MUCA-Server for real, against real OpenAI agents.

The composition root: the one place allowed to pick a task, a persona, and
which strategies are active, mirroring MULCA's old run_discord_bot.py. Every
other module stays generic; nothing outside this file knows what a "general
moderation" task is.

Run with: python -m muca_server.run_server
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from openai import AsyncOpenAI

from .agents import MainAgentOptions
from .agents.conversation import DEFAULT_SILENCE_TIMEOUT_MS
from .agents.openai import ChatCompletionsSummarizer, OpenAIAgentFactory, OpenAIEmbedder
from .cognition import CoalaPolicy
from .cognition.loader import load_persona, load_strategy_store, load_task
from .cognition.memory.sqlite import (
    SqliteEpisodicStore,
    SqliteSnapshotStore,
    db_path_for,
)
from .main import create_app
from .sessions.session_components_factory import DefaultSessionComponentsFactory

TASK_ID = "general_moderation"
# participation_equality_voice deliberately left out: voice is mention-only
# (respond_when_addressed/provide_summary/remember, all solicited) -- the
# strategy and its voice-only roster/trigger-variable plumbing stay in the
# codebase (cognition/config/strategies/participation_equality_voice.yaml),
# just not active, in case that decision changes again later.
ACTIVE_STRATEGY_IDS = [
    "participation_equality_text",
    "respond_when_addressed",
    "non_offensive",
    "provide_summary",
    "remember",
]
ASSISTANT_NAME = "MULCA"

# Anchored to this package's own location, not the current working
# directory -- a relative default here would mean restarting the server
# from a different shell/cwd silently creates a second, empty database
# instead of finding the one from before. Beside the package rather than
# inside it, so conversation data never lands among the source (or in
# site-packages). Set MUCA_DB_DIR for a real deployment.
DEFAULT_DB_DIR = Path(__file__).resolve().parent.parent / "data"


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
        main_persona=main_persona,
        intervention_persona=intervention_persona,
        task_knowledge=task_knowledge,
    )

    # One SQLite file per conversation_id, not one shared file -- e.g. each
    # of a deployment's Discord channels (channel/instance scoping) gets
    # its own database. The directory is what's fixed; the
    # filename is deterministically derived from conversation_id, so there's
    # nothing per-channel to separately configure here (that lives entirely
    # in the Discord-specific client, e.g. mulca_discord's instances.yaml --
    # this file never sees a channel id, only whatever conversation_id it's
    # handed).
    db_dir = os.environ.get("MUCA_DB_DIR", str(DEFAULT_DB_DIR))
    os.makedirs(db_dir, exist_ok=True)

    # Shared across every conversation's store: one client, same as the
    # main/summarizer agents reuse, not a new one per channel.
    embedder = OpenAIEmbedder(client)

    def store_factory(conversation_id: str):
        path = db_path_for(conversation_id, base_dir=db_dir)
        return SqliteEpisodicStore(path, embedder=embedder), SqliteSnapshotStore(path)

    # Without this, the management cycle never runs at
    # all -- CoalaPolicy only calls it when a summarizer is configured, and
    # this was never wired in before, so history_summary/
    # participant_roles_summary never updated and wm.history never
    # truncated in a real deployment.
    summarizer = ChatCompletionsSummarizer(client)

    def policy_factory(context) -> CoalaPolicy:
        return CoalaPolicy(
            strategy_store=strategy_store,
            store_factory=store_factory,
            summarizer=summarizer,
            embedder=embedder,
            assistant_name=ASSISTANT_NAME,
        )

    components_factory = DefaultSessionComponentsFactory(
        agent_factory=agent_factory,
        policy_factory=policy_factory,
        # Without this, MainAgentOptions.name defaults to "ASSISTANT" -- the
        # same missing-name bug as CoalaPolicy's assistant_name above, just
        # in the Conversation layer: the intervention agent's own
        # turn-tracking markers ("MAIN ASSISTANT (...) started speaking")
        # would say "ASSISTANT" instead of "MULCA".
        main_agent_options=MainAgentOptions(name=ASSISTANT_NAME),
        silence_timeout_ms=int(
            os.environ.get(
                "MUCA_SILENCE_TIMEOUT_MS",
                DEFAULT_SILENCE_TIMEOUT_MS,
            )
        ),
    )

    return create_app(components_factory=components_factory)


def main() -> None:
    host = os.environ.get("MUCA_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("MUCA_SERVER_PORT", "8000"))
    # Root stays at INFO always -- a DEBUG root would also unleash
    # httpx/openai/websockets' own verbose internals, not just ours.
    # MUCA_LOG_LEVEL only ever controls the muca_server logger tree
    # specifically (every logger here is logging.getLogger(__name__),
    # under that namespace).
    #
    # Defaults to DEBUG, not INFO: a plain run with nothing special set is
    # a human at a terminal actively watching it, who wants the
    # [voice-turn]-tagged diagnostic detail. Set MUCA_LOG_LEVEL=INFO
    # explicitly for a quiet run (e.g. scripted/live-verification runs that
    # don't need per-tick detail).
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("muca_server").setLevel(
        os.environ.get("MUCA_LOG_LEVEL", "DEBUG"),
    )
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
