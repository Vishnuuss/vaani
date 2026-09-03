"""A/B two prompt versions against Groq, interleaved, on a REALISTIC turn.

Why this file exists and `ttft_probe.py` was not enough
-------------------------------------------------------
`ttft_probe.py` compares MODELS. It sends a two-message toy conversation and a
warmed cache. On 31 Aug it was used to compare prompt VERSIONS and reported a
mean difference of +0.003s between 34,409 and 114,909 characters, which was
read as "prompt size is free". Run 336 then measured the LLM at 1.884s against
0.337s in production.

The toy conversation was the error. A reasoning model's cost scales with how
much it has to reconcile, and with two messages there is nothing to reconcile.
Re-measured with eight turns of real history and a real state block, the same
two prompts came out 1.070s and 2.099s.

So this bench carries:

  * eight turns of Telugu history, taken from the shape of a real call
  * the trailing state block, which is what `brain_processor._refresh` appends
    and is the UNCACHED part -- every character of it is re-read every turn
  * interleaving, because Groq's load varies by more between minutes than these
    prompts differ from each other
  * `first_speakable`, not first byte: the first byte on this model is the
    start of hidden reasoning, which the caller cannot hear

    python bench/ab_layers.py --reps 16
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
LAYER1 = "api/services/vaani/layers/01_persona/te-IN.md"
LAYER2 = "api/services/vaani/layers/02_psychology/core.md"
LAYER4 = "api/services/vaani/layers/04_mission/outbound.md"

env = {}
for p in (ROOT / ".env", Path("c:/Users/vishnu/Downloads/bswealthfinance/.env")):
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
KEY = os.environ.get("GROQ_API_KEY") or env.get("GROQ_API_KEY")
if not KEY:
    sys.exit("GROQ_API_KEY missing")

MODEL = "openai/gpt-oss-120b"

# Layer 3 stands in for the per-client business layer. It is identical in both
# arms, so its exact content does not affect the comparison -- only its size,
# which has to be realistic because it is most of the cached prefix.
LAYER3 = """# Layer 3 - Business: MB Solar Hub

MB Solar Hub installs rooftop solar for homes, shops and small factories across
Andhra Pradesh and Telangana. The visit being offered is a free site assessment:
an engineer comes to the property, looks at the roof and the last bill, and
gives a written figure. It takes about thirty minutes and costs nothing.

## Words real customers use for this
కరెంట్ బిల్లు, సోలార్ ప్యానెల్స్, రూఫ్, షెడ్, సబ్సిడీ, ఇన్‌స్టాలేషన్, యూనిట్లు.

## Answers you may give
- A typical home system pays for itself in about four to five years.
- The panels carry the manufacturer's warranty; our team confirms the years.
- Government subsidy exists for domestic connections; the amount depends on the
  sanctioned load, and our team confirms the figure for the customer's case.
- Where the number came from: the customer enquired through our website form.

## Never
Never quote a final price on the phone. Never promise an installation date.
"""

HISTORY = [
    {"role": "assistant", "content": "MODE: ASK\nనమస్కారం సార్, నేను MB Solar Hub నుంచి ప్రియ మాట్లాడుతున్నాను. మీరు మా వెబ్‌సైట్‌లో సోలార్ గురించి ఎంక్వైరీ చేశారు కదా?"},
    {"role": "user", "content": "అవునండి చేశాను."},
    {"role": "assistant", "content": "MODE: ASK\nథాంక్యూ సార్. మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేక షాప్ లాంటి కమర్షియల్ ప్లేసా?"},
    {"role": "user", "content": "సొంత ఇల్లే అండి, కింద ఒక చిన్న షాప్ కూడా ఉంది."},
    {"role": "assistant", "content": "MODE: ASK\nసరే సార్. మీ నెలవారీ కరెంట్ బిల్లు ఎంత వస్తుంది?"},
    {"role": "user", "content": "అది వచ్చేసి ఒక్కోసారి ఎక్కువ ఒక్కోసారి తక్కువ."},
    {"role": "assistant", "content": "MODE: ASK\nసరే, సుమారుగా ఎంత అనుకుంటున్నారు సార్?"},
    {"role": "user", "content": "నెలకి లక్ష దాటుతుంది సార్, షాప్ కూడా కలిపి. ఇది చాలా ఖరీదు అవుతుందేమో అని భయం."},
]

STATE_BASE = """PHASE: qualifying
KNOWN: {'property_type': 'own house with shop'}
NOT TOLD YET: ['monthly_bill', 'location', 'roof_available', 'customer_name'] -- never state or imply one.
STILL_NEED: ['monthly_bill', 'location', 'roof_available', 'customer_name']
ASK THIS, IN THESE EXACT WORDS: "మీ నెలవారీ కరెంట్ బిల్లు ఎంత వస్తుంది?" -- do not reword it and do not drop the options.
ALREADY SAID: 'సరే, సుమారుగా ఎంత అనుకుంటున్నారు సార్?' -- do not repeat it; say you could not hear.
THEY SAID: 'నెలకి లక్ష దాటుతుంది సార్, షాప్ కూడా కలిపి...' -- open with two words (సరే / మంచిది), then ask."""

MODE_PROTOCOL = """

