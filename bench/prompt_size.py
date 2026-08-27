"""Does the 4-layer prompt's SIZE cost latency, or is the reasoning burn fixed?

The project has a standing rule against trimming prompts for latency -- objection
handling is the payload. That rule is only defensible if trimming genuinely buys
nothing, so this measures it rather than assuming it.

Four prompt sizes, same conversation, same model/params. If first-content time is
flat across sizes, prefill is not the problem and the rule holds. If it scales,
there is a real trade to discuss.
"""
from __future__ import annotations

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
FULL = (ROOT / "bench/system_prompt.txt").read_text(encoding="utf-8")


def stream(system: str, reps: int = 4) -> dict:
    firsts, contents, reasons, totals = [], [], [], []
    for _ in range(reps):
        body = {"model": "openai/gpt-oss-120b", "stream": True,
                "temperature": 0.3, "max_completion_tokens": 150,
                "reasoning_effort": "low",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "assistant", "content": "MODE: ASK\nసరే, మీ నెల బిల్లు ఎంత వస్తుంది?"},
                    {"role": "user", "content": "5 టు 10,000 వస్తుంది."}]}
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                     "User-Agent": "vaani-bench/1.0"}, method="POST")
        t0 = time.perf_counter()
        fb = fc = None
        rch = 0
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    p = line[5:].strip()
                    if p == "[DONE]":
                        break
                    if fb is None:
                        fb = time.perf_counter() - t0
                    try:
                        ch = json.loads(p)
                    except json.JSONDecodeError:
                        continue
                    for c in ch.get("choices") or []:
                        d = c.get("delta") or {}
                        if d.get("reasoning"):
                            rch += len(d["reasoning"])
                        if d.get("content") and fc is None:
                            fc = time.perf_counter() - t0
        except Exception as e:
            print(f"    err {e!r}")
            continue
        if fb and fc:
            firsts.append(fb); contents.append(fc); reasons.append(rch)
            totals.append(time.perf_counter() - t0)
    if not contents:
        return {}
    return {"first_byte": statistics.median(firsts),
            "first_content": statistics.median(contents),
            "reasoning": statistics.median(reasons), "n": len(contents)}


variants = [
    ("full 4-layer      ", FULL),
    ("half (~14k)       ", FULL[:len(FULL) // 2]),
    ("quarter (~7k)     ", FULL[:len(FULL) // 4]),
    ("minimal (~1k)     ", FULL[:1000]),
]

print(f"{'prompt':<20} {'chars':>7} {'1st byte':>9} {'1st content':>12} "
      f"{'burn':>7} {'reason ch':>10}")
print("-" * 72)
rows = {}
for label, sysprompt in variants:
    r = stream(sysprompt)
    if not r:
        print(f"{label:<20} {len(sysprompt):>7}  failed")
        continue
    burn = r["first_content"] - r["first_byte"]
    rows[label.strip()] = r
    print(f"{label:<20} {len(sysprompt):>7} {r['first_byte']:9.3f} "
          f"{r['first_content']:12.3f} {burn:7.3f} {r['reasoning']:10.0f}")

if len(rows) >= 2:
    keys = list(rows)
    big, small = rows[keys[0]], rows[keys[-1]]
    print(f"\nprefill cost of the full prompt vs minimal: "
          f"{big['first_byte'] - small['first_byte']:+.3f}s to first byte")
    print(f"total cost to first speakable content:        "
          f"{big['first_content'] - small['first_content']:+.3f}s")
    print("\n=> If both deltas are near zero, prompt length is NOT the bottleneck")
    print("   and the no-trimming rule stands.")
