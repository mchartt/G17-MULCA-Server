"""
A durable, process-wide record of what this MUCA-Server process has spent,
broken down two ways: by agent type (main/intervention, with its token
modality detail) and by instance (which bot/team a session belongs to, via
ConnectAgentMessage.bot_name -- see conversation.py's _instance_label) --
plus a Discord webhook alert every time the running total crosses another
MUCA_COST_ALERT_THRESHOLD_STEP_USD.

The real deployment runs one shared MUCA-Server process for every bot/team
(see architecture/deploy/stack.yml: muca-server has replicas: 1), so this
process's own total already is the deployment-wide total -- there is no
cross-server aggregation to do.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import socket
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# The session making the current provider calls: its instance label, and
# where its `usage` events go -- (kind, tokens, cost). Set by
# Conversation.connect_agent; every task the session starts afterwards
# inherits it, which is how calls made by objects shared across sessions
# (the summarizer, the embedder) know whose they are.
UsageSink = tuple[str, Callable[[str, tuple, float], Awaitable[None]]]
usage_sink: ContextVar[UsageSink | None] = ContextVar("usage_sink", default=None)


@dataclass
class AgentTotals:
    fresh_text: int = 0
    cached_text: int = 0
    fresh_audio: int = 0
    cached_audio: int = 0
    out_text: int = 0
    out_audio: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    connects: int = 0
    audio_ms: float = 0.0


@dataclass
class InstanceTotals:
    cost_usd: float = 0.0
    calls: int = 0
    connects: int = 0
    audio_ms: float = 0.0


class UsageTracker:
    def __init__(
        self,
        path: Path,
        *,
        server_name: str | None = None,
        webhook_url: str | None = None,
        threshold_step_usd: float | None = None,
        admin_token: str | None = None,
    ) -> None:
        self._path = path
        self.server_name = server_name or socket.gethostname()
        # Precedence, lowest to highest: the hardcoded default (None), then
        # the env-var-derived constructor args above, then whatever's
        # actually saved on disk -- an admin's explicit /mulca costalert
        # choice should outlive both of those across a restart, same as
        # ModelSettings.
        self._webhook_url = webhook_url
        self._threshold_step = threshold_step_usd
        self._admin_token = admin_token
        self._agents: dict[str, AgentTotals] = {}
        self._instances: dict[str, InstanceTotals] = {}
        self._alerted_threshold = 0.0
        self._alert_lock = asyncio.Lock()

        if path.exists():
            saved = json.loads(path.read_text())
            self._alerted_threshold = saved.get("alerted_threshold", 0.0)
            self._webhook_url = saved.get("webhook_url", self._webhook_url)
            self._threshold_step = saved.get(
                "threshold_step_usd", self._threshold_step
            )
            for agent_type, values in saved.get("agents", {}).items():
                self._agents[agent_type] = AgentTotals(**values)
            for instance, values in saved.get("instances", {}).items():
                self._instances[instance] = InstanceTotals(**values)

        logger.info(
            "Usage tracker for %s: total so far $%.4f (from %s)",
            self.server_name,
            self.total_cost_usd,
            f"saved report at {path}" if path.exists() else "a fresh start",
        )

    def _agent_bucket(self, agent_type: str) -> AgentTotals:
        return self._agents.setdefault(agent_type, AgentTotals())

    def _instance_bucket(self, instance: str) -> InstanceTotals:
        return self._instances.setdefault(instance or "(unlabeled)", InstanceTotals())

    @property
    def total_cost_usd(self) -> float:
        return sum(agent.cost_usd for agent in self._agents.values())

    def record_usage(
        self,
        agent_type: str,
        instance: str,
        tokens: tuple[int, int, int, int, int, int],
        cost: float,
    ) -> None:
        agent = self._agent_bucket(agent_type)
        (
            agent.fresh_text,
            agent.cached_text,
            agent.fresh_audio,
            agent.cached_audio,
            agent.out_text,
            agent.out_audio,
        ) = (
            agent.fresh_text + tokens[0],
            agent.cached_text + tokens[1],
            agent.fresh_audio + tokens[2],
            agent.cached_audio + tokens[3],
            agent.out_text + tokens[4],
            agent.out_audio + tokens[5],
        )
        agent.cost_usd += cost
        agent.calls += 1

        instance_totals = self._instance_bucket(instance)
        instance_totals.cost_usd += cost
        instance_totals.calls += 1

        self._save()

    def record_connect(self, agent_type: str, instance: str) -> None:
        self._agent_bucket(agent_type).connects += 1
        self._instance_bucket(instance).connects += 1
        self._save()

    def record_audio_ms(
        self, agent_type: str, instance: str, duration_ms: float
    ) -> None:
        self._agent_bucket(agent_type).audio_ms += duration_ms
        self._instance_bucket(instance).audio_ms += duration_ms
        self._save()

    def _save(self) -> None:
        self._path.write_text(
            json.dumps(
                {
                    "alerted_threshold": self._alerted_threshold,
                    "webhook_url": self._webhook_url,
                    "threshold_step_usd": self._threshold_step,
                    "agents": {
                        agent_type: asdict(totals)
                        for agent_type, totals in self._agents.items()
                    },
                    "instances": {
                        instance: asdict(totals)
                        for instance, totals in self._instances.items()
                    },
                }
            )
        )

    def apply(
        self,
        *,
        admin_token: str,
        webhook_url: str | None = None,
        threshold_usd: float | None = None,
    ) -> None:
        """
        Admin: change the webhook URL and/or $ step, live, no restart
        needed -- unlike set_model, nothing a connected agent is doing
        depends on these. Persisted, so it survives this process's own
        restart too.
        """

        if not self._admin_token or not hmac.compare_digest(
            admin_token.encode(),
            self._admin_token.encode(),
        ):
            raise PermissionError("Invalid admin token")

        if webhook_url is not None:
            self._webhook_url = webhook_url
        if threshold_usd is not None:
            self._threshold_step = threshold_usd
        self._save()

        logger.info(
            "Cost alert settings changed: webhook=%s, threshold=$%s",
            "set" if self._webhook_url else "unset",
            self._threshold_step,
        )

    def report_text(self) -> str:
        lines = [
            f"**MUCA-Server usage -- {self.server_name}**",
            f"Overall cost so far: ${self.total_cost_usd:.2f}",
            "",
            "By agent:",
        ]
        for agent_type, totals in sorted(self._agents.items()):
            minutes = totals.audio_ms / 60_000
            lines.append(
                f"- **{agent_type}**: ${totals.cost_usd:.2f}, {totals.calls} "
                f"msgs, {totals.connects} connects, {minutes:.1f} min audio "
                f"(in {totals.fresh_text + totals.cached_text} text / "
                f"{totals.fresh_audio + totals.cached_audio} audio tok, "
                f"out {totals.out_text} text / {totals.out_audio} audio tok)"
            )

        if self._instances:
            lines.append("")
            lines.append("By instance:")
            for instance, totals in sorted(
                self._instances.items(), key=lambda item: -item[1].cost_usd
            ):
                minutes = totals.audio_ms / 60_000
                lines.append(
                    f"- **{instance}**: ${totals.cost_usd:.2f}, {totals.calls} "
                    f"msgs, {totals.connects} connects, {minutes:.1f} min audio"
                )

        return "\n".join(lines)

    async def maybe_alert(self) -> None:
        """Post to Discord every time the total crosses another step."""

        if not self._webhook_url or not self._threshold_step:
            return

        total = self.total_cost_usd
        next_threshold = (
            math.floor(self._alerted_threshold / self._threshold_step) + 1
        ) * self._threshold_step
        if total < next_threshold:
            return

        async with self._alert_lock:
            # Re-check: another concurrent call may have already alerted
            # past this point while this one waited for the lock.
            next_threshold = (
                math.floor(self._alerted_threshold / self._threshold_step) + 1
            ) * self._threshold_step
            if total < next_threshold:
                return
            # Jump straight to the highest step actually crossed, rather
            # than sending one message per dollar if usage spiked between
            # checks -- there is nothing to catch up on in between.
            threshold = (
                math.floor(total / self._threshold_step) * self._threshold_step
            )
            self._alerted_threshold = threshold
            self._save()

        text = (
            f":rotating_light: **${threshold:.0f} spend threshold crossed**\n"
            f"{self.report_text()}"
        )
        # Discord webhooks reject content over 2000 characters outright; a
        # 20-bot per-instance breakdown could exceed that.
        if len(text) > 1900:
            text = text[:1900] + "\n... (truncated)"
        # Only here: httpx comes with the openai extra, and this module is
        # imported by conversation.py, which a client (MULCA) imports too.
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self._webhook_url,
                    json={"content": text},
                )
                response.raise_for_status()
        except Exception:
            logger.exception(
                "Could not post the $%.0f cost alert to Discord", threshold
            )
