"""LLM-assisted memory extraction.

The LLM may PROPOSE memories based on recent chat. Proposals are surfaced
to admins for approval via slash command `/memory proposals` (Phase 3).
The LLM NEVER writes to the memory table directly.

Phase 2 status: extraction is implemented but proposals are NOT yet wired
into a UI for admins. The function is here so Phase 3 can use it. For now,
memories are created only via explicit admin commands:
  - `/remember <text>`
  - right-click → "Remember this" (context menu)
"""
from __future__ import annotations

import json
from typing import Optional

from app.ai.provider import ProviderError, get_provider
from app.ai.schemas import LLMMessage, LLMRequest
from app.config.settings import get_settings
from app.database.models import Message
from app.memory.models import ExtractedMemoryProposal, MemoryKind
from app.memory.policies import can_use_message_for_memory, looks_sensitive
from app.utils.logging import get_logger

log = get_logger(__name__)


EXTRACTION_PROMPT = """\
You are analyzing a chunk of casual Discord chat to find anything worth remembering
as a long-term structured memory for an AI groupmate bot.

Worth remembering:
- Stated preferences ("we play every Friday")
- Recurring events ("movie night is back on")
- Group lore / inside jokes (only if clearly recurring)
- Decisions ("let's switch to Saturday")

NOT worth remembering:
- Casual chatter
- Temporary opinions
- Anything about specific people that might be sensitive
- Anything that looks like personal info (phone, email, tokens)

For each proposed memory, output JSON with:
  - kind: "fact" | "preference" | "lore" | "event" | "joke"
  - content: short string, paraphrased (not a direct quote)
  - confidence: 0.0 to 1.0 (be honest — jokes ~0.3, clear facts ~0.9)
  - rationale: one short sentence why this is worth keeping

Output format: a JSON array of proposals. Empty array if nothing worth remembering.

Example output:
[
  {
    "kind": "preference",
    "content": "Group plays games every Friday at 8pm",
    "confidence": 0.9,
    "rationale": "Stated explicitly and recurring"
  }
]

Do NOT include any text outside the JSON array.
"""


async def extract_memory_proposals(
    messages: list[Message],
    authors_by_id: dict[int, object] | None = None,
) -> list[ExtractedMemoryProposal]:
    """Ask the LLM to propose memories from a chunk of messages.

    Returns a list of proposals. Caller (Phase 3 admin UI) approves/rejects.

    Privacy: messages from opted-out users are filtered out before sending
    to the LLM. The caller is responsible for passing `authors_by_id` with
    up-to-date opt_out_memory flags.
    """
    if not messages:
        return []

    # Privacy filter
    filtered: list[Message] = []
    for m in messages:
        author = authors_by_id.get(m.author_id) if authors_by_id else None
        if author is not None and not can_use_message_for_memory(m, author):
            continue
        if m.is_bot:
            continue
        filtered.append(m)

    if not filtered:
        return []

    # Build the transcript for the LLM (mask author names — just label as user)
    lines = [f"[user]: {m.content}" for m in filtered]
    transcript = "\n".join(lines)

    request = LLMRequest(
        system_prompt=EXTRACTION_PROMPT,
        messages=[LLMMessage(role="user", content=transcript)],
        max_tokens=400,
        temperature=0.2,
        tools=[],
    )

    try:
        response = await get_provider().generate(request)
    except ProviderError as e:
        log.error("extraction_provider_error", error=str(e))
        return []

    raw = (response.content or "").strip()
    # Find the JSON array — LLMs sometimes wrap in ```json ... ``` blocks
    if "```" in raw:
        # Extract content between the fences
        start = raw.find("```")
        end = raw.rfind("```")
        if start != -1 and end != -1 and end > start:
            inner = raw[start:end]
            # Strip the language tag if present
            inner = inner.split("\n", 1)[-1] if "\n" in inner else inner
            raw = inner.strip()

    # Find the outermost [...]
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end < start:
        log.warning("extraction_no_json_array_found", raw_preview=raw[:200])
        return []

    json_str = raw[start:end + 1]
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as e:
        log.warning("extraction_invalid_json", error=str(e), raw_preview=raw[:200])
        return []

    proposals: list[ExtractedMemoryProposal] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            kind_str = str(item.get("kind", "fact"))
            kind = MemoryKind(kind_str)
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            if looks_sensitive(content):
                log.warning("extraction_skipped_sensitive_proposal", content_preview=content[:80])
                continue
            proposals.append(
                ExtractedMemoryProposal(
                    kind=kind,
                    content=content,
                    confidence=float(item.get("confidence", 0.5)),
                    rationale=str(item.get("rationale", "")),
                )
            )
        except (ValueError, TypeError) as e:
            log.warning("extraction_skip_malformed_proposal", error=str(e))
            continue

    log.info("extraction_completed", proposals=len(proposals), messages=len(filtered))
    return proposals
