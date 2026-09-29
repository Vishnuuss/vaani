#!/usr/bin/env python
"""Grammar labels from a big model, for the part of the decision the words carry.

The behavioural pauses (build_pause_dataset.py) are the ground truth, but there
are only ~2,600 of them, and a character model learning Telugu clause endings
from 400 "he carried on" examples will not see most endings. The run logs hold
~3,100 real caller utterances with the agent line before each. Each is shown
whole and cut at a random word boundary, and gpt-oss-120b judges from the words
alone: would a listener reply now, or wait for the rest?

These labels NEVER decide anything on their own. They train a text-only score
that the behavioural model takes as one input and learns how far to trust
(train.py --teacher). The 145 replay calls are excluded, as everywhere else.

    python tools/turnsense/teacher_label.py
"""
from __future__ import annotations

import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "turnsense"))
import build_dataset as BD  # noqa: E402  (log parsing + held-out set)

OUT = REPO / ".tmp" / "turnsense" / "teacher.jsonl"
URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"
BATCH = 20

INSTR = """You label Telugu phone-call transcripts for an end-of-turn detector.
For each item: the AGENT said something, then the CALLER said the TEXT and paused.
Judge ONLY from the words whether the caller's thought is COMPLETE (a listener
would reply now) or INCOMPLETE (the sentence or thought is visibly unfinished and
a listener would wait: a dangling number without its unit, a connective like
కానీ/అంటే/మరి at the end, a hesitation like ఆ/ఉమ్/Uh, a postposition or
non-finite verb ending, a trailing dash, "మాది." when asked an amount, etc.).
A short answer that fully answers the agent's question is COMPLETE.
A greeting, a question back to the agent, or "హలో" is COMPLETE.
Return ONLY JSON: {"labels": [{"i": <index>, "complete": true|false, "conf": 0-100}, ...]}"""


def env(k: str) -> str:
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def items() -> list[dict]:
    hold = BD.held_out()
    rng = random.Random(11)
    out = []
    for p in sorted(BD.RUNS.glob("*.json")):
        try:
            wf, rid = (int(x) for x in p.stem.split("_"))
        except ValueError:
            continue
        if (wf, rid) in hold:
            continue
        try:
            run = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        finals, agent = BD.events(run)
        for f in finals:
            ctx = [a for a in agent if a["start"] <= f["start"]]
            a_txt = ctx[-1]["text"][-160:] if ctx else ""
            for line in [x.strip() for x in f["text"].split("\n") if x.strip()]:
                out.append({"call": f"wf{wf}_run{rid}", "agent": a_txt,
                            "text": line, "kind": "whole"})
                words = line.split()
                if len(words) >= 3:
                    cut = rng.randint(1, len(words) - 1)
                    out.append({"call": f"wf{wf}_run{rid}", "agent": a_txt,
                                "text": " ".join(words[:cut]), "kind": "prefix"})
    seen, uniq = set(), []
    for x in out:
        key = (x["agent"][-60:], x["text"])
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq


def label_batch(batch: list[dict], key: str) -> list[dict]:
    lines = [f'{i}. AGENT: {b["agent"]}\n   TEXT: {b["text"]}' for i, b in enumerate(batch)]
    body = {"model": MODEL, "reasoning_effort": "medium", "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": INSTR},
                         {"role": "user", "content": "\n".join(lines)}]}
    for attempt in range(5):
        try:
            r = requests.post(URL, headers={"Authorization": f"Bearer {key}"},
                              json=body, timeout=120)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            j = json.loads(r.json()["choices"][0]["message"]["content"])
            got = {int(x["i"]): x for x in j.get("labels", [])}
            res = []
            for i, b in enumerate(batch):
                if i in got:
                    res.append({**b, "complete": bool(got[i]["complete"]),
                                "conf": float(got[i].get("conf", 50))})
            return res
        except (requests.RequestException, ValueError, KeyError, TypeError):
            time.sleep(3)
    return []


def main() -> int:
    key = env("GROQ_API_KEY")
    its = items()
    done = set()
    if OUT.exists():
        for x in OUT.read_text(encoding="utf-8").splitlines():
            if x:
                d = json.loads(x)
                done.add((d["agent"][-60:], d["text"]))
    todo = [x for x in its if (x["agent"][-60:], x["text"]) not in done]
    print(f"{len(its)} items, {len(todo)} to label", flush=True)
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    n = 0
    with OUT.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(lambda b: label_batch(b, key), batches):
            for x in res:
                fh.write(json.dumps(x, ensure_ascii=False) + "\n")
            n += len(res)
            if n and n % 500 < BATCH:
                print(f"  labelled {n}", flush=True)
    print(f"labelled {n} this run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
