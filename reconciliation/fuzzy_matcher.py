"""
Lightweight fuzzy matching using Python's stdlib difflib.

Kept dependency-free on purpose: rapidfuzz/fuzzywuzzy are not required for
this scale of matching and would add an install step for no real accuracy
gain here. difflib.SequenceMatcher's ratio() is sufficient for merchant-name
formatting noise (legal suffixes, casing, punctuation).
"""

from difflib import SequenceMatcher


def similarity(a: str, b: str) -> float:
    """Return a 0-1 similarity ratio between two strings."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def is_likely_same_entity(name_a: str, name_b: str, threshold: float = 0.85) -> bool:
    return similarity(name_a, name_b) >= threshold
