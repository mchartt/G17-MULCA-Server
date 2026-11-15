"""
ProposalService: the life cycle of a proposal, and the only way a proposal
can change the project state.

    create()   -> a new PENDING proposal (nothing in the state changes)
    respond()  -> a member clicks a button on the card:
                    1. the member must be in the group           (else refused)
                    2. the proposal must exist and be pending    (else refused)
                    3. it must not be past its expiry            (else EXPIRED)
                    4. the button must make sense for this card  (else refused)
                    5. it is checked again against today's state (else refused,
                       or closed as expired inside the transaction)
                    6. APPLIED / REJECTED / SNOOZED and the change, in one transaction

Buttons (the `action` of proposal_response):
    confirm   charter, plan, blocker, or a task_owner card with a suggested owner
    take      "I'll take it": task_owner and delegation, the clicker becomes owner
    choose    decision and checkin: `option` is the index of the chosen option
    reject    any card: closed, nothing changes
    not_now   proactive cards only: closed as SNOOZED, and that strategy stays
              silent on that topic for SNOOZE_SECONDS (spec FR21, GL10)

Expiry is checked when someone clicks (step 3); clients can also show
"expired" on their own from `expires_at`, so no background timer is needed.
The changes themselves are written by appliers.py.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from .appliers import (
    StaleProposal,
    blocker_applier,
    charter_applier,
    checkin_applier,
    decision_applier,
    plan_applier,
    snooze_applier,
    task_applier,
)
from .models import (
    BlockerPayload,
    CharterPayload,
    CheckinPayload,
    DecisionPayload,
    PlanPayload,
    Proposal,
    ProposalKind,
    ProposalStatus,
    TaskAssignmentPayload,
)
from .store import Applier, CoordinationStore

# How long a proposal waits for a click before it expires.
DEFAULT_TTL_SECONDS = 48 * 3600
# "Not now": how long a strategy stays silent on a topic (spec FR21, 30 minutes).
SNOOZE_SECONDS = 30 * 60

ACTIONS = ("confirm", "take", "choose", "reject", "not_now")

__all__ = ["DEFAULT_TTL_SECONDS", "SNOOZE_SECONDS", "Outcome", "ProposalService", "StaleProposal", "new_id"]


@dataclass(frozen=True, slots=True)
class Outcome:
    """What respond() did: ok or not, the proposal as it is now, and why."""

    ok: bool
    message: str
    proposal: Proposal | None = None


def new_id() -> str:
    """Short random id, enough for one group's proposals, milestones and tasks."""
    return uuid.uuid4().hex[:12]


