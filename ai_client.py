"""Multi-provider multimodal AI client for the accessibility pipeline.

Supports independent configuration of two roles:
  - ``primary``: remediation / generation (image & drawing descriptions, form field
    labels, org-chart hierarchy, manifest title drafting, alt-text repair).
  - ``eval``: quality evaluation (Stage 4 content/intent review and Stage 5
    alt-text sanity checks). Defaults to the ``primary`` configuration unless
    overridden (e.g. ``--eval-provider openai --eval-model gpt-4.1`` or
    ``--eval-provider gemini --eval-model gemini-2.5-pro``), allowing a separate
    model or provider to audit the primary model's output.

Supported providers (no extra SDKs beyond ``anthropic`` and ``requests``):
  - ``auto``        Anthropic API if ANTHROPIC_API_KEY is set, else headless
                    ``claude -p`` (Claude subscription), else OpenAI/Gemini if
                    their API keys are present, else ``none``.
  - ``claude-cli``  Headless Claude Code (``claude -p``), stripped of MCP
                    servers, settings, and skills (~1K token overhead).
  - ``anthropic``   Anthropic Messages API (``ANTHROPIC_API_KEY``).
  - ``openai``      OpenAI Chat Completions API (``OPENAI_API_KEY``) or any
                    OpenAI-compatible endpoint via ``base_url`` / ``OPENAI_BASE_URL``.
  - ``openrouter``  OpenRouter (OpenAI-compatible, ``OPENROUTER_API_KEY``).
  - ``ollama``      Local Ollama server (``http://localhost:11434/v1``, no key needed).
  - ``gemini``      Google Gemini REST API (``GEMINI_API_KEY`` or ``GOOGLE_API_KEY``).
  - ``none``        Disables AI calls for that role.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Any

import anthropic
from PIL import Image
import requests

DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"
DEFAULT_MODELS: dict[str, str] = {
    "claude-cli": DEFAULT_CLAUDE_MODEL,
    "anthropic": DEFAULT_CLAUDE_MODEL,
    "openai": "gpt-4.1-mini",
    "openrouter": "anthropic/claude-sonnet-4",
    "ollama": "qwen2.5vl",
    "gemini": "gemini-2.5-flash",
    "none": "",
}

PROVIDER_ALIASES: dict[str, str] = {
    "auto": "auto",
    "subscription": "claude-cli",
    "claude": "claude-cli",
    "claude-cli": "claude-cli",
    "claude_cli": "claude-cli",
    "api": "anthropic",
    "anthropic": "anthropic",
    "openai": "openai",
    "openai-compatible": "openai",
    "openai_compatible": "openai",
    "openrouter": "openrouter",
    "ollama": "ollama",
    "gemini": "gemini",
    "google": "gemini",
    "none": "none",
    "off": "none",
}

ALT_MAX = 150  # characters; screen readers read alt text as one unbroken block

_lock = threading.Lock()
_anthropic_clients: dict[str, anthropic.Anthropic] = {}

usage_totals: dict[str, Any] = {
    "calls": 0,
    "cost_usd": 0.0,
    "by_role": {
        "primary": {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
        "eval": {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
    },
}


@dataclass(frozen=True)
class AIConfig:
    provider: str = "auto"
    model: str = ""
    base_url: str | None = None
    api_key_env: str | None = None

    def normalized_provider(self) -> str:
        key = (self.provider or "auto").strip().lower()
        if key not in PROVIDER_ALIASES:
            valid = ", ".join(sorted({"auto", "claude-cli", "anthropic", "openai",
                                      "openrouter", "ollama", "gemini", "none"}))
            raise ValueError(f"Unknown AI provider {self.provider!r}. Choose from: {valid}")
        return PROVIDER_ALIASES[key]


_primary_cfg = AIConfig(provider="auto", model=DEFAULT_CLAUDE_MODEL)
_eval_cfg: AIConfig | None = None  # None means inherit primary


def _env_cfg(prefix: str) -> dict[str, str | None]:
    return {
        "provider": os.environ.get(f"{prefix}_PROVIDER") or None,
        "model": os.environ.get(f"{prefix}_MODEL") or None,
        "base_url": os.environ.get(f"{prefix}_BASE_URL") or None,
        "api_key_env": os.environ.get(f"{prefix}_API_KEY_ENV") or None,
    }


def _merge_cfg(*layers: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for layer in layers:
        if not layer:
            continue
        for k in ("provider", "model", "base_url", "api_key_env"):
            if layer.get(k):
                out[k] = str(layer[k]).strip()
    return out


def configure(
    *,
    primary: dict[str, Any] | None = None,
    eval_cfg: dict[str, Any] | None = None,
    config_yaml: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
) -> tuple[AIConfig, AIConfig | None]:
    """Configure primary and eval AI backends.

    Precedence (highest to lowest): explicit arguments (CLI flags) > environment
    variables (``A11Y_AI_*`` / ``A11Y_EVAL_*``) > manifest ``project.ai`` /
    ``project.eval`` > ``config/base.yaml`` ``ai`` / ``eval`` > defaults.
    """
    global _primary_cfg, _eval_cfg
    base_ai = (config_yaml or {}).get("ai") or {}
    base_eval = (config_yaml or {}).get("eval") or {}
    proj_ai = (project or {}).get("ai") or {}
    proj_eval = (project or {}).get("eval") or {}

    p_merged = _merge_cfg(base_ai, proj_ai, _env_cfg("A11Y_AI"), primary)
    _primary_cfg = AIConfig(
        provider=p_merged.get("provider", "auto"),
        model=p_merged.get("model", ""),
        base_url=p_merged.get("base_url"),
        api_key_env=p_merged.get("api_key_env"),
    )
    # validate immediately so typos fail fast
    _primary_cfg.normalized_provider()

    e_merged = _merge_cfg(base_eval, proj_eval, _env_cfg("A11Y_EVAL"), eval_cfg)
    if e_merged:
        _eval_cfg = AIConfig(
            provider=e_merged.get("provider") or _primary_cfg.provider,
            model=e_merged.get("model", ""),
            base_url=e_merged.get("base_url") or (
                _primary_cfg.base_url if not e_merged.get("provider") else None
            ),
            api_key_env=e_merged.get("api_key_env"),
        )
        _eval_cfg.normalized_provider()
    else:
        _eval_cfg = None
    return _primary_cfg, _eval_cfg


def get_config(role: str = "primary") -> AIConfig:
    if role == "eval" and _eval_cfg is not None:
        return _eval_cfg
    return _primary_cfg


def eval_is_separate() -> bool:
    return _eval_cfg is not None and (
        resolve_provider("eval") != resolve_provider("primary")
        or resolve_model("eval") != resolve_model("primary")
        or (get_config("eval").base_url or "") != (get_config("primary").base_url or "")
    )


def resolve_provider(role: str = "primary") -> str:
    cfg = get_config(role)
    prov = cfg.normalized_provider()
    if prov == "auto":
        if os.environ.get(cfg.api_key_env or "ANTHROPIC_API_KEY"):
            return "anthropic"
        if shutil.which("claude"):
            return "claude-cli"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai"
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return "gemini"
        return "none"
    if prov == "claude-cli" and not shutil.which("claude"):
        return "none"
    return prov


def resolve_model(role: str = "primary") -> str:
    cfg = get_config(role)
    prov = resolve_provider(role)
    if prov == "none":
        return ""
    if cfg.model:
        return cfg.model
    return DEFAULT_MODELS.get(prov, DEFAULT_CLAUDE_MODEL)


def backend(role: str = "primary") -> str:
    """Short status string for a role.

    Keeps legacy return values (``"api"``, ``"subscription"``, ``"none"``) for
    Anthropic/Claude-CLI so existing callers remain compatible, and returns the
    provider name (``"openai"``, ``"openrouter"``, ``"ollama"``, ``"gemini"``)
    for other providers.
    """
    prov = resolve_provider(role)
    if prov == "anthropic":
        return "api"
    if prov == "claude-cli":
        return "subscription"
    return prov


def describe_role(role: str = "primary") -> str:
    prov = resolve_provider(role)
    model = resolve_model(role)
    if prov == "none":
        return "none"
    labels = {
        "claude-cli": "Claude subscription",
        "anthropic": "Anthropic API",
        "openai": "OpenAI API",
        "openrouter": "OpenRouter",
        "ollama": "Ollama",
        "gemini": "Google Gemini API",
    }
    return f"{model} ({labels.get(prov, prov)})"


def role_signature(role: str = "primary") -> str:
    """Stable identifier for cache fingerprinting (stamps)."""
    cfg = get_config(role)
    return f"{resolve_provider(role)}:{resolve_model(role)}:{cfg.base_url or ''}"


def _record_usage(role: str, cost_usd: float = 0.0, input_tokens: int = 0, output_tokens: int = 0) -> None:
    key = "eval" if role == "eval" else "primary"
    with _lock:
        usage_totals["calls"] += 1
        usage_totals["cost_usd"] += cost_usd
        bucket = usage_totals["by_role"][key]
        bucket["calls"] += 1
        bucket["cost_usd"] += cost_usd
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens


def image_block(img_bytes: bytes, max_side: int = 1568) -> dict:
    """PNG content block, downscaled to a vision model's useful resolution."""
    img = Image.open(io.BytesIO(img_bytes))
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(buf.getvalue()).decode(),
        },
    }


