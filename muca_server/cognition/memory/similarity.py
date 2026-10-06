"""
Cosine similarity ranking, shared by the in-memory and SQLite episodic
stores' search_similar.

Plain numpy, no vector-DB dependency -- brute force is fast enough at this
project's actual scale: one team's whole conversation log over a multi-week
study is at most a few hundred to roughly a thousand rows, not the millions
a vector DB earns its keep on.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def rank_by_similarity(
    query: list[float],
    candidates: Sequence[np.ndarray] | Sequence[list[float]],
) -> list[int]:
    """
    Indices into `candidates`, most similar to `query` first.

    Returns [] for no candidates, so callers never need a separate guard
    for an empty conversation.

    Pass already-numpy candidates (e.g. straight from np.frombuffer)
    rather than Python lists where the caller has them -- converting
    thousands of individual Python floats one at a time (what
    np.asarray does for nested lists) measured ~25x slower than
    np.stack'ing arrays that are already numpy, at a size well above
    this project's actual per-conversation scale.
    """

    if not candidates:
        return []

    query_vector = np.asarray(query, dtype=np.float32)
    matrix = np.stack(candidates).astype(np.float32, copy=False)

    query_norm = np.linalg.norm(query_vector)
    matrix_norms = np.linalg.norm(matrix, axis=1)

    # A zero vector (e.g. an embedding call that somehow returned all
    # zeros) has no defined direction to compare -- treat it as similar
    # to nothing, rather than dividing by zero or ranking it as a
    # perfect match.
    safe_matrix_norms = np.where(matrix_norms == 0, 1.0, matrix_norms)
    safe_query_norm = query_norm if query_norm != 0 else 1.0

    scores = (matrix @ query_vector) / (safe_matrix_norms * safe_query_norm)
    scores = np.where(matrix_norms == 0, -np.inf, scores)

    return [int(i) for i in np.argsort(scores)[::-1]]
