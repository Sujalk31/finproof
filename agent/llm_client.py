"""
Multi-provider LLM client (plan section 24-25: "An LLM for exception
investigation / explanation generation / root-cause reasoning / reports").

FinProof tries a small chain of OpenAI-compatible providers, in order, for
every call:

    1. NVIDIA NIM   (https://build.nvidia.com)      -- primary
    2. Groq         (https://console.groq.com)       -- secondary fallback
    3. Deterministic rule-based logic                -- final fallback,
       handled by the CALLER (investigator.py / chatbot.py), not here.

If provider 1 fails for any reason (bad model name, account not provisioned,
rate limit, network error, etc.) provider 2 is tried automatically before
giving up. This is what actually fixes the common "works in the NVIDIA
playground but 404s from the API" account-provisioning issue in practice --
Groq is a completely separate account/infrastructure, so it isn't affected
by an NVIDIA-side permission gap, and vice versa.

Configuration (all via environment variables, see .env.example):
    NVIDIA_API_KEY, NVIDIA_MODEL, NVIDIA_BASE_URL   (provider 1)
    GROQ_API_KEY,   GROQ_MODEL,   GROQ_BASE_URL     (provider 2)

Either provider can be configured alone, both, or neither (in which case
is_configured() returns False and every caller uses its rule-based path).

Design note on error handling: raw provider errors (HTTP status codes,
internal model IDs, JSON error bodies) are never meant to reach the user
directly -- they're classified into a short, stable, human-readable reason
(see _classify_error) and every caller is expected to build its own
audit-trail-friendly label from LLMUnavailable.reason, not from str(e) on
the underlying exception. This is the single place new error patterns
should be taught, so every caller (investigator, chatbot, future callers)
benefits automatically.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Provider:
    name: str            # short machine id, e.g. "nvidia"
    label: str           # human-friendly, shown in the UI, e.g. "NVIDIA Nemotron"
    api_key: str
    base_url: str
    model: str


def _providers() -> list[_Provider]:
    """Returns configured providers in priority order. Re-read from the
    environment on every call (not cached at import time) so tests / a
    running server can pick up a changed .env without a code reload."""
    out = []

    nvidia_key = os.environ.get("NVIDIA_API_KEY")
    if nvidia_key:
        model = os.environ.get("NVIDIA_MODEL", "nvidia/llama-3.1-nemotron-70b-instruct")
        out.append(_Provider(
            name="nvidia",
            label=f"NVIDIA NIM ({model.split('/')[-1]})",
            api_key=nvidia_key,
            base_url=os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            model=model,
        ))

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
        out.append(_Provider(
            name="groq",
            label=f"Groq ({model})",
            api_key=groq_key,
            base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            model=model,
        ))

    return out


def is_configured() -> bool:
    return bool(_providers())


# Kept for backward compatibility with any code/report text that referenced
# the old single-provider constant; now reflects whichever provider would be
# tried first.
DEFAULT_MODEL = _providers()[0].model if _providers() else None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LLMUnavailable(Exception):
    """Raised when no provider is configured, or every configured provider
    failed. `.reason` is a short, human-readable, UI-safe summary -- never
    the raw exception text (that would leak model names / HTTP bodies into
    the audit trail). `.attempts` has the per-provider detail for anyone who
    wants to log it server-side."""

    def __init__(self, reason: str, attempts: list[tuple[str, str]] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts or []


# Patterns are checked in order; first match wins. Covers NVIDIA NIM, Groq,
# and OpenAI-compatible providers generally, since the wording is similar
# across most hosted inference APIs.
_ERROR_PATTERNS = [
    (r"not found for account", "the configured model isn't enabled for this account"),
    (r"model_not_found|does not exist or you do not have access", "the configured model name isn't available on this provider"),
    (r"\b401\b|unauthorized|invalid api key|invalid_api_key", "authentication failed (check the API key)"),
    (r"\b429\b|rate limit", "rate limited (too many requests)"),
    (r"\b403\b|forbidden|permission", "access denied for this account/model"),
    (r"timed? ?out|timeout", "the request timed out"),
    (r"connection|name resolution|failed to establish|network", "a network/connection error occurred"),
    (r"\b5\d\d\b|internal server error|bad gateway|service unavailable", "the provider's service is temporarily unavailable"),
    (r"context_length_exceeded|maximum context", "the request was too long for the model's context window"),
]


def _classify_error(exc: Exception) -> str:
    text = str(exc).lower()
    for pattern, reason in _ERROR_PATTERNS:
        if re.search(pattern, text):
            return reason
    return "an unexpected error occurred"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _call_provider(provider: _Provider, messages: list, model: str, temperature: float, max_tokens: int) -> str:
    try:
        return _call_via_openai_sdk(provider, messages, model, temperature, max_tokens)
    except ImportError:
        return _call_via_raw_http(provider, messages, model, temperature, max_tokens)


def _call_via_openai_sdk(provider: _Provider, messages, model, temperature, max_tokens) -> str:
    from openai import OpenAI  # imported lazily so the package is optional

    client = OpenAI(base_url=provider.base_url, api_key=provider.api_key)
    completion = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        top_p=0.9,
        max_tokens=max_tokens,
    )
    return completion.choices[0].message.content


def _call_via_raw_http(provider: _Provider, messages, model, temperature, max_tokens) -> str:
    import requests  # stdlib-adjacent, always available in this project's env

    resp = requests.post(
        f"{provider.base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": 0.9,
            "max_tokens": max_tokens,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    text: str
    provider: str   # "nvidia" | "groq"
    model: str
    label: str      # human-friendly, safe to show directly in the UI/audit trail


def _chat(messages: list, temperature: float, max_tokens: int, model_override: str | None) -> LLMResponse:
    providers = _providers()
    if not providers:
        raise LLMUnavailable("no LLM provider configured")

    attempts: list[tuple[str, str]] = []
    for provider in providers:
        model = model_override or provider.model
        try:
            text = _call_provider(provider, messages, model, temperature, max_tokens)
            return LLMResponse(text=text, provider=provider.name, model=model, label=provider.label)
        except Exception as e:
            attempts.append((provider.label, _classify_error(e)))
            continue

    # Every configured provider failed. Build one clean summary line, e.g.
    # "NVIDIA NIM (nemotron-70b-instruct): model unavailable; Groq (llama-3.3-70b-versatile): rate limited"
    summary = "; ".join(f"{label}: {reason}" for label, reason in attempts)
    raise LLMUnavailable(summary, attempts=attempts)


def chat_completion(system_prompt: str, user_prompt: str, temperature: float = 0.2,
                     max_tokens: int = 500, model: str | None = None) -> LLMResponse:
    """Single-turn helper. Tries each configured provider in order and
    returns the first success. Raises LLMUnavailable if none succeed."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return _chat(messages, temperature, max_tokens, model)


def chat_multiturn(system_prompt: str, history: list, temperature: float = 0.3,
                    max_tokens: int = 600, model: str | None = None) -> LLMResponse:
    """history: list of {"role": "user"|"assistant", "content": str}.
    Used by the chatbot to keep short-term conversational context."""
    messages = [{"role": "system", "content": system_prompt}] + history
    return _chat(messages, temperature, max_tokens, model)


def parse_json_response(text: str) -> dict:
    """Models sometimes wrap JSON in ```json fences or add stray whitespace;
    this strips that defensively before parsing."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
    cleaned = cleaned.strip().rstrip("`").strip()
    return json.loads(cleaned)