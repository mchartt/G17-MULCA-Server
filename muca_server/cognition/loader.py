"""
Loads the three independent config choices: persona, task, active strategies.

Picking a task does not pick strategies, and picking a persona does not pick
either — they are separate knobs, assembled together at startup.
"""

from __future__ import annotations

from pathlib import Path

import yaml

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


def load_strategy_store(
    active_ids: list[str] | None = None,
    *,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    overrides_dir: Path | None = None,
) -> StrategyStore:
    """
    `overrides_dir` (e.g. WAKE_V2_CONFIG_DIR) holds only the strategies that
    differ there; each replaces the one with its id, the rest are kept.
    """

    store = StrategyStore.load_from_dir(
        config_dir / "strategies",
        active_ids=active_ids,
    )
    if overrides_dir is None:
        return store

    overrides = StrategyStore.load_from_dir(overrides_dir / "strategies")
    return StrategyStore(
        {strategy.id: overrides.get(strategy.id) or strategy for strategy in store.all()}
    )


# "wake_v2" mode's own intervention persona and strategies, where they
# differ from the defaults (see run_server.py).
WAKE_V2_CONFIG_DIR = DEFAULT_CONFIG_DIR / "wake_v2"
