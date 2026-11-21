"""Backward-compatible AI facade for the pipeline (delegates to ``ai_client``).

Historically every AI call went to Claude Haiku via ``claude -p`` or the
Anthropic API. ``ai_client.py`` generalizes this so remediation (``primary``)
and evaluations (``eval``: content review + alt-text sanity check) can each use
any configured provider/model (Claude CLI, Anthropic API, OpenAI / compatible /
Ollama / OpenRouter, or Google Gemini). This module preserves the original
imports and monkey-patches pdf-a11y's fallback VLM hook.
"""
from __future__ import annotations

import re

from pdf_a11y.fixers import images as a11y_images

import ai_client
from ai_client import (
    ALT_MAX,
    DEFAULT_CLAUDE_MODEL as HAIKU,
    ask,
    ask_eval,
    ask_primary as ask_haiku,
    backend,
    configure,
    describe_role,
    eval_is_separate,
    fit_alt,
    image_block,
    preflight,
    resolve_model,
    resolve_provider,
    role_signature,
    usage_totals,
)

__all__ = [
    "ALT_MAX",
    "ALT_SYSTEM",
    "HAIKU",
    "alt_context",
    "ask",
    "ask_eval",
    "ask_haiku",
    "backend",
    "configure",
    "describe_role",
    "eval_is_separate",
    "fit_alt",
    "image_block",
    "preflight",
    "resolve_model",
    "resolve_provider",
    "role_signature",
    "usage_totals",
]

# ---------------------------------------------------------------------------
# pdf-a11y's own alt-text call, kept as a fallback for any image the image stage
# didn't cover. The stock version reads msg.content[0].text, which breaks on
# models that emit thinking blocks, sends oversized scans, and cannot use a
# Claude subscription or non-Anthropic providers.
# ---------------------------------------------------------------------------
alt_context = {"title": "", "calls": 0}
ALT_SYSTEM = "You write alt text for images in public documents, for screen-reader users."


def _vlm_anthropic(self, img_bytes, prompt, cfg, max_len):
    alt_context["calls"] += 1
    context = (
        f'This image appears in "{alt_context["title"]}", a public document. '
        "If it is a drawing sheet, say what the sheet shows (sheet type, area, key callouts) "
        f"rather than describing lines and shapes. One sentence, at most {ALT_MAX} characters. "
        "Reply with the alt text only. "
    )
    text = ai_client.ask_primary(
        ALT_SYSTEM,
        [image_block(img_bytes), {"type": "text", "text": context + prompt}],
        effort="low",
    )
    if not text:
        return None
    if text.strip() == "DECORATIVE":
        return False
    return fit_alt(re.sub(r"^(alt text:\s*)", "", text, flags=re.I), min(max_len, ALT_MAX))


a11y_images.ImageAltTextFixer._vlm_anthropic = _vlm_anthropic
