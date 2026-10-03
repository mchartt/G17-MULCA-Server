"""
Instructions for the session runtime itself, put on the outbound queue
beside the protocol messages but never sent to the client as JSON.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EndSession:
    """
    Close this session's websocket with this code and reason.

    The client sees an ordinary close and reopens: a fresh session, with
    fresh provider connections and the memory the old one saved. 1012
    ("service restart") says exactly that.
    """

    code: int = 1012
    reason: str = "Provider session ended"
