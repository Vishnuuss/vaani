"""LLM client -- pooled, warm, streaming, cancellable.

Vendor choice is settled in vaani/bench/FINDINGS.md §2:

  primary   sarvam-105b-conversations   India-hosted, Telugu-native, no reasoning tax
  fallback  groq openai/gpt-oss-120b    faster on paper, worse Telugu, 175-270ms
                                        of it spent on reasoning we discard

CONNECTION REUSE IS LOAD-BEARING. Measured 2026-08-25: a fresh TLS connection
per request put first-token at ~950ms; a pooled pre-warmed client put the same
model at ~330ms. One client per provider, opened at startup, held for the
process lifetime. Never construct a client per turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from dataclasses import dataclass
from typing import AsyncIterator

import httpx


@dataclass(frozen=True)
class Provider:
    name: str
    chat_url: str
    models_url: str
    env_key: str
    model: str
    extra: tuple                       # tuple of (k, v) -- hashable for frozen dataclass

    @property
    def body_extra(self) -> dict:
        return dict(self.extra)


SARVAM = Provider(
    name="sarvam",
    chat_url="https://api.sarvam.ai/v1/chat/completions",
    models_url="https://api.sarvam.ai/v1/models",
    env_key="SARVAM_API_KEY",
    model="sarvam-105b-conversations",
    extra=(),
)

GROQ = Provider(
    name="groq",
    chat_url="https://api.groq.com/openai/v1/chat/completions",
    models_url="https://api.groq.com/openai/v1/models",
    env_key="GROQ_API_KEY",
    model="openai/gpt-oss-120b",
    # reasoning_format=parsed keeps analysis tokens OUT of delta.content, so the
    # first content token is genuinely speakable. Without this the agent sounds
    # slow even though TTFT looks fast.
    extra=(("reasoning_effort", "low"), ("reasoning_format", "parsed")),
)


MAX_RETRIES = 4
RETRY_BASE_S = 1.5
RETRY_CAP_S = 20.0


class LLMError(RuntimeError):
    pass


class _Retryable(RuntimeError):
    """Rate limit or transient upstream failure -- worth another attempt."""


class LLMClient:
    """Holds one warm pooled connection per provider."""

    def __init__(self, primary: Provider = SARVAM, fallback: Provider | None = GROQ):
        self.primary = primary
        self.fallback = fallback
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def __aenter__(self) -> "LLMClient":
        for provider in filter(None, (self.primary, self.fallback)):
            if not os.environ.get(provider.env_key):
                continue
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=5.0),
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
                headers={"Authorization": f"Bearer {os.environ[provider.env_key]}"},
            )
            self._clients[provider.name] = client
            # Pre-warm: pay the TCP+TLS handshake now, not on the first turn.
            try:
                await client.get(provider.models_url)
            except Exception:  # noqa: BLE001 - warm-up is best effort
                pass
        if not self._clients:
            raise LLMError("no LLM provider configured -- set SARVAM_API_KEY or GROQ_API_KEY")
        return self

    async def __aexit__(self, *exc) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def stream(
        self,
        messages: list[dict],
        *,
        provider: Provider | None = None,
        max_tokens: int = 160,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Yield speakable content deltas as they arrive.

        Cancel-safe: cancelling the task closes the HTTP stream, which is what
        the speculator relies on when the caller keeps talking.
        """
        provider = provider or self.primary
        client = self._clients.get(provider.name)
        if client is None:
            if self.fallback and self.fallback.name in self._clients:
                provider = self.fallback
                client = self._clients[provider.name]
            else:
                raise LLMError(f"provider {provider.name} not initialised")

        body = {
            "model": provider.model,
            "stream": True,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
            **provider.body_extra,
        }

        # Retry only on rate limits and transient 5xx. A 400 is our bug and
        # retrying it just burns quota.
        for attempt in range(MAX_RETRIES + 1):
            try:
                async for chunk in self._stream_once(client, provider, body):
                    yield chunk
                return
            except _Retryable as exc:
                if attempt == MAX_RETRIES:
                    raise LLMError(str(exc)) from None
                # Full jitter: a synchronised retry storm from 30 parallel
                # simulated calls would just rate-limit us again in lockstep.
                delay = min(RETRY_CAP_S, RETRY_BASE_S * 2 ** attempt)
                await asyncio.sleep(random.uniform(0, delay))

    async def _stream_once(self, client, provider: Provider, body: dict):
        async with client.stream("POST", provider.chat_url, json=body) as resp:
            if resp.status_code in (429, 500, 502, 503, 504):
                detail = (await resp.aread())[:160].decode(errors="replace")
                raise _Retryable(f"{provider.name} HTTP {resp.status_code}: {detail}")
            if resp.status_code != 200:
                detail = (await resp.aread())[:200].decode(errors="replace")
                raise LLMError(f"{provider.name} HTTP {resp.status_code}: {detail}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    return
                try:
                    choices = json.loads(data).get("choices") or []
                except json.JSONDecodeError:
                    continue
                if not choices:
                    continue
                # Only `content` is speakable. `reasoning` / `reasoning_content`
                # is the analysis channel and must never reach TTS.
                content = (choices[0].get("delta") or {}).get("content")
                if content:
                    yield content

    async def complete(self, messages: list[dict], **kw) -> str:
        """Non-streaming convenience, for off-realtime work (extraction, etc)."""
        return "".join([chunk async for chunk in self.stream(messages, **kw)])
