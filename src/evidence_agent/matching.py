"""Claim matching between predicted and expected claims.

The model never sees the expected labels, so its claim text is a paraphrase.
Matching therefore uses character-level similarity instead of exact equality.
Chinese text has no whitespace word boundaries, so we compare character
bigrams rather than tokens.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

_PUNCT = re.compile(r"[\s，。！？；：、\"'（）()【】《》,.!?;:\-—·~`]+")


def normalize(text: str) -> str:
    return _PUNCT.sub("", text).lower()


def bigrams(text: str) -> set[str]:
    normalized = normalize(text)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def similarity(left: str, right: str) -> float:
    """Blend of Jaccard and containment.

    Jaccard alone punishes a short claim that is contained in a longer
    paraphrase, which is common when a model rewrites a claim. Containment at
    0.8 weight recovers those pairs without matching unrelated claims.
    """

    a, b = bigrams(left), bigrams(right)
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if not intersection:
        return 0.0
    jaccard = intersection / len(a | b)
    containment = intersection / min(len(a), len(b))
    return max(jaccard, 0.8 * containment)


def match_claims(
    expected: Sequence[str],
    predicted: Sequence[str],
    threshold: float = 0.3,
) -> tuple[list[tuple[int, int, float]], list[int], list[int]]:
    """Greedy one-to-one matching, highest similarity first.

    Returns (pairs, unmatched_expected_indices, unmatched_predicted_indices)
    where each pair is (expected_index, predicted_index, similarity).
    """

    candidates: list[tuple[float, int, int]] = []
    for e_idx, e_text in enumerate(expected):
        for p_idx, p_text in enumerate(predicted):
            score = similarity(e_text, p_text)
            if score >= threshold:
                candidates.append((score, e_idx, p_idx))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))

    used_expected: set[int] = set()
    used_predicted: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    for score, e_idx, p_idx in candidates:
        if e_idx in used_expected or p_idx in used_predicted:
            continue
        used_expected.add(e_idx)
        used_predicted.add(p_idx)
        pairs.append((e_idx, p_idx, score))

    pairs.sort(key=lambda item: item[0])
    unmatched_expected = [i for i in range(len(expected)) if i not in used_expected]
    unmatched_predicted = [i for i in range(len(predicted)) if i not in used_predicted]
    return pairs, unmatched_expected, unmatched_predicted
