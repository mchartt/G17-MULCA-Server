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


def address_word(transcript: str) -> str:
    """The words that address MULCA, or "" (for the "wake_v2" turn facts)."""

    match = _ADDRESS_PATTERN.search(transcript)
    return match.group().strip() if match else ""


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
#
# The exception: a thanks only counts said to MULCA ("grazie MULCA",
# "thanks, assistant"). Found live (19-25 Oct): a passing "grazie" between
# students ("No grazie", "Mi sento bene, grazie") ended 28 of 54
# dismissed sessions, and 23 of those were woken again within a minute.
_THANKS = r"(thank(s| you)|grazie)"
_TO_MULCA = rf"({_NAME}|{_ALIAS})"
DISMISSAL_PATTERN = re.compile(
    r"\b(that'?s (all|it|enough)|we'?re done|you can (go|leave)|sleep"
    r"|go to bed|end( the| this)? session|stop|quiet|pause|not now"
    r"|shut up|go away|leave us|(good)?bye|shh+|hush|silence|wrap (it|this) up"
    # Up to two words between ("thank you very much MULCA", "grazie
    # mille MULCA").
    rf"|{_THANKS}(\W+\w+){{0,2}}?\W+{_TO_MULCA}|{_TO_MULCA}\W+{_THANKS}"
    r"|basta( così)?|ho finito"
    r"|abbiamo finito|puoi andare|smettila|fermati|(stai )?zitt[oa]|silenzio"
    r"|vai via|lasciaci|non ora|pausa|dormire|fine( della)? sessione"
    # Found live (group 10, 14 Oct): "MULCA turn off", "Ok, power off".
    rf"|(turn|power|switch) (it )?off|shut ?down|spegni(ti)?|{_NAME},? off)\b",
    re.IGNORECASE,
)


def matches_dismissal(transcript: str) -> bool:
    return DISMISSAL_PATTERN.search(transcript) is not None


# "wake" and "wake_v2": "start MULCA" wakes too.
WAKE_ANY_ORDER_PATTERN = re.compile(
    rf"{WAKE_PHRASE_PATTERN.pattern}|\bstart\b[\s,.!?:;-]*({_NAME}|{_ALIAS})\b",
    re.IGNORECASE,
)


def matches_wake_any_order(transcript: str) -> bool:
    return WAKE_ANY_ORDER_PATTERN.search(transcript) is not None


# "wake_v2" only. A dismissal also takes "go out", "get out" and "esci", and
# a hold stops the reply without ending the session. Found in the 15-28 Oct
# exports: "grazie... MULCA start" ended sessions instead of opening them,
# "basta che cerchi" ended one mid-talk, and "aspetta" or "wait" got an
# answer.
_DISMISSAL_V2 = re.compile(
    rf"{DISMISSAL_PATTERN.pattern}|\b(go out|get out|esci)\b",
    re.IGNORECASE,
)
HOLD_PATTERN = re.compile(
    r"\b(wait|hold on|one (sec|second|moment)|let me (think|finish)"
    r"|not (you|talking to you)|don'?t (answer|reply)"
    r"|aspett(a|ate)|un (attimo|momento|secondo)|non (iniziare|cominciare)"
    r"|non rispondere|fammi (pensare|finire)|non parlavo con te|non a te)\b",
    re.IGNORECASE,
)
# A stop or hold buried in a longer sentence is talk between students.
MAX_COMMAND_WORDS = 5
_WORD_V2 = re.compile(r"\w+")


def voice_command_v2(transcript: str) -> str | None:
    """
    "wake", "dismiss", "hold" or None: whichever comes last in the turn
    ("MULCA stop. MULCA start" wakes). A dismissal or hold counts only in a
    short turn or one that names MULCA.
    """

    commands = [(m.start(), "wake") for m in WAKE_ANY_ORDER_PATTERN.finditer(transcript)]
    if (
        len(_WORD_V2.findall(transcript)) <= MAX_COMMAND_WORDS
        or addresses_assistant(transcript)
    ):
        commands += [(m.start(), "dismiss") for m in _DISMISSAL_V2.finditer(transcript)]
        commands += [(m.start(), "hold") for m in HOLD_PATTERN.finditer(transcript)]
    return max(commands, key=lambda command: command[0])[1] if commands else None
