"""Retention + privacy policies for the memory subsystem.

Single source of truth for "what should we keep, what should we drop, and
what should we never expose". The repository + summarizer + extraction
modules all import rules from here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database.models import Memory, Message, User


# ── Retention ──────────────────────────────────────────────────
DEFAULT_FACT_EXPIRY_DAYS: Optional[int] = None       # facts never expire
DEFAULT_PREFERENCE_EXPIRY_DAYS: Optional[int] = 365   # preferences: 1 year
DEFAULT_LORE_EXPIRY_DAYS: Optional[int] = None        # lore: forever
DEFAULT_EVENT_EXPIRY_DAYS: Optional[int] = 30         # events: 30 days
DEFAULT_JOKE_EXPIRY_DAYS: Optional[int] = 180         # jokes: 6 months


def default_expiry_for_kind(kind: str) -> Optional[datetime]:
    """Return default expiry datetime for a memory kind, or None for no expiry."""
    days_map = {
        "fact": DEFAULT_FACT_EXPIRY_DAYS,
        "preference": DEFAULT_PREFERENCE_EXPIRY_DAYS,
        "lore": DEFAULT_LORE_EXPIRY_DAYS,
        "event": DEFAULT_EVENT_EXPIRY_DAYS,
        "joke": DEFAULT_JOKE_EXPIRY_DAYS,
    }
    days = days_map.get(kind)
    if days is None:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


# ── Privacy ───────────────────────────────────────────────────
def can_use_message_for_style(message: Message, author: User) -> bool:
    """Privacy check: can this message be used as a style sample?

    Returns False if:
    - The author has opted out of style learning
    - The message is from the bot itself
    - The message is empty
    """
    if author.opt_out_style:
        return False
    if message.is_bot:
        return False
    if not message.content or not message.content.strip():
        return False
    return True


def can_use_message_for_memory(message: Message, author: User) -> bool:
    """Privacy check: can this message's content be proposed as a memory?

    Returns False if:
    - The author has opted out of memory
    - The message is from the bot itself
    """
    if author.opt_out_memory:
        return False
    if message.is_bot:
        return False
    return True


# ── Confidence defaults ──────────────────────────────────────
DEFAULT_CONFIDENCE = {
    "fact": 1.0,
    "preference": 0.8,
    "lore": 0.6,
    "event": 0.9,
    "joke": 0.3,
}


def default_confidence_for_kind(kind: str) -> float:
    return DEFAULT_CONFIDENCE.get(kind, 0.5)


# ── Sanitization ──────────────────────────────────────────────
# Patterns that should never be auto-extracted from chat into memory.
# Admins can still `/remember` them explicitly if they choose.
SENSITIVE_PATTERNS = [
    # Phone numbers (very loose)
    r"\+?\d[\d\s\-]{8,}\d",
    # Email
    r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b",
    # URLs with credentials
    r"://[^@\s]+@",
    # Discord tokens (very loose — 50+ alphanumeric chars)
    r"\b[A-Za-z0-9_-]{50,}\b",
]


def looks_sensitive(text: str) -> bool:
    """Cheap heuristic: does this text contain anything that looks like PII?

    Used to refuse LLM-proposed memories that might leak sensitive info.
    Admins can override with explicit `/remember`.
    """
    import re
    for pat in SENSITIVE_PATTERNS:
        if re.search(pat, text):
            return True
    return False
