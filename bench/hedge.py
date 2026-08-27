"""How much of the tail does hedging buy?

F7: identical requests to Groq return first content anywhere from 0.26s to 1.16s.
When the spread is that wide, the cheapest fix is not a faster model -- it is to
issue the same request twice and use whichever replies first (Dean & Barroso,
"The Tail at Scale"). gpt-oss-120b is cheap and the prompt is cached, so the
second copy costs little.

This samples the real distribution, then reports what hedging 2 and 3 ways would
have produced from those same draws, so the decision rests on measured numbers
rather than on the theory.
"""
from __future__ import annotations

import concurrent.futures as cf
import json, statistics, sys, time, urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
env = {}
for p in (ROOT / ".env", Path("c:/Users/vishnu/Downloads/bswealthfinance/.env")):
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
KEY = env["GROQ_API_KEY"]
SYSTEM = (ROOT / "bench/system_prompt.txt").read_text(encoding="utf-8")


def one() -> float | None:
    """Seconds to the first CONTENT token, or None on error."""
    body = {"model": "openai/gpt-oss-120b", "stream": True, "temperature": 0.3,
            "max_completion_tokens": 120, "reasoning_effort": "low",
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "assistant", "content": "MODE: ASK\nసరే, మీ నెల బిల్లు ఎంత వస్తుంది?"},
                         {"role": "user", "content": "5 టు 10,000 వస్తుంది."}]}
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                 "User-Agent": "vaani-bench/1.0"}, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                p = line[5:].strip()
                if p == "[DONE]":
                    break
                try:
                    ch = json.loads(p)
                except json.JSONDecodeError:
                    continue
                for c in ch.get("choices") or []:
                    if (c.get("delta") or {}).get("content"):
                        return time.perf_counter() - t0
    except Exception:
        return None
    return None


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


N = 24
print(f"sampling {N} independent calls (same prompt, same params)...", flush=True)
one()  # warm the cache
singles = []
for i in range(N):
    v = one()
    if v:
        singles.append(v)
    print(f"  {i+1}/{N} {v:.3f}s" if v else f"  {i+1}/{N} err", flush=True)

print(f"\nSINGLE  n={len(singles)}  p50 {statistics.median(singles):.3f}  "
      f"p90 {pct(singles,0.9):.3f}  min {min(singles):.3f}  max {max(singles):.3f}")

# What hedging would have produced, drawn from the SAME distribution.
import random
for k in (2, 3):
    sim = [min(random.sample(singles, k)) for _ in range(4000)]
    print(f"HEDGE{k}  p50 {statistics.median(sim):.3f}  p90 {pct(sim,0.9):.3f}  "
          f"max {max(sim):.3f}   (simulated from the {len(singles)} real draws)")

# And measured for real: fire 2 concurrently, take the first.
print(f"\nmeasuring REAL hedge-2 (2 concurrent requests, first wins), 10 rounds...",
      flush=True)
real = []
with cf.ThreadPoolExecutor(max_workers=6) as ex:
    for i in range(10):
        t0 = time.perf_counter()
        futs = [ex.submit(one) for _ in range(2)]
        got = None
        for f in cf.as_completed(futs, timeout=70):
            v = f.result()
            if v is not None:
                got = time.perf_counter() - t0
                break
        for f in futs:
            f.cancel()
        if got:
            real.append(got)
        print(f"  {i+1}/10 {got:.3f}s" if got else f"  {i+1}/10 err", flush=True)

if real:
    print(f"\nREAL HEDGE2  n={len(real)}  p50 {statistics.median(real):.3f}  "
          f"p90 {pct(real,0.9):.3f}  max {max(real):.3f}")
    print(f"\nsingle p90 {pct(singles,0.9):.3f}s -> hedge2 p90 {pct(real,0.9):.3f}s "
          f"= {pct(singles,0.9)-pct(real,0.9):+.3f}s off the tail")
