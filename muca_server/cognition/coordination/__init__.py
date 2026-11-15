"""
The Group Coordinator (AUI 2026-27, Group 17): project state, proposals that
change it only after a member's click, and the session hook. See INDICE.md.
"""

from .models import ProjectState, Proposal, ProposalKind, ProposalStatus
from .proposals import ProposalService
from .router import CoordinatorRouter
from .settings import CoordinatorSettings
from .store import CoordinationStore
from .writer import ProposalWriter

__all__ = [
    "CoordinationStore",
    "CoordinatorRouter",
    "CoordinatorSettings",
    "ProjectState",
    "Proposal",
    "ProposalKind",
    "ProposalService",
    "ProposalStatus",
    "ProposalWriter",
]