OUTPUT FORMAT -- the first line of your reply is always exactly one of:
MODE: ASK / MODE: CLOSE / MODE: END"""


def git_show(rev: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT,
                          capture_output=True, check=True).stdout.decode("utf-8")


def build(layer1: str, layer2: str, layer4: str) -> str:
    return "\n\n".join([layer1.strip(), layer2.strip(), LAYER3.strip(),
                        layer4.strip()])


def stream(system: str, state: str) -> dict:
    messages = ([{"role": "system", "content": system}] + HISTORY
                + [{"role": "system", "content": state + MODE_PROTOCOL}])
    body = {"model": MODEL, "messages": messages, "stream": True,
            "temperature": 0.3, "max_completion_tokens": 200,
            "reasoning_effort": "low"}
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode(),
        # Groq answers 403 to a request with no User-Agent. An hour was spent
        # on that once; it is not an auth problem and the key is fine.
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json",
                 "User-Agent": "vaani-bench/1.0"},
        method="POST")
    t0 = time.perf_counter()
    first_speakable = None
    text = ""
    reasoning = 0
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("reasoning"):
                        reasoning += len(d["reasoning"])
                    piece = d.get("content")
                    if not piece:
                        continue
                    text += piece
                    if first_speakable is None:
                        s = text.lstrip()
                        if s.upper().startswith("MODE:"):
                            if "\n" in s:
                                first_speakable = time.perf_counter() - t0
                        elif len(s) >= 4:
                            first_speakable = time.perf_counter() - t0
    except Exception as e:
        return {"error": repr(e)}
    return {"first_speakable": first_speakable, "reasoning": reasoning,
            "text": text[:200]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=16)
    ap.add_argument("--base", default="HEAD",
                    help="git revision the 'before' arm reads its layers from")
    a = ap.parse_args()

    before_sys = build(git_show(a.base, LAYER1), git_show(a.base, LAYER2),
                       git_show(a.base, LAYER4))
    after_sys = build((ROOT / LAYER1).read_text(encoding="utf-8"),
                      (ROOT / LAYER2).read_text(encoding="utf-8"),
                      (ROOT / LAYER4).read_text(encoding="utf-8"))

    # The 'after' arm pays for the COACH line too. Two cues fire on this
    # utterance -- the caller named a large number AND said it sounds
    # expensive -- so this is the worst case, not the average one.
    from api.services.vaani.coach import coach
    coach_lines = coach(HISTORY[-1]["content"])
    after_state = "\n".join([STATE_BASE] + coach_lines)

    arms = [("before", before_sys, STATE_BASE), ("after", after_sys, after_state)]
    for name, sysp, st in arms:
        print(f"{name:<7} system {len(sysp):>7,} chars   state block {len(st):>4} chars")
    print(f"        coach lines fired: {len(coach_lines)}")
    print()

    got = {"before": [], "after": []}
    reason = {"before": [], "after": []}
    for i in range(a.reps):
        # Interleave, and alternate the order so neither arm always follows
        # the other into a warm or a cold moment.
        order = arms if i % 2 == 0 else arms[::-1]
        for name, sysp, st in order:
            r = stream(sysp, st)
            if r.get("error") or not r.get("first_speakable"):
                print(f"  rep {i} {name}: {r.get('error', 'no speakable token')}")
                continue
            got[name].append(r["first_speakable"])
            reason[name].append(r["reasoning"])
        print(f"  rep {i+1}/{a.reps}", end="\r", flush=True)
    print(" " * 30, end="\r")

    print(f"{'arm':<8} {'n':>3} {'mean':>8} {'median':>8} {'p90':>8} {'reason ch':>10}")
    print("-" * 50)
    stats = {}
    for name in ("before", "after"):
        v = sorted(got[name])
        if not v:
            print(f"{name:<8} no completed requests")
            continue
        stats[name] = statistics.mean(v)
        p90 = v[min(len(v) - 1, int(0.9 * len(v)))]
        print(f"{name:<8} {len(v):>3} {statistics.mean(v):8.3f} "
              f"{statistics.median(v):8.3f} {p90:8.3f} "
              f"{statistics.mean(reason[name]):10.0f}")
    if len(stats) == 2:
        d = stats["after"] - stats["before"]
        print(f"\ndelta: {d:+.3f}s  ({d / stats['before'] * 100:+.1f}%)")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
