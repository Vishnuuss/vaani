"""LLM benchmark -- time to first FINAL token, across providers.

Two corrections baked in here, both learned the hard way on 2026-08-25:

1. CONNECTION REUSE IS WORTH ~600ms. The first version of this benchmark opened
   a fresh TLS connection per request and measured gpt-oss-120b at 954ms. On a
   pooled, pre-warmed connection the same model measures ~326ms. That 750ms
   "floor" was our own handshake, not the vendor. The pipeline MUST hold one
   warm client per provider for the life of the process.

2. Raw TTFT still lies. gpt-oss-* emit an analysis channel before the final
   channel, so we time first-CONTENT-token separately and treat only that as
   the moment TTS can start.

Sarvam is a first-class contender here, not an afterthought: it is India-hosted
and Telugu-native, which can beat a faster US model once the call originates in
Mumbai.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .report import Metric, Section, pct

BUDGET_FIRST_FINAL_MS = 320.0

USER_TURN = "అవును సార్, నా ఇల్లు నాదే. కరెంట్ బిల్లు నెలకి మూడు వేలు వస్తుంది."

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions",
             "https://api.groq.com/openai/v1/models", "GROQ_API_KEY"),
    "sarvam": ("https://api.sarvam.ai/v1/chat/completions",
               "https://api.sarvam.ai/v1/models", "SARVAM_API_KEY"),
}


@dataclass
class Variant:
    label: str
    provider: str
    model: str
    body: dict = field(default_factory=dict)


VARIANTS = [
    Variant("groq   gpt-oss-120b effort=low", "groq", "openai/gpt-oss-120b",
            {"reasoning_effort": "low", "reasoning_format": "parsed"}),
    Variant("groq   gpt-oss-20b  effort=low", "groq", "openai/gpt-oss-20b",
            {"reasoning_effort": "low", "reasoning_format": "parsed"}),
    Variant("sarvam 105b-conversations", "sarvam", "sarvam-105b-conversations"),
    # sarvam-105b (non-conversations) is a reasoning model -- measured 8.1s p50.
    # Deliberately excluded: it is not a realtime candidate.
]


def _system_prompt(target_tokens: int = 3000) -> str:
    """Approximate the compiled 4-layer prompt. Prefill scales with length, so a
    toy prompt would give a number we could never reproduce in production."""
    block = (
        "You are a warm sales agent for a solar company, speaking natural colloquial "
        "Telugu on a phone call. Short sentences. No markdown. Numbers as words. "
        "Acknowledge objections before answering them. Never promise returns. "
        "Collect: home ownership, monthly electricity bill, installation timeline. "
    )
    return block * max(1, (target_tokens * 4) // len(block))


def _bench_one(client, url, key, variant, system_prompt, samples):
    """-> (headers_ms[], first_delta_ms[], first_final_ms[], sample_text, error)"""
    hdr, first_delta, first_final, sample = [], [], [], ""

    for _ in range(samples):
        body = {
            "model": variant.model, "stream": True, "max_completion_tokens": 90,
            "temperature": 0.7,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": USER_TURN}],
            **variant.body,
        }
        t0 = time.perf_counter()
        h_ms = d_ms = f_ms = None
        buf = []
        try:
            with client.stream("POST", url, json=body,
                               headers={"Authorization": f"Bearer {key}"}) as resp:
                h_ms = (time.perf_counter() - t0) * 1000.0
                if resp.status_code != 200:
                    detail = resp.read()[:120].decode(errors="replace")
                    return hdr, first_delta, first_final, sample, \
                        f"HTTP {resp.status_code}: {detail}"
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        choices = json.loads(data).get("choices") or []
                    except json.JSONDecodeError:
                        continue
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    now = (time.perf_counter() - t0) * 1000.0
                    if d_ms is None:
                        d_ms = now
                    if delta.get("content"):
                        if f_ms is None:
                            f_ms = now
                        buf.append(delta["content"])
        except Exception as exc:  # noqa: BLE001
            return hdr, first_delta, first_final, sample, \
                f"{type(exc).__name__}: {str(exc)[:110]}"

        if f_ms is not None:
            hdr.append(h_ms)
            first_delta.append(d_ms)
            first_final.append(f_ms)
            sample = sample or "".join(buf)
        time.sleep(0.15)

    return hdr, first_delta, first_final, sample, ""


def run(samples: int = 12) -> Section:
    sec = Section(title="LLM -- time to first FINAL token (pooled warm connection)")
    try:
        import httpx
    except ImportError:
        sec.error = "pip install httpx"
        return sec

    system_prompt = _system_prompt()
    replies = []

    for name, (chat_url, models_url, env_key) in PROVIDERS.items():
        variants = [v for v in VARIANTS if v.provider == name]
        if not variants:
            continue
        key = os.environ.get(env_key)
        if not key:
            for v in variants:
                sec.metric(f"{v.label}  | t_first_FINAL", note=f"{env_key} not set")
            continue

        # One pooled client per provider, pre-warmed before any timing starts.
        # This is the whole point of the file -- see the module docstring.
        with httpx.Client(timeout=45.0,
                          limits=httpx.Limits(max_keepalive_connections=10)) as client:
            try:
                client.get(models_url, headers={"Authorization": f"Bearer {key}"})
            except Exception:  # noqa: BLE001 - warm-up is best effort
                pass

            for v in variants:
                m_hdr = sec.metric(f"{v.label}  | response headers")
                m_final = sec.metric(f"{v.label}  | t_first_FINAL  <<<",
                                     budget=BUDGET_FIRST_FINAL_MS)
                hdr, fd, ff, sample, err = _bench_one(
                    client, chat_url, key, v, system_prompt, samples)
                for x in hdr:
                    m_hdr.add(x)
                for x in ff:
                    m_final.add(x)
                if err:
                    m_final.note = err
                elif fd and ff:
                    tax = pct(ff, 50) - pct(fd, 50)
                    m_final.note = (f"{tax:.0f}ms is discarded reasoning"
                                    if tax > 15 else "no reasoning tax")
                if sample:
                    replies.append(f"--- {v.label} ---\n{sample}\n")

    # Latency is only half the decision. The Telugu has to be read by a human,
    # so we always dump the actual replies next to the numbers.
    if replies:
        out = Path(os.environ.get("VAANI_BENCH_DIR", ".tmp/bench")) / "llm_telugu_samples.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(replies), encoding="utf-8")
        sec.metrics.append(Metric(
            name="=> TELUGU SAMPLES",
            note=f"read them: {out} -- latency alone must not pick the model",
        ))
    return sec
