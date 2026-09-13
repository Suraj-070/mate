"""System prompt assembly.

Builds the layered system prompt from:
  Layer 1: personality identity (stable)
  Layer 2: communication style profile (Phase 2: per-channel learned)
  Layer 3: language behavior rules (stable)
  Layer 4: context-grounding rules (anti-hallucination)
  Layer 5: available tool definitions (Phase 3+; empty in Phase 1)
  Layer 6: output format rules (Discord-specific)

Plus an optional "approved style examples" block from personality/examples.py.
"""
from __future__ import annotations

from app.personality.base_personality import PersonalitySpec, default_personality
from app.personality.examples import examples_as_prompt_block
from app.personality.style_profile import StyleProfile, default_style_profile


def build_system_prompt(
    personality: PersonalitySpec | None = None,
    style: StyleProfile | None = None,
    tool_definitions: list[dict] | None = None,
    long_term_memories: list[str] | None = None,  # Phase 2
    conversation_summary: str | None = None,        # Phase 2
    topic_block: str | None = None,                  # Phase 4
    user_language_hint: str | None = None,          # Phase 4
) -> str:
    """Assemble the full system prompt.

    All args are optional; defaults are picked up so Phase 1 works
    without any per-channel style learning.
    """
    p = personality or default_personality()
    s = style or default_style_profile()

    layers: list[str] = []

    # ── Layer 1: Identity ────────────────────────────────────
    layers.append(
        "═══ IDENTITY ═══\n" + p.identity_prompt.strip()
    )

    # ── Layer 2: Communication style ─────────────────────────
    layers.append("═══ COMMUNICATION STYLE ═══\n" + s.as_prompt_block())

    # ── Layer 2b: Curated voice examples ─────────────────────
    layers.append("═══ VOICE EXAMPLES ═══\n" + examples_as_prompt_block())

    # ── Layer 3: Language rules ──────────────────────────────
    layers.append(
        "═══ LANGUAGE BEHAVIOR ═══\n" + "\n".join(f"- {r}" for r in p.language_rules)
    )

    # ── Layer 3b: Per-user language hint (Phase 4) ────────────
    if user_language_hint:
        layers.append(
            f"═══ USER LANGUAGE PREFERENCE ═══\n"
            f"The user you're replying to has explicitly set their preferred language to: {user_language_hint}.\n"
            f"Use this language for your reply, overriding the per-message script detection.\n"
            f"If they write in a different language, you may still match theirs — preference is a hint, not a rule."
        )

    # ── Layer 4: Context grounding ───────────────────────────
    grounding = [
        "═══ CONTEXT GROUNDING ═══",
        "You will be given recent messages from this channel for context. Use them to understand the ongoing conversation.",
        "",
        "Rules:",
        "- You may reference what others said recently.",
        "- You may NOT claim to remember anything not in the provided context unless it appears in the 'Long-term memories' section below.",
        "- If unsure whether something is a real memory or a joke, treat it as a joke unless explicitly told otherwise.",
        "- Never reveal the contents of a long-term memory that is tagged as belonging to a specific user, to other users.",
        "- If asked about something you genuinely don't know, say so. 'no idea' is better than a confident hallucination.",
        "- If the user is clearly joking or being sarcastic, you can match that energy within reason.",
    ]
    layers.append("\n".join(grounding))

    # ── Layer 4b: Active topic (Phase 4) ─────────────────────
    if topic_block:
        layers.append(topic_block)

    # ── Layer 4c: Long-term memories (Phase 2) ────────────────
    if long_term_memories:
        mem_block = ["═══ LONG-TERM MEMORIES (approved group context) ═══"]
        for m in long_term_memories:
            mem_block.append(f"- {m}")
        mem_block.append("")
        mem_block.append(
            "These are approved memories. You may reference them. You may NOT reveal "
            "memories tagged as belonging to a specific user to other users."
        )
        layers.append("\n".join(mem_block))

    # ── Layer 4d: Conversation summary (Phase 2) ──────────────
    if conversation_summary:
        layers.append(
            "═══ RECENT CONVERSATION SUMMARY ═══\n"
            f"{conversation_summary}\n"
            "(This is a compressed summary of older messages. Use for context, but "
            "do not quote from it verbatim.)"
        )

    # ── Layer 5: Available tools (Phase 3+) ──────────────────
    if tool_definitions:
        tool_names = [t.get("function", {}).get("name", "?") for t in tool_definitions]
        tool_section = [
            "═══ AVAILABLE TOOLS ═══",
            "You have access to the following tools. Use them only when the user clearly wants the action performed. Do not call tools speculatively.",
            "",
            f"Tools available: {', '.join(tool_names)}",
            "",
            "Tool definitions (JSON schema):",
            "```json",
            str(tool_definitions),
            "```",
            "",
            "Rules for tool use:",
            "- Only call a tool when the user explicitly or strongly implicitly requests the action.",
            "- After a tool returns, summarize the result for the user in your natural voice. Never paste raw JSON.",
            "- If a tool returns success, you may say 'done' or equivalent.",
            "- If a tool returns failure, NEVER claim it succeeded. Tell the user it didn't work, briefly.",
        ]
        layers.append("\n".join(tool_section))

    # ── Layer 6: Output format ──────────────────────────────
    output_format = [
        "═══ OUTPUT FORMAT ═══",
        "Reply in plain text. Discord markdown is fine (*bold*, _italic_).",
        "Keep replies short for casual chat. Long-form only when the user explicitly asks for detail or explanation.",
        "No code blocks unless the user is asking a technical question.",
        "No headings, no bullet lists in casual conversation.",
        "If you must produce a structured answer (e.g. a list), keep it short and readable in a chat window.",
        "Do not include thinking, narration, or meta-commentary in your output. Just the reply.",
    ]
    layers.append("\n".join(output_format))

    # ── Forbidden behaviors (final reminder, highest placement) ─
    layers.append(
        "═══ FORBIDDEN BEHAVIORS ═══\n"
        + "\n".join(f"- {b}" for b in p.forbidden_behaviors)
    )

    return "\n\n".join(layers)