class ProposalService:
    def __init__(
        self,
        store: CoordinationStore,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._ttl = ttl_seconds
        self._clock = clock

    async def create(
        self,
        kind: ProposalKind,
        payload: BaseModel,
        *,
        rationale: str,
        origin_channel: str,
    ) -> Proposal:
        """Save a new pending proposal. The project state does not change."""
        now = self._clock()
        proposal = Proposal(
            proposal_id=new_id(),
            kind=kind,
            payload=payload.model_dump(mode="json"),
            rationale=rationale,
            origin_channel=origin_channel,
            created_at=now,
            expires_at=now + self._ttl,
        )
        # Same validation the applier will use: a bad payload never gets stored.
        proposal.typed_payload()
        await self._store.add_proposal(proposal)
        return proposal

    async def respond(
        self,
        proposal_id: str,
        *,
        action: str,
        member_id: int,
        participants: dict[int, str],
        option: int | None = None,
    ) -> Outcome:
        """A member's click. See the module docstring for the six checks."""
        # 1. Only members of the group may decide.
        if member_id not in participants:
            return Outcome(False, "Only members of this group can answer this card.")

        # 2. It must exist and still be open.
        proposal = await self._store.get_proposal(proposal_id)
        if proposal is None:
            return Outcome(False, "This card no longer exists.")
        if proposal.status is not ProposalStatus.PENDING:
            return Outcome(False, f"This card was already {proposal.status.value}.", proposal)

        # 3. Expired cards cannot be answered (they are closed as expired).
        now = self._clock()
        if now >= proposal.expires_at:
            await self._store.resolve_proposal(
                proposal_id, ProposalStatus.EXPIRED, member_id=None, now=now
            )
            return Outcome(False, "This card has expired.", await self._store.get_proposal(proposal_id))

        if action == "reject":
            closed = await self._store.resolve_proposal(
                proposal_id, ProposalStatus.REJECTED, member_id=member_id, now=now
            )
            return await self._result(proposal_id, closed, "Rejected.")

        # 4. The button must belong to this kind of card; 5. checked again.
        try:
            payload = proposal.typed_payload()
        except ValueError as exc:  # pydantic.ValidationError is a ValueError
            return Outcome(False, f"This card is no longer valid: {exc}", proposal)
        if action == "not_now":
            return await self._snooze(proposal, payload, member_id=member_id, now=now)
        problem, applier, done = self._prepare(
            proposal,
            payload,
            action=action,
            option=option,
            member_id=member_id,
            participants=participants,
            now=now,
        )
        if problem:
            return Outcome(False, problem, proposal)

        # 6. Applied atomically.
        try:
            applied = await self._store.resolve_proposal(
                proposal_id,
                ProposalStatus.APPLIED,
                member_id=member_id,
                now=now,
                apply=applier,
            )
        except StaleProposal as exc:
            # Rolled back: close the card so it cannot be answered later.
            await self._store.resolve_proposal(
                proposal_id, ProposalStatus.EXPIRED, member_id=None, now=now
            )
            return Outcome(False, str(exc), await self._store.get_proposal(proposal_id))
        return await self._result(proposal_id, applied, done)

    async def _snooze(self, proposal: Proposal, payload, *, member_id: int, now: float) -> Outcome:
        """"Not now": only on proactive cards (they carry a strategy and a topic)."""
        context = getattr(payload, "context", None)
        if context is None:
            return Outcome(False, "Not now is only for cards the coordinator posted on its own.", proposal)
        snoozed = await self._store.resolve_proposal(
            proposal.proposal_id,
            ProposalStatus.SNOOZED,
            member_id=member_id,
            now=now,
            apply=snooze_applier(
                strategy_id=context.strategy_id,
                topic=context.topic,
                until=now + SNOOZE_SECONDS,
            ),
        )
        minutes = SNOOZE_SECONDS // 60
        return await self._result(
            proposal.proposal_id,
            snoozed,
            f"Got it: no more suggestions like this about “{context.topic}” for {minutes} minutes.",
        )

    async def _result(self, proposal_id: str, changed: bool, done: str) -> Outcome:
        """Common ending: re-read the proposal; if nothing changed, someone was first."""
        proposal = await self._store.get_proposal(proposal_id)
        if not changed:
            return Outcome(False, "Someone else answered this card first.", proposal)
        return Outcome(True, done, proposal)

    def _prepare(
        self,
        proposal: Proposal,
        payload,
        *,
        action: str,
        option: int | None,
        member_id: int,
        participants: dict[int, str],
        now: float,
    ) -> tuple[str, Applier | None, str]:
        """
        Which change this click makes. Returns (problem, applier, message):
        a non-empty problem means "do not apply" and is shown to the member.
        """
        if action not in ACTIONS:
            return f"Unknown action {action!r}.", None, ""
        kind = proposal.kind

        if kind is ProposalKind.CHARTER and action == "confirm":
            assert isinstance(payload, CharterPayload)
            return "", charter_applier(
                payload,
                member_id=member_id,
                now=now,
                created_at=proposal.created_at,
                new_id=new_id,
            ), "Confirmed and saved."

        if kind in (ProposalKind.TASK_OWNER, ProposalKind.DELEGATION) and action in ("confirm", "take"):
            assert isinstance(payload, TaskAssignmentPayload)
            if action == "take":
                owner = member_id
            elif kind is ProposalKind.TASK_OWNER and payload.owner_id is not None:
                owner = payload.owner_id
            else:
                return "Nobody is suggested on this card: press “I'll take it”.", None, ""
            # The owner must be in the group now, not only when the card was made.
            if owner not in participants:
                return "The suggested owner is not in this group any more.", None, ""
            return "", task_applier(payload, owner_id=owner, new_task_id=new_id()), (
                f"Saved: {participants[owner]} owns “{payload.title}”."
            )

        if kind is ProposalKind.DECISION and action == "choose":
            assert isinstance(payload, DecisionPayload)
            if option is None or not 0 <= option < len(payload.options):
                return "Choose one of the options on the card.", None, ""
            return "", decision_applier(
                payload,
                option=option,
                member_id=member_id,
                now=now,
                decision_id=new_id(),
                proposal_id=proposal.proposal_id,
            ), f"Decision saved: {payload.options[option].label}."

        if kind is ProposalKind.PLAN and action == "confirm":
            assert isinstance(payload, PlanPayload)
            gone = [i.title for i in payload.items if i.owner_id is not None and i.owner_id not in participants]
            if gone:
                return f"A suggested owner is not in this group any more ({gone[0]}).", None, ""
            return "", plan_applier(payload, new_id=new_id), f"Plan saved: {len(payload.items)} task(s)."

        if kind is ProposalKind.CHECKIN and action == "choose":
            assert isinstance(payload, CheckinPayload)
            if option is None or not 0 <= option < len(payload.options):
                return "Choose one of the options on the card.", None, ""
            chosen = payload.options[option]
            if chosen.new_owner_id is not None and chosen.new_owner_id not in participants:
                return "That member is not in this group any more.", None, ""
            return "", checkin_applier(chosen), f"Plan updated: {chosen.label}."

        if kind is ProposalKind.BLOCKER and action == "confirm":
            assert isinstance(payload, BlockerPayload)
            return "", blocker_applier(payload, new_task_id=new_id()), f"Marked as blocked: \u201c{payload.title}\u201d."

        return f"This button does not apply to a {kind.value} card.", None, ""