def _strip_json_fences(text: str) -> str:
    s = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, flags=re.S | re.I)
    return m.group(1).strip() if m else s


def _parse_json_text(text: str, provider_label: str) -> dict:
    cleaned = _strip_json_fences(text)
    try:
        parsed = json.loads(cleaned)
    except ValueError as e:
        raise RuntimeError(f"{provider_label} answer was not valid JSON: {cleaned[:160]}") from e
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{provider_label} answer was JSON but not an object")
    return parsed


# ---------------------------------------------------------------------------
# Provider backends
# ---------------------------------------------------------------------------
def _get_anthropic_client(api_key: str, base_url: str | None) -> anthropic.Anthropic:
    cache_key = f"{api_key[:12]}:{base_url or ''}"
    with _lock:
        if cache_key not in _anthropic_clients:
            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            _anthropic_clients[cache_key] = anthropic.Anthropic(**kwargs)
        return _anthropic_clients[cache_key]


def _call_anthropic(cfg: AIConfig, model: str, system: str, content: list[dict],
                    schema: dict | None, effort: str, role: str) -> str | dict | None:
    api_key = os.environ.get(cfg.api_key_env or "ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(f"Missing {cfg.api_key_env or 'ANTHROPIC_API_KEY'} for Anthropic API")
    client = _get_anthropic_client(api_key, cfg.base_url)
    output_config: dict[str, Any] = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            output_config=output_config,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.BadRequestError as e:
        # Older Claude models may not accept output_config; fall back to JSON instruction
        if "output_config" not in str(e).lower():
            raise RuntimeError(f"Anthropic API call failed: {e}") from e
        sys_prompt = system + (f"\n\nRespond ONLY with valid JSON matching this schema:\n{json.dumps(schema)}"
                               if schema else "")
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=8192,
                system=sys_prompt,
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as e2:
            raise RuntimeError(f"Anthropic API call failed: {e2}") from e2
    except anthropic.APIError as e:
        raise RuntimeError(f"Anthropic API call failed: {e}") from e

    in_tok = getattr(msg.usage, "input_tokens", 0) or 0
    out_tok = getattr(msg.usage, "output_tokens", 0) or 0
    _record_usage(role, input_tokens=in_tok, output_tokens=out_tok)
    if msg.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return _parse_json_text(text, "Anthropic API") if schema else text


def _call_claude_cli(model: str, system: str, content: list[dict],
                     schema: dict | None, effort: str, role: str) -> str | dict | None:
    cli = shutil.which("claude")
    if not cli:
        raise RuntimeError("`claude` CLI not found on PATH")
    cmd = [
        cli, "-p", "--model", model, "--effort", effort, "--tools", "",
        "--no-session-persistence", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands", "--setting-sources", "", "--system-prompt", system,
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
    ]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    stdin = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=600,
                                  env=env, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("claude -p gave no answer within 10 minutes") from e
    result = None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result = event
    if result is None or result.get("is_error") or result.get("subtype") != "success":
        detail = (result or {}).get("result") or proc.stderr[-300:]
        raise RuntimeError(f"claude -p failed: {detail}")
    _record_usage(role, cost_usd=float(result.get("total_cost_usd") or 0.0))
    return result.get("structured_output") if schema else (result.get("result") or "").strip()


def _to_openai_parts(content: list[dict]) -> list[dict]:
    parts: list[dict] = []
    for block in content:
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block["text"]})
        elif btype == "image":
            src = block["source"]
            mime = src.get("media_type", "image/png")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{src['data']}"},
            })
    return parts


