"""
What the conversation currently looks like, as numbers.

Plain code: nothing here calls a model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from .ports import Utterance


def gini_index(counts: Sequence[int]) -> float:
    """
    How unevenly people are participating.

    0 is everyone speaking equally, close to 1 is one person dominating.
    """

    if not counts:
        return 0.0

    ordered = sorted(counts)
    n = len(ordered)
    if n == 1:
        return 0.0

    total = sum(ordered)
    if total == 0:
        return 0.0

    cumulative = sum(
        (i + 1) * count for i, count in enumerate(ordered)
    )

    return (2 * cumulative) / (n * total) - (n + 1) / n


def compute_trigger_variables(
    history: Sequence[Utterance],
    *,
    assistant_id: int,
    participants: dict[int, str] | None = None,
) -> dict:
    """
    Summarize participation for the observers to judge against.

    The assistant's own turns are excluded: it is not a participant to
    balance against, and counting it would hide who has gone quiet.

    `participants` lets somebody who is present but has not spoken yet show
    up with a count of zero. Without it the imbalance reads as zero simply
    because the quiet person has not said anything, which is backwards.

    Counts are keyed by display name so the observers can refer to people the
    same way they appear in the transcript. Two people sharing a display name
    are conflated, which is a known limit.
    """

    spoken = [
        utterance
        for utterance in history
        if utterance.speaker_id != assistant_id
    ]
    counts = Counter(utterance.label() for utterance in spoken)

    spoken_ids = {utterance.speaker_id for utterance in spoken}
    for speaker_id, name in (participants or {}).items():
        if speaker_id != assistant_id and speaker_id not in spoken_ids:
            counts.setdefault(name, 0)

    total = sum(counts.values())
    shares = {
        name: (count / total if total else 0.0)
        for name, count in counts.items()
    }

    return {
        "gini_index": gini_index(list(counts.values())),
        "message_count": len(spoken),
        "participant_message_counts": dict(counts),
        "participant_shares": shares,
    }
