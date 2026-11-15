"""
ProposalService: the life cycle of a proposal, and the only way a proposal
can change the project state.

    create()   -> a new PENDING proposal (nothing in the state changes)
    respond()  -> a member clicks Confirm or Reject:
                    1. the member must be in the group           (else refused)
                    2. the proposal must exist and be pending    (else refused)
                    3. it must not be past its expiry            (else EXPIRED)
                    4. it is checked again against today's state (else refused)
                    5. CONFIRM: status APPLIED and the change, in one transaction
                       REJECT:  status REJECTED, nothing else changes

Expiry is checked when someone clicks (step 3); clients can also show
"expired" on their own from `expires_at`, so no background timer is needed.

Each proposal kind has one *applier*: the function that writes its change.
Adding a kind = one payload model (models.py) + one applier here.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .models import (
    Charter,
    CharterPayload,
    Milestone,
    Proposal,
    ProposalKind,
    ProposalStatus,
)
from .store import Applier, CoordinationStore

# How long a proposal waits for a click before it expires.
DEFAULT_TTL_SECONDS = 48 * 3600


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
        payload: CharterPayload,
        *,
        rationale: str,
        origin_channel: str,
    ) -> Proposal:
        """Save a new pending proposal. The project state does not change."""
        now = self._clock()
        proposal = Proposal(
            proposal_id=new_id(),
            kind=kind,
            payload=payload.to_wire(),
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
    ) -> Outcome:
        """A member's click. See the module docstring for the five checks."""
        # 1. Only members of the group may decide.
        if member_id not in participants:
            return Outcome(False, "Only members of this group can answer this card.")

        # 2. It must exist and still be open.
        proposal = await self._store.get_proposal(proposal_id)
        if proposal is None:
            return Outcome(False, "This card no longer exists.")
        if proposal.status is not ProposalStatus.PENDING:
            return Outcome(
                False,
                f"This card was already {proposal.status.value}.",
                proposal,
            )

        # 3. Expired cards cannot be confirmed (they are closed as expired).
        now = self._clock()
        if now >= proposal.expires_at:
            await self._store.resolve_proposal(
                proposal_id, ProposalStatus.EXPIRED, member_id=None, now=now
            )
            return Outcome(
                False,
                "This card has expired.",
                await self._store.get_proposal(proposal_id),
            )

        if action == "reject":
            closed = await self._store.resolve_proposal(
                proposal_id, ProposalStatus.REJECTED, member_id=member_id, now=now
            )
            return await self._result(proposal_id, closed, "Rejected.")

        if action != "confirm":
            return Outcome(False, f"Unknown action {action!r}.", proposal)

        # 4. Checked again on today's state, then 5. applied atomically.
        problem, applier = self._prepare(proposal, member_id=member_id, now=now)
        if problem:
            return Outcome(False, problem, proposal)
        applied = await self._store.resolve_proposal(
            proposal_id,
            ProposalStatus.APPLIED,
            member_id=member_id,
            now=now,
            apply=applier,
        )
        return await self._result(proposal_id, applied, "Confirmed and saved.")

    async def _result(self, proposal_id: str, changed: bool, done: str) -> Outcome:
        """Common ending: re-read the proposal; if nothing changed, someone was first."""
        proposal = await self._store.get_proposal(proposal_id)
        if not changed:
            return Outcome(False, "Someone else answered this card first.", proposal)
        return Outcome(True, done, proposal)

    def _prepare(
        self,
        proposal: Proposal,
        *,
        member_id: int,
        now: float,
    ) -> tuple[str, Applier | None]:
        """
        Validate the proposal again and build its applier.
        Returns (problem, applier): a non-empty problem means "do not apply".
        """
        try:
            payload = proposal.typed_payload()
        except ValueError as exc:  # pydantic.ValidationError is a ValueError
            return f"This card is no longer valid: {exc}", None

        if proposal.kind is ProposalKind.CHARTER:
            assert isinstance(payload, CharterPayload)
            return "", _charter_applier(payload, member_id=member_id, now=now)

        return f"Unsupported card kind {proposal.kind.value!r}.", None


def _charter_applier(payload: CharterPayload, *, member_id: int, now: float) -> Applier:
    """The change a confirmed charter makes: charter + milestones replaced."""
    charter = Charter(
        goal=payload.goal,
        deliverables=payload.deliverables,
        confirmed_by=member_id,
        confirmed_at=now,
    )
    milestones = [
        Milestone(milestone_id=new_id(), title=m.title, due_date=m.due_date)
        for m in payload.milestones
    ]

    async def apply(db) -> None:
        await CoordinationStore.write_charter(db, charter, milestones)

    return apply
