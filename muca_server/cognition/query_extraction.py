"""
Pulling the actual topic out of a "remember"-triggering message.

Plain code, no model call -- same reasoning as forced_intervention.py: a
mechanical step like this doesn't need a model's judgement, and a
deterministic extractor is trivially testable where an LLM's exact
phrasing would not be. Embedding the whole triggering message (including
"please tell me something about") would dilute similarity search with
boilerplate words the rest of the conversation never uses.
"""

from __future__ import annotations

import re

# Ordered most-specific-first: the first one that matches wins. Each has
# exactly one capture group, the topic itself. Not an exhaustive parser of
# every possible phrasing -- a narrow, extendable heuristic, with the whole
# (address-stripped) message as the fallback when nothing matches.
_WRAPPER_PATTERNS = [
    re.compile(r"tell me (?:something|anything) about (.+)", re.IGNORECASE),
    re.compile(r"tell me about (.+)", re.IGNORECASE),
    re.compile(r"what do you (?:know|remember) about (.+)", re.IGNORECASE),
    re.compile(r"do you (?:know|remember)(?: anything)? about (.+)", re.IGNORECASE),
    re.compile(r"what did (?:we|i|you) (?:say|decide) about (.+)", re.IGNORECASE),
    re.compile(r"remind me about (.+)", re.IGNORECASE),
]

# A leading direct address ("MULCA, ...", "@MULCA ...") isn't part of the
# topic -- stripped first so none of the patterns above need to account
# for it themselves.
_ADDRESS_PREFIX = re.compile(r"^\s*@?mulca[,:]?\s*", re.IGNORECASE)

_TRAILING_PUNCTUATION = re.compile(r"[\s.?!]+$")


def extract_query(text: str) -> str:
    """
    The topic to search for, given the triggering message's raw text.

    Falls back to the whole (address-stripped) message if none of the
    known wrapper phrasings match -- better to search on slightly too
    much text than to return nothing and search on an empty string.
    """

    stripped = _ADDRESS_PREFIX.sub("", text).strip()

    for pattern in _WRAPPER_PATTERNS:
        match = pattern.search(stripped)
        if match:
            return _TRAILING_PUNCTUATION.sub("", match.group(1)).strip()

    return _TRAILING_PUNCTUATION.sub("", stripped).strip()
