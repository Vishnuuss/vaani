#!/usr/bin/env python
"""Build the TurnSense-te dataset: was the caller finished when STT handed us his words?

Why these labels, and not the old ones
--------------------------------------
The Sep-7 root cause was a ONE-CLASS training set: every row of turnstops.jsonl
said `was_turn_end: True`, so the detector learned the deployed system's own
decisions, mistakes included ("ఆగండి మాట్లాడనివ్వట్లేదు" -- "wait, you're not
letting me talk" -- appeared 25 times labelled as a finished turn).

Here the label is BEHAVIOUR, read off what happened next in the real call:

    HOLD   the caller spoke again before the agent's reply began (or within
           0.3 s of it -- he was already mid-breath). He was not finished,
           whatever the agent did.
    SHIFT  the agent started speaking and the caller let it run for at least
           0.8 s. He was finished.
    (drop) everything in between -- a model trained on a guess is a guess.

Each row is one VAD segment's final transcript, i.e. exactly the moment the
live analyzer's `note_text` fires and a decision has to be taken, with:

    agent      the agent's most recent line before the caller started (context:
               "మీ పేరు ఏంటి?" makes "విష్ణు" finished; "ఏ సిటీ?" makes "మాది" not)
    turn       everything the caller has said since that line, this segment last
    text       this segment alone

A final Soniox delivered with internal line breaks ("సంవత్సరాలు, మరి.\\nసొంత
ఇల్లు.") was two VAD segments with the caller continuing in between, so every
line but the last is a HOLD row by construction.

The 145 calls of the fresh audio corpus are the replay TEST set and are
excluded here, so the replay numbers are on calls the model never saw.

    python tools/turnsense/build_dataset.py
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
RUNS = REPO / ".tmp" / "harvest" / "runs"
FRESH = REPO / ".tmp" / "audio_new" / "caller"
OUT = REPO / ".tmp" / "turnsense" / "dataset.jsonl"

HOLD_LEAD = 0.3        # caller restarted no later than 0.3 s after the agent began
SHIFT_LISTEN = 0.8     # agent spoke >= 0.8 s before the caller spoke again
MAX_HOLD_GAP = 2.5     # a restart later than this is a new turn, not a continuation


def _ts(v) -> float:
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def held_out() -> set[tuple[int, int]]:
    out = set()
    for w in FRESH.glob("*.wav"):
        try:
            wf, rid = w.stem.split("_run")
            out.add((int(wf[2:]), int(rid)))
        except ValueError:
            pass
    return out


def events(run: dict):
    ev = (run.get("logs") or {}).get("realtime_feedback_events") or []
    if not ev:
        return [], []
    base = _ts(ev[0]["timestamp"])
    finals, agent = [], []
    for e in ev:
        p = e.get("payload") or {}
        try:
            if e.get("type") == "rtf-user-transcription" and p.get("final"):
                text = (p.get("text") or "").strip()
                if not text:
                    continue
                finals.append({
                    "start": _ts(p.get("timestamp") or e["timestamp"]) - base,
                    "arrive": _ts(e["timestamp"]) - base,
                    "text": text})
            elif e.get("type") == "rtf-bot-text" and p.get("text"):
                agent.append({
                    "start": _ts(p.get("timestamp") or e["timestamp"]) - base,
                    "text": str(p["text"]).strip()})
        except (KeyError, ValueError, TypeError):
            continue
    finals.sort(key=lambda f: f["start"])
    agent.sort(key=lambda a: a["start"])
    return finals, agent


def rows_for(wf: int, rid: int, run: dict) -> list[dict]:
    finals, agent = events(run)
    out = []
    for i, f in enumerate(finals):
        prev_agent = [a for a in agent if a["start"] <= f["start"]]
        ctx = prev_agent[-1] if prev_agent else None
        ctx_start = ctx["start"] if ctx else -1e9
        # the caller's turn so far: every final since that agent line began
        turn = [g["text"] for g in finals[:i + 1] if g["start"] > ctx_start]

        nxt_user = finals[i + 1] if i + 1 < len(finals) else None
        nxt_agent = next((a for a in agent if a["start"] > f["arrive"] - 0.05), None)

        label = None
        if nxt_user is not None:
            gap = nxt_user["start"] - f["arrive"]
            before_agent = (nxt_agent is None
                            or nxt_user["start"] <= nxt_agent["start"] + HOLD_LEAD)
            if before_agent and gap <= MAX_HOLD_GAP:
                label = 0                                     # HOLD
        if label is None and nxt_agent is not None:
            if nxt_user is None or nxt_user["start"] >= nxt_agent["start"] + SHIFT_LISTEN:
                label = 1                                     # SHIFT
        if label is None:
            continue

        lines = [x.strip() for x in f["text"].split("\n") if x.strip()]
        base = {"wf": wf, "run": rid, "agent": ctx["text"] if ctx else "",
                "seg_index": i, "n_prev_in_turn": len(turn) - 1}
        prefix = " ".join(turn[:-1])
        # internal line breaks: the caller continued -> HOLD rows
        for k in range(len(lines) - 1):
            so_far = " ".join(filter(None, [prefix] + lines[:k + 1]))
            out.append({**base, "text": lines[k], "turn": so_far, "label": 0,
                        "source": "internal-break"})
        last = lines[-1] if lines else f["text"]
        out.append({**base, "text": last,
                    "turn": " ".join(filter(None, [prefix] + lines)),
                    "label": label, "source": "behaviour"})
    return out


def main() -> int:
    hold = held_out()
    paths = sorted(RUNS.glob("*.json"))
    rows, skipped, runs_used = [], 0, 0
    for p in paths:
        try:
            wf, rid = (int(x) for x in p.stem.split("_"))
        except ValueError:
            continue
        if (wf, rid) in hold:
            skipped += 1
            continue
        try:
            run = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        r = rows_for(wf, rid, run)
        if r:
            runs_used += 1
        rows.extend(r)

    # split BY CALL: 85 / 15, fixed seed
    ids = sorted({(r["wf"], r["run"]) for r in rows})
    random.Random(29).shuffle(ids)
    val = set(ids[: int(len(ids) * 0.15)])
    for r in rows:
        r["split"] = "val" if (r["wf"], r["run"]) in val else "train"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    c = Counter((r["split"], r["label"]) for r in rows)
    print(f"runs read {len(paths)}, held out (replay test set) {skipped}, "
          f"runs with rows {runs_used}")
    print(f"rows {len(rows)}   train HOLD {c[('train', 0)]} SHIFT {c[('train', 1)]}"
          f"   val HOLD {c[('val', 0)]} SHIFT {c[('val', 1)]}")
    print("by source", Counter(r["source"] for r in rows))
    print("by workflow", Counter(r["wf"] for r in rows).most_common(8))
    ex = [r for r in rows if r["label"] == 0 and r["source"] == "behaviour"][:12]
    print("\nHOLD examples:")
    for r in ex:
        print(f"  [{r['agent'][-40:]}]  ->  {r['text'][:60]}")
    ex = [r for r in rows if r["label"] == 1][:12]
    print("\nSHIFT examples:")
    for r in ex:
        print(f"  [{r['agent'][-40:]}]  ->  {r['text'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
