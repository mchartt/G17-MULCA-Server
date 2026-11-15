"""
CardRequests: two small member actions on a card that never change the
project state, used by router.py.

    helpful   proposal_response action "helpful" on a proactive card (spec
              FR21): saved in coord_feedback, once per member; the card and
              the plan stay as they are. It tells the group (and the report)
              which interventions were worth it.
    card      coordinator_command "card", args.proposal_id: the card as the
              server has it, sent back only to the asker inside the notice.
              Discord uses it for "Why?" after the bot restarted and lost
              the cards it kept in memory.

Both are a member's own action: no model call, no proposal, no state change.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from .store import CoordinationStore

HELPFUL_ONLY_PROACTIVE = "“Helpful” is for the cards the coordinator posted on its own."
NO_SUCH_CARD = "That card is not in this group."


class CardRequests:
    def __init__(
        self,
        *,
        store: Callable[[], CoordinationStore],
        notice: Callable[..., Awaitable[None]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._notice = notice
        self._clock = clock

    async def helpful(self, message: Any) -> None:
        """Record "Helpful" from this member on a proactive card (once each)."""
        proposal = await self._store().get_proposal(message.proposal_id)
        if proposal is None:
            await self._notice(False, NO_SUCH_CARD, message.request_id)
            return
        if "context" not in proposal.payload:
            await self._notice(False, HELPFUL_ONLY_PROACTIVE, message.request_id)
            return
        count = await self._store().add_feedback(proposal.proposal_id, message.member_id, self._clock())
        people = "1 member" if count == 1 else f"{count} members"
        await self._notice(True, f"Thanks: noted as helpful ({people} so far).", message.request_id)

    async def card(self, message: Any) -> None:
        """Send one card back to the member who asked (no state change)."""
        proposal_id = str(message.args.get("proposal_id", "")).strip()
        proposal = await self._store().get_proposal(proposal_id) if proposal_id else None
        if proposal is None:
            await self._notice(False, NO_SUCH_CARD, message.request_id)
            return
        await self._notice(True, "", message.request_id, proposal=proposal.to_wire())
