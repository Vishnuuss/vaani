"""Grade one call against the defects being fixed, instead of eyeballing it.

Reading a flattened transcript is not reliable enough to drive a fix loop. On
11 September run 887 looked like it repeated four questions; the per-turn event
log showed four of six fields capped at exactly two asks and only two leaking.
The flattened view had merged separate turns and hidden which reply belonged to
which. That mistake cost a round.

So this counts, from `logs.realtime_feedback_events`, which carries a turn
number on every bot and user event:

    ASKS        how many times each configured field was actually asked
    DOUBLES     turns where the bot spoke twice for one caller utterance
    DEAD AIR    endpoints over the threshold, which the caller hears as silence
    CAPTURED    what reached the lead record

    python tools/call_grade.py 889
    python tools/call_grade.py 889 --workflow 2
"""
from __future__ import annotations

import argparse
import json
import re
import sys
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

BASE = (env.get("VAANI_SERVER_API_URL")
        or "https://vaani.bswealthfinance.com").rstrip("/")
KEY = env["VAANI_SERVER_API_KEY"]

# Matches state.py's tokenizer. `\w` is Unicode-aware but matches LETTERS only,
# and every Telugu vowel sign is a combining mark -- splitting on `\W+` shreds
# బిల్లు into fragments and nothing ever matches.
_WORD = re.compile(r"[^\wऀ-෿‌‍]+")


def _tokens(text: str) -> set:
    return {t for t in _WORD.split((text or "").lower()) if len(t) >= 3}


def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n >= 4 and n >= 0.6 * min(len(a), len(b))


