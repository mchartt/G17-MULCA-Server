"""
Loads the three independent config choices: persona, task, active strategies.

Picking a task does not pick strategies, and picking a persona does not pick
either — they are separate knobs, assembled together at startup.

Also the course knowledge and schedule (config/knowledge), for the main agent
only.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .course_schedule import load_schedule, render_schedule
from .strategies import StrategyStore

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent / "config"


def load_persona(
    name: str,
    *,
    config_dir: Path = DEFAULT_CONFIG_DIR,
) -> str:
    path = config_dir / "personas" / f"{name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["persona"]


def load_task(
    task_id: str,
    *,
    config_dir: Path = DEFAULT_CONFIG_DIR,
) -> str:
    path = config_dir / "tasks" / f"{task_id}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["description"]


def load_course_schedule(
    *,
    config_dir: Path = DEFAULT_CONFIG_DIR,
) -> dict | None:
    path = config_dir / "knowledge" / "schedule.yaml"
    return load_schedule(path) if path.exists() else None


def load_knowledge(
    *,
    config_dir: Path = DEFAULT_CONFIG_DIR,
) -> str:
    """
    The course material for the main agent's instructions: every
    knowledge/*.md file, then the schedule as text.
    """

    parts = [
        path.read_text(encoding="utf-8").strip()
        for path in sorted((config_dir / "knowledge").glob("*.md"))
    ]
    schedule = load_course_schedule(config_dir=config_dir)
    if schedule is not None:
        parts.append(render_schedule(schedule))
    return "\n\n".join(parts)


def load_strategy_store(
    active_ids: list[str] | None = None,
    *,
    config_dir: Path = DEFAULT_CONFIG_DIR,
) -> StrategyStore:
    return StrategyStore.load_from_dir(
        config_dir / "strategies",
        active_ids=active_ids,
    )
