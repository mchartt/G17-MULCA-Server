"""M3: the three strategy files and when run_server.py offers them."""

from __future__ import annotations

import pytest

from muca_server import run_server
from muca_server.cognition.coordination.proactive_writer import STRATEGY_KINDS
from muca_server.cognition.loader import load_strategy_store


def test_the_three_yaml_files_load_in_the_lab_format():
    store = load_strategy_store(run_server.COORDINATOR_STRATEGY_IDS)
    for strategy_id in run_server.COORDINATOR_STRATEGY_IDS:
        strategy = store.get(strategy_id)
        assert strategy is not None and strategy.modalities == frozenset({"text"})
        # Project rule 11: every trigger also says when NOT to fire.
        assert "Do NOT fire" in strategy.trigger_description
        assert strategy.cooldown_messages == 10
    assert set(run_server.COORDINATOR_STRATEGY_IDS) == set(STRATEGY_KINDS)


def test_they_are_not_in_the_labs_default_catalog():
    assert not set(run_server.COORDINATOR_STRATEGY_IDS) & set(run_server.ACTIVE_STRATEGY_IDS)


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_offered_to_the_model_only_when_the_coordinator_is_on(monkeypatch, tmp_path, enabled):
    seen = []
    real = run_server.load_strategy_store

    def spy(ids=None, **kwargs):
        seen.append(list(ids or []))
        return real(ids, **kwargs)

    monkeypatch.setattr(run_server, "load_strategy_store", spy)
    monkeypatch.setenv("MUCA_COORDINATOR_ENABLED", enabled)
    monkeypatch.setenv("MUCA_DB_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    run_server.build_app()
    with_coordinator = [ids for ids in seen if "cs2_delegate_simple_task" in ids]
    # On: the ambient catalog and the policy's store. Off: nowhere.
    assert len(with_coordinator) == (2 if enabled == "1" else 0)
