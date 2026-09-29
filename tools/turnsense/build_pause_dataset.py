#!/usr/bin/env python
"""One row per REAL pause: what STT had said, and whether the caller carried on.

Inputs, both produced offline from the recordings:
    .tmp/turnsense/prep_silero/<call>.json   production's Silero latch + agent lines
    .tmp/turnsense/rt/<call>.json            Soniox stt-rt-v5 text at every stop

The label is what the caller DID, measured from the audio, in the same terms the
replay scores a cut-off (resume within 1.0 s of the verdict; the fast verdict
lands ~0.45 s after the last word):

    HOLD   (0)  he spoke again within 1.5 s of his last word
    SHIFT  (1)  he stayed silent 2.5 s or more, or the call ended
    dropped     1.5-2.5 s: ambiguous, not trained on

One correction, and it is the "హలో" lesson from the logs: when the caller's NEXT
words are a re-prompt ("హలో", "వినపడుతుందా", "ఏమన్నారు"), he was not continuing
a sentence -- he was waiting on us and filling the silence. That pause is a
SHIFT, and labelling it HOLD would teach the model that answering promptly is a
mistake. Counted and reported, not hidden.

Turns: a turn boundary is an agent line starting in the gap, or 2.5 s of
silence. Context for a row is the agent line before the turn's first segment.

    python tools/turnsense/build_pause_dataset.py
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO.parent / "dograh-vapi"))
from api.services.vaani.turnsense import REPROMPTS, _tokens, hold_rate  # noqa: E402

PREP = REPO / ".tmp" / "turnsense" / "prep_silero"
RT = REPO / ".tmp" / "turnsense" / "rt"
FRESH = REPO / ".tmp" / "audio_new" / "caller"
OUT = REPO / ".tmp" / "turnsense" / "pauses.jsonl"

VAD_STOP = 0.2
HOLD_MAX = 1.5
SHIFT_MIN = 2.5


def is_reprompt(text: str) -> bool:
    toks = [t.lower() for t in _tokens(text)]
    if not toks:
        return False
    j = " ".join(toks)
    return j in REPROMPTS or (len(toks) <= 3 and toks[0] in {"హలో", "hello"})


def rows_for(stem: str) -> tuple[list[dict], Counter]:
    c = Counter()
    rt = json.loads((RT / f"{stem}.json").read_text(encoding="utf-8"))
    prep = json.loads((PREP / f"{stem}.json").read_text(encoding="utf-8"))
    segs = rt.get("segments") or []
    agent = sorted(prep.get("agent") or [])
    if rt.get("err"):
        c["call_err"] += 1
    rows = []
    turn_start = 0
    for k, s in enumerate(segs):
        onset, quiet, text = s["onset"], s["quiet"], (s.get("text") or "").strip()
        e = quiet - VAD_STOP
        # turn boundary before this segment?
        if k > 0:
            pq = segs[k - 1]["quiet"] - VAD_STOP
            agent_between = any(pq < a[0] < onset for a in agent)
            if agent_between or onset - pq >= SHIFT_MIN:
                turn_start = k
        nxt = segs[k + 1] if k + 1 < len(segs) else None
        gap = (nxt["onset"] - e) if nxt else None
        if gap is None or gap >= SHIFT_MIN:
            label = 1
        elif gap <= HOLD_MAX:
            label = 0
            if nxt and is_reprompt(nxt.get("text", "")):
                label = 1
                c["reprompt_relabel"] += 1
        else:
            c["dropped_ambiguous"] += 1
            continue
        if not text:
            c["empty_text"] += 1
            continue
        # His habit so far: only pauses whose outcome had happened by now.
        prev_gaps = [segs[j + 1]["onset"] - (segs[j]["quiet"] - VAD_STOP)
                     for j in range(k)]
        holds = sum(1 for g in prev_gaps if g <= HOLD_MAX)
        first_onset = segs[turn_start]["onset"]
        ctx = [a for a in agent if a[0] <= first_onset]
        turn_txt = " ".join(x.get("text", "").strip()
                            for x in segs[turn_start:k + 1] if x.get("text"))
        rows.append({
            "call": stem, "wf": int(stem.split("_run")[0][2:]), "k": k,
            "agent": ctx[-1][1] if ctx else "", "turn": turn_txt, "last": text,
            "seg_secs": max(0.0, quiet - onset - VAD_STOP),
            "n_segs": k - turn_start + 1, "gap": gap, "label": label,
            "caller_hold_rate": hold_rate(holds, len(prev_gaps)),
            "caller_pauses": len(prev_gaps),
        })
        c["rows"] += 1
        c[f"label_{label}"] += 1
    return rows, c


def main() -> int:
    fresh = {w.stem for w in FRESH.glob("*.wav")}
    stems = sorted(p.stem for p in RT.glob("*.json") if (PREP / p.name).exists())
    tot = Counter()
    rows = []
    for st in stems:
        r, c = rows_for(st)
        tot += c
        for x in r:
            x["split"] = "test" if st in fresh else None
        rows.extend(r)
    train_calls = sorted({x["call"] for x in rows if x["split"] is None})
    random.Random(29).shuffle(train_calls)
    val = set(train_calls[: max(1, int(len(train_calls) * 0.15))])
    for x in rows:
        if x["split"] is None:
            x["split"] = "val" if x["call"] in val else "train"
    OUT.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
                   encoding="utf-8")
    by = Counter((x["split"], x["label"]) for x in rows)
    print(f"calls {len(stems)} (test {len(fresh & set(stems))})  rows {len(rows)}")
    for sp in ("train", "val", "test"):
        print(f"  {sp:5}  HOLD {by[(sp, 0)]:5}  SHIFT {by[(sp, 1)]:5}")
    print("  " + ", ".join(f"{k} {v}" for k, v in sorted(tot.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
