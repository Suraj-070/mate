"""Topic tracking — cheap keyword extraction from recent messages.

Per-channel. Pulls the last N messages from the conversation buffer, extracts
the most frequent non-stopword tokens, and returns them as a "topic" list.
Injected into the LLM context as "Active topic: gaming, friday, 8pm".

This is NOT semantic topic modeling — it's a cheap heuristic. Good enough
for a friend-group bot. If we ever need real topic modeling, swap this module
for an LLM-assisted one — the rest of the pipeline doesn't care.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from app.context.conversation_buffer import BufferedMessage, get_buffer
from app.config.settings import get_settings
from app.utils.text import detect_script, Script


# Stopwords — common words we don't want as "topics".
# Bilingual (English + Romanized Nepali) for our default use case.
_STOPWORDS = {
    # English
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "my", "your", "his", "our", "their", "this", "that", "these", "those",
    "to", "of", "in", "on", "at", "for", "with", "by", "from", "as",
    "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "will", "would", "should", "could", "may", "might", "must", "shall",
    "if", "then", "so", "than", "too", "very", "just", "only", "also",
    "not", "no", "yes", "yeah", "yep", "nope", "ok", "okay", "cool",
    "like", "get", "got", "go", "going", "gone", "come", "came", "make",
    "made", "see", "saw", "know", "knew", "think", "thought", "say", "said",
    "what", "when", "where", "why", "how", "who", "which",
    "im", "ive", "dont", "cant", "wont", "thats", "heres", "theres",
    "u", "ur", "r", "ya", "yo", "hey", "hi", "hello", "sup",
    # Romanized Nepali common words
    "ho", "cha", "thiyo", "chha", "khabar", "k", "ke", "kina", "kasari",
    "matra", "ani", "ra", "ya", "va", "ni", "ta", "bhaneko", "bhane",
    " aba", "yesari", "tara", " र ", "अनि",
}


# Token pattern: words 3+ chars long, alphabetic (with Devanagari range)
_TOKEN_RE = re.compile(r"[A-Za-z\u0900-\u097F]{3,}")


def extract_tokens(text: str) -> list[str]:
    """Extract candidate tokens from text.

    Returns lowercased tokens 3+ chars long. Filters stopwords.
    """
    if not text:
        return []
    tokens = _TOKEN_RE.findall(text.lower())
    return [t for t in tokens if t not in _STOPWORDS]


def detect_topic(
    messages: Iterable[BufferedMessage],
    top_n: int | None = None,
) -> list[str]:
    """Extract the top N topic keywords from a list of messages.

    Args:
        messages: iterable of BufferedMessage (typically the channel's buffer)
        top_n: how many keywords to return (default from settings)

    Returns a list of strings, e.g. ["gaming", "friday", "8pm", "tonight"]
    """
    if top_n is None:
        top_n = get_settings().topic_keywords_count

    counter: Counter[str] = Counter()
    for msg in messages:
        if msg.is_bot:
            continue  # don't count bot's own messages
        tokens = extract_tokens(msg.content)
        counter.update(tokens)

    # Return the top N most common, filtering rare ones (count < 2)
    most_common = counter.most_common(top_n * 2)  # over-fetch then filter
    result = [word for word, count in most_common if count >= 1][:top_n]
    return result


def get_channel_topic(discord_channel_id: str) -> list[str]:
    """Convenience: fetch the current topic for a channel from the buffer."""
    buffer = get_buffer()
    msgs = buffer.get(discord_channel_id, limit=10)
    if not msgs:
        return []
    return detect_topic(msgs)


def topic_as_prompt_block(topic: list[str]) -> str | None:
    """Format the topic list as a system-prompt block. Returns None if empty."""
    if not topic:
        return None
    return (
        "═══ ACTIVE TOPIC (recent keywords) ═══\n"
        f"{', '.join(topic)}\n"
        "(Use this as background context. Don't force these words into your reply.)"
    )
