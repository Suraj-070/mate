"""Style profile schema + DB-backed loading.

Phase 1: returned hardcoded defaults.
Phase 2: loads from DB per-channel when available, falls back to defaults.
Phase 2: rebuild is triggered when new approved samples are added.

The style profile is the DISTILLED form of approved samples — never raw
message content. The LLM only ever sees the structured fields + a few
distilled example strings chosen to anonymize authorship.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Optional

from app.database import repository as repo
from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class StyleProfile:
    """Structured observations about group communication style."""

    formality: Literal["very_casual", "casual", "neutral", "formal"] = "very_casual"
    average_response_length: Literal["very_short", "short", "medium", "long"] = "short"
    humor_level: Literal["none", "low", "moderate", "high"] = "moderate"
    emoji_usage: Literal["none", "rare", "occasional", "frequent"] = "occasional"
    language_behavior: Literal["english_only", "nepali_only", "mixed"] = "mixed"
    common_expressions: list[str] = field(default_factory=list)
    style_examples: list[str] = field(default_factory=list)

    def as_prompt_block(self) -> str:
        """Render the profile as a compact system-prompt block."""
        lines = [
            "Communication norms for this group (derived from approved style samples):",
            f"- formality: {self.formality}",
            f"- average reply length: {self.average_response_length}",
            f"- humor: {self.humor_level}",
            f"- emoji usage: {self.emoji_usage}",
            f"- language behavior: {self.language_behavior}",
        ]
        if self.common_expressions:
            lines.append(f"- common expressions: {', '.join(self.common_expressions[:8])}")
        if self.style_examples:
            lines.append("- representative example tone (do NOT copy verbatim):")
            for ex in self.style_examples[:5]:
                lines.append(f"    · {ex}")
        return "\n".join(lines)

    def to_json(self) -> str:
        """Serialize to JSON for DB storage."""
        return json.dumps(
            {
                "formality": self.formality,
                "average_response_length": self.average_response_length,
                "humor_level": self.humor_level,
                "emoji_usage": self.emoji_usage,
                "language_behavior": self.language_behavior,
                "common_expressions": self.common_expressions,
                "style_examples": self.style_examples,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, json_str: str) -> "StyleProfile":
        """Deserialize from DB JSON. Falls back to defaults on parse error."""
        try:
            data = json.loads(json_str)
            return cls(
                formality=data.get("formality", "very_casual"),
                average_response_length=data.get("average_response_length", "short"),
                humor_level=data.get("humor_level", "moderate"),
                emoji_usage=data.get("emoji_usage", "occasional"),
                language_behavior=data.get("language_behavior", "mixed"),
                common_expressions=data.get("common_expressions", []),
                style_examples=data.get("style_examples", []),
            )
        except (json.JSONDecodeError, TypeError) as e:
            log.warning("style_profile_parse_failed", error=str(e))
            return default_style_profile()


def default_style_profile() -> StyleProfile:
    """Phase 1 default. Used as fallback when no DB profile exists yet."""
    return StyleProfile(
        formality="very_casual",
        average_response_length="short",
        humor_level="moderate",
        emoji_usage="occasional",
        language_behavior="mixed",
        common_expressions=[],
        style_examples=[],
    )


# ── DB-backed loader ───────────────────────────────────────────
async def load_style_profile_for_channel(channel_db_id: int) -> StyleProfile:
    """Load the per-channel style profile from DB.

    Returns the default profile if:
    - Style learning is disabled in settings
    - No profile row exists yet for this channel
    - The stored JSON is corrupt
    """
    from app.config.settings import get_settings
    settings = get_settings()
    if not settings.enable_style_learning:
        return default_style_profile()

    profile_row = await repo.get_style_profile(channel_db_id)
    if profile_row is None or not profile_row.profile_json:
        return default_style_profile()

    return StyleProfile.from_json(profile_row.profile_json)


# ── Rebuild logic ──────────────────────────────────────────────
REBUILD_PROMPT = """\
You are analyzing a set of approved message samples from a friend group's Discord chat
to extract a structured communication-style profile.

Output JSON with these fields:
{
  "formality": "very_casual" | "casual" | "neutral" | "formal",
  "average_response_length": "very_short" | "short" | "medium" | "long",
  "humor_level": "none" | "low" | "moderate" | "high",
  "emoji_usage": "none" | "rare" | "occasional" | "frequent",
  "language_behavior": "english_only" | "nepali_only" | "mixed",
  "common_expressions": [up to 8 short phrases that appear frequently],
  "style_examples": [up to 5 short PARAPHRASED examples showing the tone — do NOT copy verbatim]
}

Rules:
- Be honest about the group's actual style, not aspirational.
- For style_examples, PARAPHRASE — never copy a real user's exact words.
- If a sample is in mixed language, set language_behavior to "mixed".
- Output ONLY the JSON. No commentary.
"""


async def rebuild_style_profile(
    channel_db_id: int,
    force: bool = False,
) -> Optional[StyleProfile]:
    """Rebuild the style profile for a channel from approved samples.

    Triggered automatically when enough new samples are added (see
    style_profile_rebuild_threshold). Can also be called manually via
    `/style rebuild`.

    Args:
        channel_db_id: The DB ID of the channel.
        force: If True, rebuild even if the threshold isn't met.

    Returns the new StyleProfile, or None if rebuild was skipped.
    """
    from app.ai.provider import ProviderError, get_provider
    from app.ai.schemas import LLMMessage, LLMRequest
    from app.config.settings import get_settings
    settings = get_settings()

    if not settings.enable_style_learning:
        return None

    unused_count = await repo.count_unused_style_samples(channel_db_id)
    if not force and unused_count < settings.style_profile_rebuild_threshold:
        return None

    # Fetch sample contents (preferring unused ones)
    sample_contents = await repo.fetch_style_sample_contents(
        channel_db_id, limit=30
    )
    if not sample_contents:
        return None

    # Find the highest sample ID we're including, to mark them as used
    samples_with_ids = await repo.list_style_samples(channel_db_id, limit=30)
    if not samples_with_ids:
        return None
    highest_sample_id = max(sample.id for sample, _ in samples_with_ids)

    transcript = "\n".join(f"- {c}" for c in sample_contents)
    request = LLMRequest(
        system_prompt=REBUILD_PROMPT,
        messages=[LLMMessage(role="user", content=transcript)],
        max_tokens=500,
        temperature=0.3,
        tools=[],
    )

    try:
        response = await get_provider().generate(request)
    except ProviderError as e:
        log.error("style_rebuild_provider_error", error=str(e), channel_db_id=channel_db_id)
        return None

    raw = (response.content or "").strip()
    # Strip ```json fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines)

    try:
        new_profile = StyleProfile.from_json(raw)
    except Exception as e:
        log.warning("style_rebuild_parse_failed", error=str(e), raw_preview=raw[:200])
        return None

    # Persist
    await repo.upsert_style_profile(
        channel_db_id=channel_db_id,
        profile_json=new_profile.to_json(),
        sample_count=len(samples_with_ids),
        last_sample_id=highest_sample_id,
    )
    # Mark samples as used
    await repo.mark_style_samples_used(channel_db_id, highest_sample_id)

    log.info(
        "style_profile_rebuilt",
        channel_db_id=channel_db_id,
        sample_count=len(samples_with_ids),
    )

    # Maybe trigger rebuild for next batch — fire and forget
    try:
        await rebuild_style_profile(channel_db_id, force=False)
    except Exception:
        pass

    return new_profile
