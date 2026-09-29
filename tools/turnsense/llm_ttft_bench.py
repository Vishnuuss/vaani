#!/usr/bin/env python
"""How long until the caller hears the first word -- per model, with the REAL prompt.

`llm ttfb` on gpt-oss is a decoy (memory: wf6-latency-reasoning-low): it times the
first REASONING token. What the caller waits for is the first CONTENT token, and
really the first clause the TTS can start speaking. So this measures:

    content   first non-empty `delta.content`
    clause    first . ? ! , । inside the content -- the text aggregator's cue

System prompt = MB Solar's compiled 4-layer prompt (27,248 chars, compiled from
backups/wf2_def_20260924_103141.json by production's own compiler). Conversation
= the first turns of run 1021, verbatim. Every model gets identical input, and
models are interleaved round-robin so a slow patch of network hits all of them.

Measured from a laptop in India, NOT from the Mumbai server: absolute numbers
include the home link; the ORDER is what this is for. The first draw per model
warms the provider's prompt cache and is excluded from the percentiles.

    python tools/turnsense/llm_ttft_bench.py --runs 7
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import requests

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
SYSTEM = (REPO / ".tmp" / "turnsense" / "wf2_compiled_system.md").read_text(encoding="utf-8")


def env(k: str) -> str:
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


CONV = [
    ("assistant", "నమస్కారం అండి, ఎంబి సోలార్ హబ్ నుంచి ప్రియ మాట్లాడుతున్నాను. మీరు సోలార్ గురించి ఎంక్వైరీ చేశారు కదా, ఒక్క నిమిషం మాట్లాడవచ్చా?"),
    ("user", "ఆ... మాట్లాడొచ్చు."),
    ("assistant", "మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?"),
    ("user", "సొంత ఇల్లు."),
    ("assistant", "మంచిది, మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?"),
    ("user", "ఆ... మాది. 2 వేలు వస్తుంది. సబ్సిడీ ఎంత వస్తుంది అండి?"),
]

GROQ = ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", "bearer")
OPENAI = ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY", "bearer")
SARVAM = ("https://api.sarvam.ai/v1/chat/completions", "SARVAM_API_KEY", "sarvam")

CANDIDATES = {
    "groq gpt-oss-120b low (LIVE)": (GROQ, {"model": "openai/gpt-oss-120b", "reasoning_effort": "low"}),
    "groq gpt-oss-20b low": (GROQ, {"model": "openai/gpt-oss-20b", "reasoning_effort": "low"}),
    "groq qwen3.8-27b no-think": (GROQ, {"model": "qwen/qwen3.8-27b", "reasoning_effort": "none"}),
    "groq qwen3.6-27b no-think": (GROQ, {"model": "qwen/qwen3.6-27b", "reasoning_effort": "none"}),
    # OpenAI: account out of credits on 29 Sep (HTTP 429 insufficient_quota).
    # "openai gpt-4.1-mini": (OPENAI, {"model": "gpt-4.1-mini"}),
    "sarvam-105b (IN)": (SARVAM, {"model": "sarvam-105b"}),
    "sarvam-105b-conversations (IN)": (SARVAM, {"model": "sarvam-105b-conversations"}),
}

CLAUSE = set(".?!,।")

# One kept-alive session per provider, as production's HTTP clients are. A new
# connection per draw paid TCP + TLS setup to the US on every request, which put
# every model at 1.2-1.5 s from this laptop and measured the handshake, not the model.
_SESSIONS: dict[str, requests.Session] = {}


def _session(url: str) -> requests.Session:
    host = url.split("/")[2]
    if host not in _SESSIONS:
        _SESSIONS[host] = requests.Session()
    return _SESSIONS[host]


def one(endpoint, body: dict) -> dict:
    url, keyname, auth = endpoint
    key = env(keyname)
    h = {"Content-Type": "application/json"}
    if auth == "bearer":
        h["Authorization"] = f"Bearer {key}"
    else:
        h["api-subscription-key"] = key
    msgs = [{"role": "system", "content": SYSTEM}] + [
        {"role": r, "content": c} for r, c in CONV]
    payload = {**body, "messages": msgs, "stream": True, "temperature": 0.4}
    if "gpt-5" in body.get("model", ""):
        payload.pop("temperature")
        payload["max_completion_tokens"] = 400
    else:
        payload["max_tokens"] = 400
    t0 = time.perf_counter()
    first_byte = content_t = clause_t = None
    text = ""
    usage = None
    try:
        with _session(url).post(url, headers=h, json=payload, stream=True, timeout=60) as r:
            if r.status_code >= 400:
                return {"err": f"{r.status_code} {r.text[:200]}"}
            r.encoding = "utf-8"          # Groq sends no charset; requests guesses latin-1
            for raw in r.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                if first_byte is None:
                    first_byte = time.perf_counter() - t0
                if not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if j.get("usage"):
                    usage = j["usage"]
                if (j.get("x_groq") or {}).get("usage"):
                    usage = j["x_groq"]["usage"]
                for ch in j.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content") or ""
                    if piece:
                        if content_t is None and piece.strip():
                            content_t = time.perf_counter() - t0
                        text += piece
                        if clause_t is None and content_t is not None and any(
                                c in CLAUSE for c in piece):
                            clause_t = time.perf_counter() - t0
    except requests.RequestException as e:
        return {"err": type(e).__name__}
    return {"first_byte": first_byte, "content": content_t, "clause": clause_t,
            "total": time.perf_counter() - t0, "text": text, "usage": usage}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--only", help="substring filter on candidate names")
    a = ap.parse_args()
    names = [n for n in CANDIDATES if not a.only or a.only in n]
    res = {n: [] for n in names}
    for i in range(a.runs):
        for n in names:
            ep, body = CANDIDATES[n]
            r = one(ep, body)
            res[n].append(r)
            tag = r.get("err") or f"content {r['content'] or -1:.3f}  clause {r['clause'] or -1:.3f}"
            print(f"  run {i} {n:34} {tag}", flush=True)
    out = REPO / ".tmp" / "turnsense" / "llm_bench.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{'model':34} {'n':>2} {'content p50':>11} {'p90':>6} "
          f"{'clause p50':>10} {'p90':>6}  telugu%  sample")
    for n in names:
        rs = [r for r in res[n][1:] if not r.get("err") and r.get("content")]
        if not rs:
            errs = [r.get("err") for r in res[n] if r.get("err")]
            print(f"{n:34}  no successful draws  {errs[:1]}")
            continue
        c = sorted(r["content"] for r in rs)
        cl = sorted(r["clause"] for r in rs if r.get("clause"))
        txt = rs[-1]["text"].strip().replace("\n", " ")
        te = sum(1 for ch in txt if "ఀ" <= ch <= "౿")
        let = sum(1 for ch in txt if not ch.isspace() and ch not in ".,?!")
        p90 = lambda v: v[min(len(v) - 1, int(len(v) * 0.9))]   # noqa: E731
        print(f"{n:34} {len(rs):>2} {statistics.median(c):>11.3f} {p90(c):>6.3f} "
              f"{(statistics.median(cl) if cl else float('nan')):>10.3f} "
              f"{(p90(cl) if cl else float('nan')):>6.3f}  {te / max(1, let):6.0%}  "
              f"{txt[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
