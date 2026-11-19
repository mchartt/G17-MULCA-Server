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

from ..memory.sqlite import SqliteEpisodicStore, db_path_for
from .catchup import CatchUp
from .links import GroupLinks, LiveGroups
from .proactive_writer import ProactiveWriter
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
        model=settings.writer_model,
        record_cost=partial(record_cost, tracker=usage_tracker),
    )

    proactive_writer = ProactiveWriter(writer)

    def store_for(conversation_id: str) -> CoordinationStore:
        # The conversation's own SQLite file (cognition/memory/sqlite.py).
        return CoordinationStore(db_path_for(conversation_id, base_dir=db_dir))

    # Linked groups (links.py): one table on disk, the open sessions in memory.
    links = GroupLinks(db_dir)
    live = LiveGroups()
    catchup = CatchUp(lambda conversation_id: SqliteEpisodicStore(db_path_for(conversation_id, base_dir=db_dir)))

    def factory(conversation: Any, outbound_queue: asyncio.Queue) -> CoordinatorRouter:
        router = CoordinatorRouter(
            conversation,
            outbound_queue,
            store_for=store_for,
            writer=writer,
            proposal_ttl_seconds=settings.proposal_ttl_seconds,
            proactive_writer=proactive_writer,
            checkin_interval_seconds=settings.checkin_interval_seconds,
            links=links,
            live=live,
            catchup=catchup,
        )
        attach_proactive(conversation, router)
        return router

    return factory


def attach_proactive(conversation: Any, router: CoordinatorRouter) -> bool:
    """
    Plug the router's ProactiveBridge into the session's CoalaPolicy, so
    CS5/CS2/CS3 become cards. The policy lives on the Conversation as
    `_policy` (agents/conversation.py, set in __init__); reading it here keeps
    the shared file unchanged. Returns False (and the coordinator works
    without proactive cards) for a policy without the hook, e.g. NoCognition
    in tests: test_coordinator_proactive.py checks both cases.
    """
    policy = getattr(conversation, "_policy", None)
    setter = getattr(policy, "set_proactive_bridge", None)
    if setter is None or router.proactive is None:
        return False
    setter(router.proactive)
    return True
