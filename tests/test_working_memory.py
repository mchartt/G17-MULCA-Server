from muca_server.cognition import WorkingMemory
from muca_server.cognition.strategies import Strategy


def make_strategy(cooldown: int = 5) -> Strategy:
    return Strategy(
        id="participation_equality",
        trigger_description="",
        prompt_template="{prompt_fragment}",
        cooldown_messages=cooldown,
    )


def test_a_strategy_with_no_cooldown_never_cools_down():
    wm = WorkingMemory(conversation_id="c1")
    strategy = make_strategy(cooldown=0)
    wm.record_action(strategy.id, 1)

    assert not wm.strategy_in_cooldown(strategy)


def test_a_strategy_is_in_cooldown_right_after_firing():
    wm = WorkingMemory(conversation_id="c1")
    strategy = make_strategy(cooldown=5)
    wm.total_message_count = 10
    wm.record_action(strategy.id, 10)

    assert wm.strategy_in_cooldown(strategy)


def test_a_strategy_leaves_cooldown_after_enough_messages():
    wm = WorkingMemory(conversation_id="c1")
    strategy = make_strategy(cooldown=5)
    wm.total_message_count = 10
    wm.record_action(strategy.id, 10)
    wm.total_message_count = 16

    assert not wm.strategy_in_cooldown(strategy)


def test_cooldown_survives_a_history_truncation():
    """
    The bug this class exists to prevent: MULCA once measured cooldown
    against len(history), which the management cycle truncates -- so a
    strategy that fired once could look permanently on cooldown, or its
    cooldown could reset early, depending on truncation timing.
    """
    wm = WorkingMemory(conversation_id="c1")
    strategy = make_strategy(cooldown=5)
    wm.total_message_count = 10
    wm.record_action(strategy.id, 10)

    # A management cycle truncates history but total_message_count is
    # untouched -- it must be, since it is the cooldown clock.
    wm.apply_management_cycle("summary", "roles")
    assert len(wm.history) == 0

    assert wm.strategy_in_cooldown(strategy)

    wm.total_message_count = 16
    assert not wm.strategy_in_cooldown(strategy)


def test_a_forced_context_and_a_selected_strategy_are_mutually_exclusive():
    wm = WorkingMemory(conversation_id="c1")

    wm.set_intervention_result("provide_summary", "give a recap")
    wm.set_forced_context("you were addressed directly")

    assert wm.latest_strategy_id is None
    assert wm.latest_prompt_fragment is None

    wm.set_intervention_result("provide_summary", "give a recap")
    assert wm.forced_context is None


def test_clear_turn_output_forgets_everything_from_the_turn():
    wm = WorkingMemory(conversation_id="c1")
    wm.set_intervention_result("provide_summary", "give a recap")
    wm.set_retrieved_context("some history")

    wm.clear_turn_output()

    assert wm.latest_strategy_id is None
    assert wm.latest_prompt_fragment is None
    assert wm.forced_context is None
    assert wm.retrieved_context is None
