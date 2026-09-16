"""
The narrow list of cases that skip the observers' judgement entirely.

Plain code, no model call. Deliberately a short explicit list rather than
heuristics: anything needing judgement should go to an observer instead.

`@mainllm` is a debug rule — a guaranteed way to make the main agent answer
without depending on an observer's judgement, so the reply path can be
verified on its own. Real @-mentions are deliberately *not* here: bypassing
the observers also bypasses their judgement about whether answering needs
something looked up, which made mentioned questions get worse answers than
the same question asked without the mention.
"""

from __future__ import annotations

from .ports import Utterance

DEBUG_MENTION = "@mainllm"


def check_forced_intervention(
    utterance: Utterance,
) -> str | None:
    """A directive when a rule matches, otherwise None."""

    if DEBUG_MENTION in utterance.content.lower():
        return (
            f'You were directly addressed by {utterance.label()}: '
            f'"{utterance.content}". Respond to them directly.'
        )

    return None
