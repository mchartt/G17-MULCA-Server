"""
The Group Coordinator's on/off switch and its few settings.

OFF by default. When off, run_server.py builds no coordinator at all and
MUCA-Server behaves exactly as before (tests/coordinator/test_switch_off.py).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .proposals import DEFAULT_TTL_SECONDS

ENABLED_ENV_VAR = "MUCA_COORDINATOR_ENABLED"
TTL_ENV_VAR = "MUCA_COORDINATOR_PROPOSAL_TTL_HOURS"
TRUE_WORDS = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class CoordinatorSettings:
    enabled: bool = False
    proposal_ttl_seconds: float = DEFAULT_TTL_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "CoordinatorSettings":
        """Read the switch (and the proposal lifetime, in hours) from the environment."""
        enabled = env.get(ENABLED_ENV_VAR, "").strip().lower() in TRUE_WORDS
        ttl_hours = env.get(TTL_ENV_VAR, "").strip()
        return cls(
            enabled=enabled,
            proposal_ttl_seconds=(
                float(ttl_hours) * 3600 if ttl_hours else DEFAULT_TTL_SECONDS
            ),
        )
