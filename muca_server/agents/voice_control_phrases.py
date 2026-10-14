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

# How the transcriber renders MULCA's name: found live, "mulca" itself is
# transcribed unreliably.
_NAME = r"(mulca|muca|malca|molca)"
_ALIAS = r"(assist[ae]nt[ei]?|chatbot)"

# Waking takes a command, not a mention: "<name or alias> start", e.g.
# "mulca start", "assistant, start", "chatbot please start". Found live: a
# single word woke MULCA all the time -- several groups build their own
# MULCAs, assistants and chatbots and say those words in passing. "start"
# exactly, so "the assistant starts by..." doesn't count.
WAKE_PHRASE_PATTERN = re.compile(
    rf"\b({_NAME}|{_ALIAS})\b[\s,.!?:;-]*(please\s+)?start\b",
    re.IGNORECASE,
)


def matches_wake_phrase(transcript: str) -> bool:
    return WAKE_PHRASE_PATTERN.search(transcript) is not None


# Talking *to* MULCA once it is awake: its name anywhere, or an alias as
# the first word. Keeps an engaged session going (see conversation.py's
# ENGAGED_MAX_UNADDRESSED_TURNS) without having to say "start" again.
_ADDRESS_PATTERN = re.compile(
    rf"\b{_NAME}\b|^\W*((hey|hi|ok|okay)\W+)?{_ALIAS}\b",
    re.IGNORECASE,
)


def addresses_assistant(transcript: str) -> bool:
    return _ADDRESS_PATTERN.search(transcript) is not None


# A spoken way out, in either language. Deliberately broad -- broader than
# waking, on purpose: a false stop costs one "mulca start", a false wake
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
