"""Text utilities: language/script detection, token estimation, sanitization.

Lightweight heuristics only — no heavy NLP. The LLM itself handles real
multilingual understanding; we just need cheap signals for routing decisions.
"""
from __future__ import annotations

import re
from enum import Enum


class Script(str, Enum):
    """Detected dominant script of a message."""
    LATIN = "latin"
    DEVANAGARI = "devanagari"
    MIXED = "mixed"
    OTHER = "other"


# Devanagari Unicode block: U+0900–U+097F
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_script(text: str) -> Script:
    """Cheap heuristic: count Latin vs Devanagari chars and pick dominant.

    Returns MIXED if the minority script is >20% of total alpha chars.
    """
    if not text:
        return Script.LATIN

    deva = len(_DEVANAGARI_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    total = deva + latin

    if total == 0:
        return Script.OTHER

    deva_ratio = deva / total
    latin_ratio = latin / total

    if deva_ratio > 0.8:
        return Script.DEVANAGARI
    if latin_ratio > 0.8:
        return Script.LATIN
    if deva_ratio > 0.2 and latin_ratio > 0.2:
        return Script.MIXED
    return Script.DEVANAGARI if deva_ratio > latin_ratio else Script.LATIN


# Very rough token estimate: ~4 chars per token for English, ~1.5 chars/token
# for Devanagari (which tokenizes less efficiently in most BPE vocabs).
# This is only used for budget heuristics — never billing-critical.
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    script = detect_script(text)
    if script == Script.DEVANAGARI:
        return max(1, len(text) // 2)
    if script == Script.MIXED:
        return max(1, len(text) // 3)
    return max(1, len(text) // 4)


# Common AI-ism openers/closers the LLM sometimes slips in despite the prompt.
# We strip these in post-processing for chat hygiene.
_BANNED_AI_PHRASES = [
    "As an AI",
    "As a language model",
    "I'm just an AI",
    "I don't have personal experiences",
    "I cannot fulfill that request",
    "Let me know if you need anything else",
    "Feel free to ask if you have any more questions",
    "I hope this helps",
    "Is there anything else",
]

_BANNED_AI_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(p) for p in _BANNED_AI_PHRASES) + r")"
    r"(?:[^.!?\n]*?)?[\.,]\s*",
    re.IGNORECASE,
)

_TRAILING_CLOSER_RE = re.compile(
    r"\s*(Let me know|Feel free to ask|I hope this helps|Is there anything else)[^.!?\n]*[.!?\n]*\s*$",
    re.IGNORECASE,
)


def strip_ai_isms(text: str) -> str:
    """Remove common assistant-isms the LLM may emit despite instructions.

    Strips the *prefix phrase* (e.g. "As an AI language model, ") but keeps
    the rest of the sentence — we only want to kill the AI-ism opener, not
    delete the entire sentence which often carries useful content.

    For trailing closers like "Let me know if you need anything else!" we
    do strip the whole trailing clause since it adds no content.
    """
    # Strip leading AI-ism prefix (just the phrase + optional continuation up to comma/period)
    prev = None
    while prev != text:
        prev = text
        text = _BANNED_AI_RE.sub("", text, count=1)
    # Strip trailing "Let me know..." style closers
    text = _TRAILING_CLOSER_RE.sub("", text)
    return text.strip()


def truncate_for_discord(text: str, max_chars: int = 2000) -> list[str]:
    """Split a long response into chunks Discord can accept (<=2000 chars).

    Tries to break on sentence boundaries, then on newlines, then on words.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        # Try sentence boundary first
        cut = remaining.rfind(". ", 0, max_chars)
        if cut == -1 or cut < max_chars // 2:
            # Try newline
            cut = remaining.rfind("\n", 0, max_chars)
        if cut == -1 or cut < max_chars // 2:
            # Try space
            cut = remaining.rfind(" ", 0, max_chars)
        if cut == -1 or cut < max_chars // 4:
            # Hard cut
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
