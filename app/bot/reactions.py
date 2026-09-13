"""Reaction picker for the REACT outcome.

When the bot decides to "acknowledge but not reply", it picks an emoji
via cheap keyword heuristics. No LLM call — reactions should be cheap.

Phase 3+ may add LLM-based reaction picking if heuristics prove insufficient.
"""
from __future__ import annotations

import re


# Keyword → emoji mappings. Lowercase substring match.
# Keep these SHORT — emojis take visual space.
_KEYWORD_EMOJI_MAP: list[tuple[str, str]] = [
    # Positive
    ("good news", "👍"),
    ("love", "❤️"),
    ("agreed", "👍"),
    ("nice", "👌"),
    ("sweet", "👌"),
    ("awesome", "🔥"),
    ("hell yeah", "🔥"),
    ("hyped", "🔥"),
    ("yay", "🎉"),
    ("congrats", "🎉"),
    ("finally", "🎉"),

    # Negative / sympathetic
    ("ugh", "😅"),
    ("rough", "😅"),
    ("long day", "😅"),
    ("tired", "😴"),
    ("exhausted", "😴"),
    ("sad", "💛"),
    ("rough day", "💛"),
    ("rip", "💀"),
    ("oof", "😅"),
    ("ouch", "😅"),
    ("bad news", "💛"),

    # Confused / curious
    ("confused", "🤔"),
    ("wait what", "🤔"),
    ("huh", "🤔"),
    ("really?", "🤔"),

    # Gaming / activity
    ("game", "🎮"),
    ("playing", "🎮"),
    ("won", "🏆"),
    ("lost", "💀"),

    # Food
    ("hungry", "🍔"),
    ("food", "🍽️"),
    ("eating", "🍽️"),

    # Sleep / night
    ("good night", "🌙"),
    ("gn", "🌙"),
    ("sleep", "😴"),
    ("bed", "🛏️"),

    # Misc acknowledgements
    ("lol", "😂"),
    ("lmao", "😂"),
    ("haha", "😂"),
    ("funny", "😂"),
    ("true", "💯"),
    ("facts", "💯"),
    ("real", "💯"),
    ("mood", "💯"),
]


# Default acknowledgement emojis — used when no keyword matches.
_DEFAULT_ACK_EMOJIS = ["👍", "👌", "🫡"]


def pick_reaction(text: str) -> str:
    """Pick a single emoji reaction for the given message text.

    Heuristic:
      1. Lowercase the text.
      2. Find the first keyword match.
      3. If no match, return a default acknowledgement emoji.

    Returns the emoji string.
    """
    if not text:
        return _DEFAULT_ACK_EMOJIS[0]

    lower = text.lower()
    for keyword, emoji in _KEYWORD_EMOJI_MAP:
        # Word-boundary-ish match: keyword as a substring is fine for our
        # short casual messages. We don't need perfect tokenization.
        if keyword in lower:
            return emoji

    return _DEFAULT_ACK_EMOJIS[0]
