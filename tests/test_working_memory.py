from muca_server.cognition import Utterance, WorkingMemory
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
    wm.record_action(strategy.id, 10, now=0.0)
    wm.total_message_count = 16

    # now=61: past both the message-count gate and the wall-clock one, so
    # this isolates the message-count gate specifically.
    assert not wm.strategy_in_cooldown(strategy, now=61.0)


def test_the_wall_clock_gate_vetoes_even_once_enough_messages_have_passed():
    """
    Voice produces more completed turns per real second than text (people
    talk faster than they type), so the same message-count window alone
    lets a nagging strategy like participation_equality refire far more
    often, in real time, in a voice call than it would in text. This
    secondary, flat wall-clock floor is what actually bounds that.
    """

    wm = WorkingMemory(conversation_id="c1")
    strategy = make_strategy(cooldown=1)
    wm.total_message_count = 10
    wm.record_action(strategy.id, 10, now=100.0)
    wm.total_message_count = 11  # Message-count gate alone is satisfied.

    assert wm.strategy_in_cooldown(strategy, now=155.0)  # Only 55s passed.
    assert not wm.strategy_in_cooldown(strategy, now=161.0)  # 61s passed.


def test_cooldown_survives_a_history_truncation():
    """
    The bug this class exists to prevent: MULCA once measured cooldown
    against len(history), which the recent window truncates -- so a
    strategy that fired once could look permanently on cooldown, or its
    cooldown could reset early, depending on truncation timing.
    """
    wm = WorkingMemory(conversation_id="c1")
    strategy = make_strategy(cooldown=5)
    wm.total_message_count = 10
    wm.record_action(strategy.id, 10, now=0.0)

    # Trimming to the recent window leaves total_message_count untouched --
    # it must, since it is the cooldown clock.
    wm.trim_history(tail_size=8)
    assert len(wm.history) == 0

    assert wm.strategy_in_cooldown(strategy, now=20.0)

    wm.total_message_count = 16
    assert not wm.strategy_in_cooldown(strategy, now=61.0)


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


def test_transcript_includes_the_assistant_by_default():
    wm = WorkingMemory(conversation_id="c1", assistant_id=0)
    wm.append_history(
        Utterance(speaker_id=1, speaker_name="Ada", content="hi", timestamp_ms=1)
    )
    wm.append_history(
        Utterance(speaker_id=0, speaker_name="MULCA", content="hello", timestamp_ms=2)
    )

    transcript = wm.transcript()

    assert "Ada: hi" in transcript
    assert "MULCA: hello" in transcript


def test_transcript_can_exclude_the_assistant_for_the_summarizer():
    wm = WorkingMemory(conversation_id="c1", assistant_id=0)
    wm.append_history(
        Utterance(speaker_id=1, speaker_name="Ada", content="hi", timestamp_ms=1)
    )
    wm.append_history(
        Utterance(speaker_id=0, speaker_name="MULCA", content="hello", timestamp_ms=2)
    )

    transcript = wm.transcript(exclude_assistant=True)

    assert "Ada: hi" in transcript
    assert "MULCA" not in transcript