def get(path: str):
    r = urllib.request.Request(f"{BASE}{path}", headers={"X-API-Key": KEY})
    with urllib.request.urlopen(r, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def questions_for(workflow: int) -> dict:
    """The client's own spoken questions, read off the live workflow."""
    # `/workflow/{id}` is PUT-only; the questions live on the current draft.
    try:
        wf = get(f"/api/v1/workflow/fetch/{workflow}")
    except Exception:
        return {}
    out = {}
    blob = json.dumps(wf, ensure_ascii=False)
    for m in re.finditer(r'"name"\s*:\s*"([a-z_]+)"[^}]*?"ask"\s*:\s*"([^"]+)"',
                         blob):
        out[m.group(1)] = m.group(2)
    return out


def field_of(said: str, questions: dict) -> str:
    """Which field this sentence asks about. Mirrors CallState.field_asked_in."""
    said_tokens = _tokens(said)
    if not said_tokens or not questions:
        return ""
    per = {n: _tokens(q) for n, q in questions.items() if _tokens(q)}
    shared = {}
    for toks in per.values():
        for t in toks:
            shared[t] = shared.get(t, 0) + 1
    best, best_score = "", 0.0
    for name, toks in per.items():
        distinctive = [t for t in toks if shared[t] == 1]
        if not distinctive:
            continue
        hits = sum(1 for t in distinctive
                   if any(_same_word(t, s) for s in said_tokens))
        score = hits / len(distinctive)
        if hits >= 2 and score > best_score:
            best, best_score = name, score
    return best if best_score >= 0.5 else ""


def grade(workflow: int, rid: int) -> int:
    det = get(f"/api/v1/workflow/{workflow}/runs/{rid}")
    gc = det.get("gathered_context") or {}
    ev = ((det.get("logs") or {}).get("realtime_feedback_events") or [])
    ev.sort(key=lambda e: e.get("timestamp") or "")

    questions = questions_for(workflow)

    bots, users, waits = [], [], []
    for e in ev:
        p = e.get("payload") or {}
        if e.get("type") == "rtf-bot-text":
            bots.append((e.get("turn"), (p.get("text") or "").strip()))
        elif e.get("type") == "rtf-user-transcription":
            users.append((e.get("turn"), (p.get("transcript")
                                          or p.get("text") or "").strip()))
        elif e.get("type") == "rtf-latency-breakdown":
            for k in ("endpoint_secs", "endpoint", "user_stop_to_llm"):
                if isinstance(p.get(k), (int, float)):
                    waits.append(float(p[k]))
                    break

    print(f"=== run {rid}   build {gc.get('build') or '-'}")
    print(f"    disposition {gc.get('call_disposition')}   "
          f"{(det.get('cost_info') or {}).get('call_duration_seconds')}s   "
          f"turns {len(users)}")

    # --- asks per field ---
    asks = {}
    for _turn, text in bots:
        f = field_of(text, questions)
        if f:
            asks[f] = asks.get(f, 0) + 1
    print("\n    ASKS PER FIELD  (cap is 2)")
    if not questions:
        print("      (no spoken questions readable from the workflow)")
    for f, n in sorted(asks.items(), key=lambda kv: -kv[1]):
        print(f"      {'OVER ' if n > 2 else '  ok '} {f:<18} {n}")

    # --- two replies for one caller turn ---
    # A caller utterance that is answered twice is the "it is not listening"
    # complaint. Detected as two bot texts with no caller text between them.
    order = sorted(
        [("bot", e.get("turn"), e.get("timestamp")) for e in ev
         if e.get("type") == "rtf-bot-text"] +
        [("user", e.get("turn"), e.get("timestamp")) for e in ev
         if e.get("type") == "rtf-user-transcription"],
        key=lambda x: x[2] or "")
    doubles, prev = 0, None
    for kind, turn, _ts in order:
        if kind == "bot" and prev == "bot":
            doubles += 1
        prev = kind
    print(f"\n    DOUBLE REPLIES   {doubles}"
          + ("   <-- he hears two answers to one thing" if doubles else "   ok"))

    # --- dead air ---
    if waits:
        over = [w for w in waits if w >= 2.0]
        print(f"\n    DEAD AIR         max {max(waits):.2f}s   "
              f"{len(over)} turn(s) over 2s"
              + ("   <-- nobody waits through this" if over else "   ok"))

    # --- silence he actually sat through ---
    #
    # `rtf-latency-breakdown` only exists for turns that COMPLETED, so a turn
    # that never produced a reply is invisible to the DEAD AIR line above. Run
    # 893 sat silent for 28.6s while he said "hello" seven times and hung up,
    # and this tool called it 0.69s. Measured instead from the wall clock: his
    # last word to the next thing the agent said.
    from datetime import datetime

    def _t(ts):
        try:
            return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        except Exception:
            return None

    # Measured between CONSECUTIVE events of any kind, because a final can
    # arrive long after the words were spoken. Run 893's transcript carries the
    # answer and seven "hello"s in ONE final emitted 28s late; keying off that
    # final reported 4.4s. The gap between the agent's question and the next
    # event in the log is what he actually sat through.
    stamps = [_t(e.get("timestamp")) for e in ev
              if e.get("type") in ("rtf-bot-text", "rtf-user-transcription")]
    stamps = [t for t in stamps if t is not None]
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    if gaps:
        worst = max(gaps)
        bad = [g for g in gaps if g >= 3.0]
        note = "   (includes the agent's own speaking time)"
        print("")
        print("    SILENCE          worst %.1fs   %d gap(s) over 3s%s"
              % (worst, len(bad), note))

    # --- the wait that actually means he was ignored ---
    #
    # The gap above is event-to-event, so it counts the agent TALKING as though
    # it were silence. Run 898 scored "26 gaps over 3s, worst 13.9s" for a call
    # whose longest gaps were the agent explaining solar in Telugu for thirteen
    # seconds, exactly as the caller had asked. That reading nearly sent a fix
    # after a problem that was not there.
    #
    # A caller-to-agent gap is the honest one: he has stopped, nothing is
    # playing, and he is waiting on us.
    waits2, last_user2 = [], None
    for e in ev:
        if e.get("type") == "rtf-user-transcription":
            last_user2 = _t(e.get("timestamp"))
        elif e.get("type") == "rtf-bot-text" and last_user2 is not None:
            t2 = _t(e.get("timestamp"))
            if t2 is not None:
                waits2.append((t2 - last_user2).total_seconds())
            last_user2 = None
    if waits2:
        bad2 = [w for w in waits2 if w >= 3.0]
        print("")
        print("    HIS WAIT         worst %.1fs   %d over 3s%s"
              % (max(waits2), len(bad2),
                 "   <-- he is waiting on us" if bad2 else "   ok"))

    # --- what was captured ---
    fields = {k: v for k, v in (gc.get("extracted_variables") or {}).items()}
    got = [k for k, v in fields.items() if v not in (None, "", [])]
    print(f"\n    CAPTURED         {len(got)}/{len(fields)}  {got or 'NOTHING'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=int)
    ap.add_argument("--workflow", type=int, default=2)
    a = ap.parse_args()
    raise SystemExit(grade(a.workflow, a.run))