def _call_openai_compat(prov: str, cfg: AIConfig, model: str, system: str, content: list[dict],
                        schema: dict | None, effort: str, role: str) -> str | dict | None:
    if prov == "ollama":
        base_url = (cfg.base_url or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1").rstrip("/")
        api_key = os.environ.get(cfg.api_key_env or "OLLAMA_API_KEY") or "ollama"
    elif prov == "openrouter":
        base_url = (cfg.base_url or "https://openrouter.ai/api/v1").rstrip("/")
        api_key = os.environ.get(cfg.api_key_env or "OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(f"Missing {cfg.api_key_env or 'OPENROUTER_API_KEY'} for OpenRouter")
    else:
        base_url = (cfg.base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        api_key = os.environ.get(cfg.api_key_env or "OPENAI_API_KEY")
        is_local = any(h in base_url for h in ("localhost", "127.0.0.1", "0.0.0.0"))
        if not api_key and not is_local:
            raise RuntimeError(f"Missing {cfg.api_key_env or 'OPENAI_API_KEY'} for OpenAI API")

    url = f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    user_parts = _to_openai_parts(content)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_parts},
        ],
    }
    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "a11y_response", "strict": True, "schema": schema},
        }

    def post(body: dict[str, Any]) -> requests.Response:
        try:
            return requests.post(url, headers=headers, json=body, timeout=300)
        except requests.RequestException as e:
            raise RuntimeError(f"{prov} API request failed: {e}") from e

    resp = post(payload)
    # Some OpenAI-compatible servers (Ollama, older models) reject strict json_schema;
    # fall back to json_object + schema embedded in the system prompt.
    if schema and resp.status_code in (400, 422):
        fallback_sys = (
            f"{system}\n\nRespond ONLY with a valid JSON object conforming to this JSON Schema:\n"
            f"{json.dumps(schema)}"
        )
        fallback_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": fallback_sys},
                {"role": "user", "content": user_parts},
            ],
            "response_format": {"type": "json_object"},
        }
        resp2 = post(fallback_payload)
        if resp2.ok:
            resp = resp2
        elif resp2.status_code in (400, 422):
            fallback_payload.pop("response_format", None)
            resp = post(fallback_payload)

    if not resp.ok:
        raise RuntimeError(f"{prov} API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    usage = data.get("usage") or {}
    _record_usage(
        role,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"{prov} API returned no choices")
    msg = choices[0].get("message") or {}
    if msg.get("refusal"):
        return None
    raw_content = msg.get("content")
    if isinstance(raw_content, list):
        text = "".join(p.get("text", "") for p in raw_content if isinstance(p, dict)).strip()
    else:
        text = str(raw_content or "").strip()
    if not text:
        return None
    return _parse_json_text(text, f"{prov} API") if schema else text


def _gemini_schema(schema: Any) -> Any:
    """Strip JSON Schema keys Gemini's responseSchema rejects (e.g. additionalProperties)."""
    if isinstance(schema, list):
        return [_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    drop = {"additionalProperties", "$schema"}
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in drop:
            continue
        out[k] = _gemini_schema(v)
    return out


def _call_gemini(cfg: AIConfig, model: str, system: str, content: list[dict],
                 schema: dict | None, effort: str, role: str) -> str | dict | None:
    api_key = (os.environ.get(cfg.api_key_env) if cfg.api_key_env else None) \
        or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(f"Missing {cfg.api_key_env or 'GEMINI_API_KEY'} for Gemini API")
    base_url = (cfg.base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    url = f"{base_url}/models/{model}:generateContent"

    parts: list[dict[str, Any]] = []
    for block in content:
        if block.get("type") == "text":
            parts.append({"text": block["text"]})
        elif block.get("type") == "image":
            src = block["source"]
            parts.append({"inline_data": {"mime_type": src.get("media_type", "image/png"),
                                          "data": src["data"]}})

    gen_cfg: dict[str, Any] = {}
    if schema:
        gen_cfg["responseMimeType"] = "application/json"
        gen_cfg["responseSchema"] = _gemini_schema(schema)

    payload: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": gen_cfg,
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=300)
    except requests.RequestException as e:
        raise RuntimeError(f"Gemini API request failed: {e}") from e
    if not resp.ok:
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    meta = data.get("usageMetadata") or {}
    _record_usage(
        role,
        input_tokens=int(meta.get("promptTokenCount") or 0),
        output_tokens=int(meta.get("candidatesTokenCount") or 0),
    )
    candidates = data.get("candidates") or []
    if not candidates:
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            return None
        raise RuntimeError("Gemini API returned no candidates")
    cand = candidates[0]
    if cand.get("finishReason") in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
        return None
    text = "".join(
        p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])
        if isinstance(p, dict) and not p.get("thought")
    ).strip()
    if not text:
        return None
    return _parse_json_text(text, "Gemini API") if schema else text


