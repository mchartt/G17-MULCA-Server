"""
The wake/dismissal phrase patterns, shared between conversation.py (which
matches on them) and openai/main_agent.py (which must exempt a dismissal
from its own low-confidence transcript filter before conversation.py ever
sees the text). Split out specifically to avoid a circular import: conversation.py
already imports from main_agent.py, so main_agent.py can't import these
back from conversation.py.
"""

from __future__ import annotations

import re

WAKE_PHRASE_PATTERN = re.compile(
    r"\b(assist[ae]nt\w*|chatbot\w*|bot|mulca|muca)\b",
    re.IGNORECASE,
)

# MULCA's own name counts anywhere in a sentence. The generic aliases only
# count as the first word (after an optional "hey"): found live, students
# building their own assistants/chatbots say them in passing all the time
# ("our assistant should show the deadline"), and every one woke MULCA.
# Still no *required* lead-in -- the transcriber drops a quiet "hey", and
# a bare "Assistant?" must keep working.
_NAME_PATTERN = re.compile(r"\b(mulca|muca)\b", re.IGNORECASE)
_LEADING_ALIAS_PATTERN = re.compile(
    r"^\W*((hey|hi|hej|hei|ehi|ok|okay|hello)\W+)?"
    r"(assist[ae]nt\w*|chatbot\w*|bot)\b",
    re.IGNORECASE,
)


def matches_wake_phrase(transcript: str) -> bool:
    return (
        _NAME_PATTERN.search(transcript) is not None
        or _LEADING_ALIAS_PATTERN.search(transcript) is not None
    )


# A spoken way out, in either language. Deliberately broad -- broader than
# waking, on purpose: a false stop costs one "hey mulca", a false wake
# costs a group being talked over. A plain substring search, no length
# ceiling, no "did MULCA just speak" context check. Found live: a word-count ceiling meant a
# versatile, longer way of saying goodbye ("go ahead and wrap it up,
# thanks for the help") never matched at all, while the wake side had no
# such limit. The known cost of dropping it: "thanks Ada, now let's move
# on to the budget" now also ends an engaged session, same as "mulca,
# that bot answer was great" would already falsely open one -- accepted
# for both, not just one.
DISMISSAL_PATTERN = re.compile(
    r"\b(that'?s (all|it|enough)|we'?re done|you can (go|leave)|sleep"
    r"|go to bed|end( the| this)? session|stop|quiet|pause|not now"
    r"|shut up|go away|leave us|(good)?bye|shh+|hush|silence"
    r"|thank(s| you)|grazie|basta( così)?|ho finito"
    r"|abbiamo finito|puoi andare|smettila|fermati|(stai )?zitt[oa]|silenzio"
    r"|vai via|lasciaci|non ora|pausa|dormire|fine( della)? sessione)\b",
    re.IGNORECASE,
)


def matches_dismissal(transcript: str) -> bool:
    return DISMISSAL_PATTERN.search(transcript) is not None
