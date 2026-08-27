"""Measure what actually matters: time to first SPEAKABLE token.

`ttfb` in the call logs is the first byte of the stream. On a reasoning model
that byte is the start of the chain-of-thought, which the caller cannot hear.
The number that decides whether the call feels fast is when the first word the
TTS can say arrives -- after reasoning, and after the MODE header.

    python bench/ttft_probe.py --reps 5
"""
from __future__ import annotations

import argparse, json, os, statistics, sys, time, urllib.request
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
KEY = os.environ.get("GROQ_API_KEY") or env.get("GROQ_API_KEY")
if not KEY:
    sys.exit("GROQ_API_KEY missing")

MODE_PREFIXES = ("MODE:", "MODE :")


def stream(model: str, messages: list, extra: dict) -> dict:
    """One streamed completion. Returns the timings that matter."""
    body = {"model": model, "messages": messages, "stream": True,
            "temperature": 0.3, "max_completion_tokens": 200, **extra}
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json",
                 "User-Agent": "vaani-bench/1.0"},
        method="POST")
    t0 = time.perf_counter()
    first_byte = first_content = first_speakable = None
    text = ""
    reasoning_chars = 0
    usage = {}
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                if first_byte is None:
                    first_byte = time.perf_counter() - t0
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("reasoning"):
                        reasoning_chars += len(d["reasoning"])
                    piece = d.get("content")
                    if not piece:
                        continue
                    if first_content is None:
                        first_content = time.perf_counter() - t0
                    text += piece
                    if first_speakable is None:
                        # Speakable = past the MODE header. If the model wrote a
                        # MODE line we must wait for its newline; otherwise the
                        # very first content token is already speakable.
                        stripped = text.lstrip()
                        if stripped.upper().startswith(MODE_PREFIXES):
                            if "\n" in stripped:
                                first_speakable = time.perf_counter() - t0
                        elif len(stripped) >= 4:
                            first_speakable = time.perf_counter() - t0
    except Exception as e:
        return {"error": repr(e)}
    return {"first_byte": first_byte, "first_content": first_content,
            "first_speakable": first_speakable,
            "total": time.perf_counter() - t0,
            "reasoning_chars": reasoning_chars, "text": text[:160],
            "usage": usage}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--prompt-file", default="bench/system_prompt.txt")
    a = ap.parse_args()

    pf = ROOT / a.prompt_file
    system = pf.read_text(encoding="utf-8") if pf.exists() else (
        "You are Priya from MB Solar Hub, calling Telugu leads about rooftop solar. "
        "Speak natural Tenglish. Ask one question at a time.\n\n"
        "OUTPUT FORMAT -- the first line of your reply is always exactly one of:\n"
        "MODE: ASK / MODE: CLOSE / MODE: END\n")
    print(f"system prompt: {len(system)} chars"
          f"{' (from ' + a.prompt_file + ')' if pf.exists() else ' (fallback)'}\n")

    messages = [
        {"role": "system", "content": system},
        {"role": "assistant", "content": "MODE: ASK\nసరే, మీ నెల బిల్లు ఎంత వస్తుంది?"},
        {"role": "user", "content": "5 టు 10,000 వస్తుంది."},
    ]

    # (label, model, extra params)
    # Only what this Groq account can actually reach -- llama/kimi are not
    # provisioned here, so benching them would be measuring a 404.
    configs = [
        ("gpt-oss-120b effort=low",   "openai/gpt-oss-120b", {"reasoning_effort": "low"}),
        ("gpt-oss-120b effort=none",  "openai/gpt-oss-120b", {"reasoning_effort": "none"}),
        ("gpt-oss-120b none+hidden",  "openai/gpt-oss-120b", {"reasoning_effort": "none", "reasoning_format": "hidden"}),
        ("gpt-oss-120b eff=medium",   "openai/gpt-oss-120b", {"reasoning_effort": "medium"}),
        ("gpt-oss-20b  effort=none",  "openai/gpt-oss-20b",  {"reasoning_effort": "none"}),
        ("gpt-oss-20b  none+hidden",  "openai/gpt-oss-20b",  {"reasoning_effort": "none", "reasoning_format": "hidden"}),
        ("qwen3.6-27b  no params",    "qwen/qwen3.6-27b",    {}),
        ("qwen3.6-27b  eff=none",     "qwen/qwen3.6-27b",    {"reasoning_effort": "none"}),
        ("qwen3.8-27b  eff=none",     "qwen/qwen3.8-27b",    {"reasoning_effort": "none"}),
    ]

    print(f"{'config':<28} {'1st byte':>9} {'1st content':>12} {'1st SPEAKABLE':>14} "
          f"{'reason ch':>10} {'ok':>4}")
    print("-" * 84)
    results = {}
    for label, model, extra in configs:
        runs = [stream(model, messages, extra) for _ in range(a.reps)]
        good = [r for r in runs if not r.get("error") and r.get("first_speakable")]
        if not good:
            err = runs[0].get("error") or "no speakable token"
            print(f"{label:<28} {'--':>9} {'--':>12} {'--':>14} {'--':>10} "
                  f"{len(good)}/{a.reps}   {str(err)[:70]}")
            continue
        fs = statistics.median(r["first_speakable"] for r in good)
        fb = statistics.median(r["first_byte"] for r in good)
        fc = statistics.median(r["first_content"] for r in good)
        rc = statistics.median(r["reasoning_chars"] for r in good)
        results[label] = fs
        print(f"{label:<28} {fb:9.3f} {fc:12.3f} {fs:14.3f} {rc:10.0f} "
              f"{len(good)}/{a.reps}")
        print(f"{'':<28} -> {good[0]['text'][:110]!r}")

    if results:
        best = min(results, key=results.get)
        print(f"\nfastest to a speakable word: {best}  ({results[best]:.3f}s)")
        base = results.get("gpt-oss-120b effort=low")
        if base:
            print(f"current config is {base:.3f}s -> saving {base - results[best]:.3f}s")


if __name__ == "__main__":
    main()
