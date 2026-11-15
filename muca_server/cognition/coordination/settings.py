"""
The Group Coordinator's on/off switch and its few settings.

OFF by default. When off, run_server.py builds no coordinator at all and
MUCA-Server behaves exactly as before (tests/coordinator/test_coordinator_switch.py).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .proposals import DEFAULT_TTL_SECONDS
from .writer import DEFAULT_WRITER_MODEL

ENABLED_ENV_VAR = "MUCA_COORDINATOR_ENABLED"
TTL_ENV_VAR = "MUCA_COORDINATOR_PROPOSAL_TTL_HOURS"
# Hours between two timed check-ins (checkin_timer.py); unset or 0 = only on command.
CHECKIN_ENV_VAR = "MUCA_COORDINATOR_CHECKIN_HOURS"
# The text model that drafts every card (writer.py); on Azure, the deployment name.
MODEL_ENV_VAR = "MUCA_COORDINATOR_MODEL"
TRUE_WORDS = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class CoordinatorSettings:
    enabled: bool = False
    proposal_ttl_seconds: float = DEFAULT_TTL_SECONDS
    checkin_interval_seconds: float = 0.0
    writer_model: str = DEFAULT_WRITER_MODEL

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "CoordinatorSettings":
        """Read the switch, the proposal lifetime and the check-in interval (hours) from the environment."""
        enabled = env.get(ENABLED_ENV_VAR, "").strip().lower() in TRUE_WORDS
        ttl_hours = env.get(TTL_ENV_VAR, "").strip()
        checkin_hours = env.get(CHECKIN_ENV_VAR, "").strip()
        return cls(
            writer_model=env.get(MODEL_ENV_VAR, "").strip() or DEFAULT_WRITER_MODEL,
            checkin_interval_seconds=max(0.0, float(checkin_hours) * 3600) if checkin_hours else 0.0,
            enabled=enabled,
            proposal_ttl_seconds=(
                float(ttl_hours) * 3600 if ttl_hours else DEFAULT_TTL_SECONDS
            ),
        )
