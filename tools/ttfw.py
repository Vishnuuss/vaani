"""Time to first AUDIBLE WORD, per model, on easy and hard Telugu turns.

Why this and not `compare_models.py`: that tool rewrites a workflow's LLM
repeatedly and needs a scratch workflow to be safe. None exists on this server
(7-10 are 404); the only workflows are the five client agents. This talks to
Groq directly and touches no workflow at all.

Why "first audible word" and not first chunk: `openai/gpt-oss-120b` is a
REASONING model. Its opening chunks carry an `analysis` channel the caller
cannot hear, and `hedged_llm.py` already measured that the first chunk arrives
at ~0.2s while the first content arrives at ~0.5s, uncorrelated. Timing the
first chunk measures nothing a caller experiences.

The question being answered is the client's: latency must be the same whether
the question is easy or hard. A reasoning model thinks longer on a harder
question, so the EASY/HARD spread is the number that matters here, not the
median.

    python tools/ttfw.py
    python tools/ttfw.py --n 5 --models llama-3.3-70b-versatile
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

env = {}
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

KEY = env["GROQ_API_KEY"]
URL = "https://api.groq.com/openai/v1/chat/completions"

CANDIDATES = [
    "openai/gpt-oss-120b",          # incumbent: reasoning, the one being judged
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.1-8b-instant",
]

SYSTEM = (
    "You are Priya from MB Solar Hub, speaking natural spoken Telugu on the "
    "phone. Answer in ONE or TWO short sentences, the way somebody speaks. "
    "Never invent a number, price, location or brand."
)

# One easy turn and one the incumbent demonstrably labours over. Run 898's
# caller asked for exactly the second one and waited.
TURNS = {
    "easy": "హైదరాబాద్‌లో ఉంటాను.",
    "hard": ("సోలార్ గురించి కొంచెం తెలుగులో ఎక్స్‌ప్లెయిన్ చేస్తారా? "
             "వింటర్ టైంలో పనిచేస్తుందా, రాత్రి పనిచేస్తుందా, "
             "సబ్సిడీ ఎంత వస్తుంది?"),
}


def first_word_secs(model: str, user: str, timeout: float = 25.0):
    """Seconds until the first CONTENT token. None if the model said nothing."""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "stream": True,
        "max_tokens": 200,
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json",
                 # Cloudflare in front of api.groq.com answers a
                 # User-Agent-less request with error 1010 ("banned based on
                 # your browser's signature"), which looks exactly like an
                 # auth failure. It is not: the key is fine.
                 "User-Agent": "vaani-ttfw/1.0"},
        method="POST")
    began = time.monotonic()
    buffered = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return None
                try:
                    chunk = json.loads(payload)
                except Exception:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                # `content` only: `reasoning` is a separate channel the
                # caller never hears, and counting it is the mistake this tool
                # exists to avoid.
                #
                # And not every model keeps them separate. `qwen/qwen3.6-27b`
                # streams its thinking INSIDE content, wrapped in <think>, so
                # the first version of this scored it 0.296s -- the fastest of
                # any candidate -- for the first token of a monologue the TTS
                # would have read aloud to the caller. It is disqualified on
                # those grounds anyway, but the number was wrong before it was
                # disqualified, which is worse.
                piece = (delta.get("content") or "")
                buffered += piece
                if "<think>" in buffered and "</think>" not in buffered:
                    continue
                visible = buffered.split("</think>")[-1]
                if visible.strip():
                    return time.monotonic() - began
    except Exception as exc:
        print(f"    {model}: ERROR {exc}")
        return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--models", nargs="*", default=CANDIDATES)
    a = ap.parse_args()

    print(f"time to first AUDIBLE word, {a.n} draws per cell\n")
    print(f"{'model':<48} {'easy p50':>9} {'hard p50':>9} {'spread':>8}")
    print("-" * 78)
    for model in a.models:
        cell = {}
        for label, text in TURNS.items():
            got = [s for s in (first_word_secs(model, text)
                               for _ in range(a.n)) if s is not None]
            cell[label] = statistics.median(got) if got else None
        easy, hard = cell.get("easy"), cell.get("hard")
        if easy is None or hard is None:
            print(f"{model:<48} {'DEAD':>9} {'DEAD':>9} {'-':>8}")
            continue
        print(f"{model:<48} {easy:>9.3f} {hard:>9.3f} {hard - easy:>+8.3f}")
    print("\nspread is the client's requirement: latency must not depend on "
          "how hard the question is.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
