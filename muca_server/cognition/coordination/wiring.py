"""
Builds the Group Coordinator's session hook from run_server.py's pieces.

run_server.py calls `build_coordinator_factory(...)` once at boot:
  - coordinator OFF -> returns None, and sessions are built exactly as before;
  - coordinator ON  -> returns a function that, for each new session, wraps
    the Conversation in a CoordinatorRouter (router.py).

Kept apart from run_server.py so the composition root only gains one call,
and so tests can build the same factory with a fake client.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from ..memory.sqlite import db_path_for
from .router import CoordinatorRouter
from .settings import CoordinatorSettings
from .store import CoordinationStore
from .writer import ProposalWriter


def build_coordinator_factory(
    settings: CoordinatorSettings,
    *,
    client: Any,
    db_dir: str,
    usage_tracker: Any = None,
):
    """The per-session coordinator factory, or None when the switch is off."""
    if not settings.enabled:
        return None

    # The lab's own cost recorder, so coordinator calls show up in the usage
    # report next to the agents' (imported here: needs the "openai" extra).
    from ...agents.openai.main_agent import record_cost

    writer = ProposalWriter(
        client,
        record_cost=partial(record_cost, tracker=usage_tracker),
    )

    def store_for(conversation_id: str) -> CoordinationStore:
        # The conversation's own SQLite file (cognition/memory/sqlite.py).
        return CoordinationStore(db_path_for(conversation_id, base_dir=db_dir))

    def factory(conversation: Any, outbound_queue: asyncio.Queue) -> CoordinatorRouter:
        return CoordinatorRouter(
            conversation,
            outbound_queue,
            store_for=store_for,
            writer=writer,
            proposal_ttl_seconds=settings.proposal_ttl_seconds,
        )

    return factory
