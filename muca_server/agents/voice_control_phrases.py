"""
The wake/dismissal phrase patterns, shared between conversation.py (which
matches on them) and openai/main_agent.py (which must exempt them from its
own low-confidence transcript filter before conversation.py ever sees the
text). Split out specifically to avoid a circular import: conversation.py
already imports from main_agent.py, so main_agent.py can't import these
back from conversation.py.
"""

from __future__ import annotations

import re

WAKE_PHRASE_PATTERN = re.compile(
    r"\b(assist\w*|chatbot\w*|bot|mulca|muca)\b",
    re.IGNORECASE,
)


def matches_wake_phrase(transcript: str) -> bool:
    return WAKE_PHRASE_PATTERN.search(transcript) is not None


# A spoken way out, in either language. Deliberately as flexible as
# WAKE_PHRASE_PATTERN above, same reasoning: a plain substring search, no
# length ceiling, no "did MULCA just speak" context check -- wake already
# accepts that trade-off (any sentence naming MULCA opens a window,
# regardless of length or what else is in it), so dismissal accepts the
# mirror-image one for closing. Found live: a word-count ceiling meant a
# versatile, longer way of saying goodbye ("go ahead and wrap it up,
# thanks for the help") never matched at all, while the wake side had no
# such limit. The known cost of dropping it: "thanks Ada, now let's move
# on to the budget" now also ends an engaged session, same as "mulca,
# that bot answer was great" would already falsely open one -- accepted
# for both, not just one.
DISMISSAL_PATTERN = re.compile(
    r"\b(that'?s (all|it|enough)|we'?re done|you can go|sleep"
    r"|end( the| this)? session|stop|be quiet"
    r"|shut up|go away|thank(s| you)|grazie|basta( così)?|ho finito"
    r"|abbiamo finito|puoi andare|smettila|fermati|(stai )?zitt[oa]|silenzio"
    r"|dormire|fine( della)? sessione)\b",
    re.IGNORECASE,
)


def matches_dismissal(transcript: str) -> bool:
    return DISMISSAL_PATTERN.search(transcript) is not None


def matches_voice_control_phrase(transcript: str) -> bool:
    """
    Either pattern -- the one check openai/main_agent.py needs before
    discarding a low-confidence transcript: a real wake or dismissal
    attempt must never be silently dropped just for sounding unsure of
    itself, since both tend to be short, clipped utterances (exactly what
    scores low confidence) and losing either leaves someone unable to
    start or stop an engaged session by voice at all.
    """

    return matches_wake_phrase(transcript) or matches_dismissal(transcript)
