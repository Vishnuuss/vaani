#!/usr/bin/env python
"""Mine the backchannel lexicon from what callers actually said.

Why this exists
---------------
`barge_in.BACKCHANNELS` was written by hand from memory of Telugu phone
manners. That is a guess, and a guess in this particular set is expensive in
both directions: a word wrongly IN the set means the caller cannot interrupt
with it, and a word wrongly OUT means the bot gets cut off by someone who was
only saying "go on".

2,097 run logs hold every `rtf-user-transcription` this project has ever
produced. The words are there. There is no reason to guess.

What it measures, and the one thing it cannot
----------------------------------------------
For every short caller utterance it counts:

  n            how often it was said at all
  answered     how often it directly followed a bot QUESTION

`answered` is the discriminator that matters. "సరే" after *"shall we book a
survey?"* is the answer yes -- putting it in BACKCHANNELS would make the agent
deaf to a booking. The same word said in the middle of an explanation is a
listener noise. A word that lands on questions most of the time does NOT belong
in the set, however backchannel-ish it sounds.

What this cannot see: whether the caller was speaking OVER the bot. The
transcript has no overlap information -- that lives in the audio, and
`tools/label_backchannels.py` reads it from the two legs. The two are meant to
be read together.

    python tools/mine_backchannels.py [--max-words 3] [--min-count 3]
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO.parent / "dograh-vapi"))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from api.services.vaani.barge_in import (  # noqa: E402
    BACKCHANNELS, INTERRUPT_WORDS, normalise)

# A bot line ending in one of these was a question. Telugu marks questions with
# a verb suffix far more often than with "?", and Sarvam's punctuation is not
# reliable, so both are checked.
_Q_SUFFIXES = ("ా", "ారా", "ందా", "దా", "రా", "చ్చా", "ారు", "ేది", "ంటే")


def _is_question(text: str) -> bool:
    t = (text or "").strip().rstrip("।. ")
    if not t:
        return False
    if t.endswith("?"):
        return True
    last = t.split()[-1] if t.split() else ""
    return last.endswith(_Q_SUFFIXES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-words", type=int, default=3)
    ap.add_argument("--min-count", type=int, default=3)
    a = ap.parse_args()

    said = Counter()
    after_q = Counter()
    runs = 0

    for p in sorted(glob.glob(str(REPO / ".tmp" / "harvest" / "runs" / "*.json"))):
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            continue
        events = ((d.get("logs") or {}).get("realtime_feedback_events")) or []
        if not isinstance(events, list):
            continue
        runs += 1
        last_bot = ""
        for e in events:
            if not isinstance(e, dict):
                continue
            kind = e.get("type")
            payload = e.get("payload") or {}
            if kind == "rtf-bot-text":
                last_bot = payload.get("text") or ""
            elif kind == "rtf-user-transcription" and payload.get("final"):
                norm = normalise(payload.get("text") or "")
                if not norm or len(norm.split()) > a.max_words:
                    continue
                said[norm] += 1
                if _is_question(last_bot):
                    after_q[norm] += 1

    print(f"{runs} runs, {sum(said.values())} short caller utterances, "
          f"{len(said)} distinct\n")

    rows = [(w, n, after_q[w]) for w, n in said.items() if n >= a.min_count]
    rows.sort(key=lambda r: -r[1])

    known = {w for w in said if all(t in BACKCHANNELS for t in w.split())}
    print(f"{'utterance':28} {'n':>5} {'after a question':>17} {'status':>12}")
    print("-" * 68)
    for w, n, q in rows[:60]:
        frac = q / n
        if w in known:
            status = "in lexicon"
        elif any(t in INTERRUPT_WORDS for t in w.split()):
            status = "interrupt"
        elif frac >= 0.5:
            status = "ANSWER"
        else:
            status = "CANDIDATE"
        print(f"{w[:28]:28} {n:>5} {q:>6} ({frac*100:>4.0f}%) {status:>12}")

    print("\nCANDIDATE = short, common, and mostly NOT said in answer to a")
    print("question. Those are the words worth adding. ANSWER = the caller was")
    print("replying; adding it would make the agent deaf to a real answer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
