"""Measure REAL, coherent prompt tiers -- and control for the prompt cache.

The first attempt measured each tier as a block: all reps of A, then all of B.
A was warm from earlier runs while every other tier was a fresh prefix, so it
reported the SMALLEST prompt as the slowest -- the exact opposite of what the
byte-truncation sweep had said. Two experiments disagreeing that hard means the
signal was cache warmth and server load, not prompt size.

So: warm every variant with discarded calls first, interleave round-robin so load
drift hits all tiers equally, and report the spread so an "effect" smaller than
the noise floor cannot be mistaken for a finding.
"""

from __future__ import annotations

import json, re, statistics, sys, time, urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
L = ROOT / "api/services/vaani/layers"
env = {}
for p in (ROOT / ".env", Path("c:/Users/vishnu/Downloads/bswealthfinance/.env")):
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
KEY = env["GROQ_API_KEY"]


def read(p): return (L / p).read_text(encoding="utf-8").strip()


def sections(text: str) -> dict[str, str]:
    """Split a layer into {heading: body-including-heading}, order preserved."""
    out, cur, buf = {}, "(preamble)", []
    for line in text.splitlines(keepends=True):
        if re.match(r"^#{1,2} ", line):
            if buf: out[cur] = "".join(buf)
            cur, buf = line.strip(), [line]
        else:
            buf.append(line)
    if buf: out[cur] = "".join(buf)
    return out


persona = read("01_persona/te-IN.md")
psych = read("02_psychology/core.md")
mission = read("04_mission/outbound.md")
business = ""
import json as _j
d = _j.load(open("c:/Users/vishnu/Downloads/bswealthfinance/snapshots/"
                 "wf5_v75_20260823_102630_golden.json", encoding="utf-8"))
def walk(o):
    global business
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "prompt" and isinstance(v, str) and len(v) > len(business): business = v
            walk(v)
    elif isinstance(o, list):
        for x in o: walk(x)
walk(d)

ps = sections(psych)
OBJ_START = "# Objection handling"
keys = list(ps)
oi = keys.index(OBJ_START)
# "Reading the call" begins the tail that must always stay.
ri = next(i for i, k in enumerate(keys) if k.startswith("# Reading the call"))
psych_core = "".join(ps[k] for k in keys[:oi])
psych_obj = "".join(ps[k] for k in keys[oi:ri])
psych_tail = "".join(ps[k] for k in keys[ri:])

pers = sections(persona)
pk = list(pers)
# Voice/format rules are needed on every turn; the long "bookish Telugu" corpus
# of examples is the bulky part.
pers_bulk = next((k for k in pk if "Bookish" in k), None)
persona_lean = "".join(pers[k] for k in pk if k != pers_bulk)

def join(*parts): return "\n\n".join(p for p in parts if p)

TIERS = {
    "A full (ship today)":      join(persona, psych, business, mission),
    "B minus objections":       join(persona, psych_core, psych_tail, business, mission),
    "C lean persona + B":       join(persona_lean, psych_core, psych_tail, business, mission),
    "D core rules only":        join(persona_lean, psych_core, business, mission),
}


def probe(system: str, reps: int = 5) -> dict:
    fbs, fcs, rch = [], [], []
    for _ in range(reps):
        body = {"model": "openai/gpt-oss-120b", "stream": True, "temperature": 0.3,
                "max_completion_tokens": 150, "reasoning_effort": "low",
                "messages": [{"role": "system", "content": system},
                             {"role": "assistant", "content": "MODE: ASK\nసరే, మీ నెల బిల్లు ఎంత వస్తుంది?"},
                             {"role": "user", "content": "5 టు 10,000 వస్తుంది."}]}
        req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                     "User-Agent": "vaani-bench/1.0"}, method="POST")
        t0 = time.perf_counter(); fb = fc = None; r_ = 0
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"): continue
                    p = line[5:].strip()
                    if p == "[DONE]": break
                    if fb is None: fb = time.perf_counter() - t0
                    try: ch = json.loads(p)
                    except json.JSONDecodeError: continue
                    for c in ch.get("choices") or []:
                        dd = c.get("delta") or {}
                        if dd.get("reasoning"): r_ += len(dd["reasoning"])
                        if dd.get("content") and fc is None: fc = time.perf_counter() - t0
        except Exception as e:
            print(f"      err {e!r}"); continue
        if fb and fc: fbs.append(fb); fcs.append(fc); rch.append(r_)
    if not fcs: return {}
    return {"fb": statistics.median(fbs), "fc": statistics.median(fcs),
            "r": statistics.median(rch), "n": len(fcs)}


print(f"layer sizes: persona {len(persona)} (lean {len(persona_lean)}) | "
      f"psych core {len(psych_core)} obj {len(psych_obj)} tail {len(psych_tail)} | "
      f"business {len(business)} | mission {len(mission)}\n")

REPS = 7
labels = list(TIERS)

print("warming the prompt cache for every tier (discarded)...", flush=True)
for label in labels:
    probe(TIERS[label], reps=2)

print(f"interleaving {REPS} rounds so load drift hits all tiers equally...\n", flush=True)
samples = {k: [] for k in labels}
for rnd in range(REPS):
    for label in labels:                       # round-robin, not block
        r = probe(TIERS[label], reps=1)
        if r:
            samples[label].append((r["fb"], r["fc"], r["r"]))

print(f"{'tier':<24} {'chars':>7} {'1st byte':>9} {'content p50':>12} "
      f"{'min':>7} {'max':>7} {'reason':>7} {'n':>3}")
print("-" * 84)
base = None
for label in labels:
    xs = samples[label]
    if not xs:
        print(f"{label:<24} {len(TIERS[label]):>7}  no samples")
        continue
    fcs = sorted(x[1] for x in xs)
    fb = statistics.median(x[0] for x in xs)
    fc = statistics.median(fcs)
    rr = statistics.median(x[2] for x in xs)
    if base is None:
        base = fc
    delta = "" if fc == base else f"  {fc - base:+.3f}s"
    print(f"{label:<24} {len(TIERS[label]):>7} {fb:9.3f} {fc:12.3f} "
          f"{fcs[0]:7.3f} {fcs[-1]:7.3f} {rr:7.0f} {len(xs):>3}{delta}")

spread = [sorted(x[1] for x in samples[l]) for l in labels if samples[l]]
if spread:
    within = statistics.median(s[-1] - s[0] for s in spread)
    across = (max(statistics.median(s) for s in spread)
              - min(statistics.median(s) for s in spread))
    print(f"\nmedian within-tier spread (noise floor): {within:.3f}s")
    print(f"spread across tier medians (the effect):  {across:.3f}s")
    print("=> effect clears the noise floor; prompt size matters."
          if across > within else
          "=> effect is INSIDE the noise floor: prompt size is NOT measurably "
          "driving latency.")