# ---------------------------------------------------------------------------
# Public call entry points
# ---------------------------------------------------------------------------
def ask(system: str, content: list[dict], schema: dict | None = None,
        effort: str = "medium", role: str = "primary") -> str | dict | None:
    """One AI turn for ``role`` (``"primary"`` or ``"eval"``).

    Returns parsed JSON dict when ``schema`` is given, else text; ``None`` on
    refusal. Every transport/provider failure raises ``RuntimeError``.
    """
    cfg = get_config(role)
    prov = resolve_provider(role)
    model = resolve_model(role)
    if prov == "none":
        raise RuntimeError(f"No AI backend configured for role={role}")
    if prov == "anthropic":
        return _call_anthropic(cfg, model, system, content, schema, effort, role)
    if prov == "claude-cli":
        return _call_claude_cli(model, system, content, schema, effort, role)
    if prov in ("openai", "openrouter", "ollama"):
        return _call_openai_compat(prov, cfg, model, system, content, schema, effort, role)
    if prov == "gemini":
        return _call_gemini(cfg, model, system, content, schema, effort, role)
    raise RuntimeError(f"Unsupported AI provider: {prov}")


def ask_primary(system: str, content: list[dict], schema: dict | None = None,
                effort: str = "medium") -> str | dict | None:
    return ask(system, content, schema=schema, effort=effort, role="primary")


