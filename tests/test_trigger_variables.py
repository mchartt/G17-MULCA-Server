from muca_server.cognition import Utterance, compute_trigger_variables, gini_index


def utterance(speaker_id: int, name: str, content: str = "hi") -> Utterance:
    return Utterance(
        speaker_id=speaker_id,
        speaker_name=name,
        content=content,
        timestamp_ms=0,
    )


def test_gini_index_is_zero_for_equal_participation():
    assert gini_index([5, 5, 5]) == 0.0


def test_gini_index_is_zero_for_a_single_speaker():
    assert gini_index([7]) == 0.0


def test_gini_index_is_zero_for_no_speakers():
    assert gini_index([]) == 0.0


def test_gini_index_rises_with_imbalance():
    balanced = gini_index([5, 5])
    imbalanced = gini_index([9, 1])

    assert imbalanced > balanced


def test_the_assistant_is_excluded_from_participation_stats():
    history = [
        utterance(0, "MULCA", "I am the assistant"),
        utterance(1, "Ada"),
    ]

    variables = compute_trigger_variables(
        history,
        assistant_id=0,
    )

    assert variables["message_count"] == 1
    assert "MULCA" not in variables["participant_message_counts"]


def test_a_silent_participant_is_seeded_at_zero():
    history = [utterance(1, "Ada")]

    variables = compute_trigger_variables(
        history,
        assistant_id=0,
        participants={1: "Ada", 2: "Grace"},
    )

    assert variables["participant_message_counts"]["Grace"] == 0
    assert variables["gini_index"] > 0.0