def ask_eval(system: str, content: list[dict], schema: dict | None = None,
             effort: str = "medium") -> str | dict | None:
    return ask(system, content, schema=schema, effort=effort, role="eval")


def preflight(role: str = "primary") -> str | None:
    """Tiny connectivity check before a batch run: None when OK, else error message."""
    if resolve_provider(role) == "none":
        return f"no AI backend configured for {role}"
    try:
        ask("Reply with the single word OK.", [{"type": "text", "text": "OK?"}],
            effort="low", role=role)
    except RuntimeError as e:
        return str(e)[:300]
    return None


def fit_alt(text: str, limit: int = ALT_MAX) -> str:
    """Bring alt text within ``limit``: one request to shorten, then cut at a
    sentence or word boundary (never mid-word)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if resolve_provider("primary") != "none":
        try:
            shorter = ask_primary(
                "You shorten alt text without losing its key fact.",
                [{
                    "type": "text",
                    "text": (f"Rewrite this alt text in at most {limit - 10} characters, one sentence, "
                             f"keeping the most important information. Reply with the text only.\n\n{text}"),
                }],
                effort="low",
            )
            if isinstance(shorter, str) and shorter and len(" ".join(shorter.split())) <= limit:
                return " ".join(shorter.split())
        except RuntimeError:
            pass
    cut = text[:limit]
    sentence = max(cut.rfind(". "), cut.rfind("; "))
    if sentence > limit * 0.5:
        return cut[:sentence + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip(",;:") + "…"
