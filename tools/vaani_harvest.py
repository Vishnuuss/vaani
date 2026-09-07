"""Harvest training data from the 2,086 real calls already sitting in production.

STRICTLY READ-ONLY. This issues GETs and nothing else. It never dials, never
writes, never touches a workflow definition. Production is the live business.

Why this exists
---------------
`vaani_dataset.py` found 112 clean turns on the new Vaani server -- far short of
what any training job needs. But production has been running Telugu agents for
months:

    1090  loan qualification      538  solar        215  investment
      93  Shreya loan              67  loan          50  loan (dup)
      26  solar (dup)               7  real estate

Every one of those runs carries a transcript, per-turn events, and -- this is the
part that matters -- SEPARATED user audio at `recordings/<id>/user.wav`. Caller
audio, isolated from the agent, with the transcript beside it.

What each output is for, and its honest limits
----------------------------------------------
`turnstops.jsonl` is the valuable one. A Telugu turn detector does not exist
anywhere: Smart Turn v3 covers 23 languages, LiveKit v1 14, Deepgram Flux 10,
AssemblyAI 4-6, and Telugu is in none of them. Endpointing is ~1.4s of a ~2.3s
turn -- the single largest cost -- so this is worth more than any prompt change.
Crucially it is AGENT-INDEPENDENT: it learns from how Telugu speakers finish a
sentence, so the old agents' own flaws do not contaminate it. Every industry
here is usable.

`sft.jsonl` needs more care. These replies came from a DIFFERENT agent, on a
node-graph prompt, with none of the current fixes -- so they are filtered through
the eval battery's own `hard_rules`, the same function the deploy gate uses. A
turn the gate would reject can never become something a model is taught to
imitate. Even so, treat this as a starting corpus to review, not as ground truth.

`dpo.jsonl` is the free win. Every reply that FAILS `hard_rules` is a labelled
negative that cost nothing to produce, and there are months of them.

    python tools/vaani_harvest.py --survey            # what is there, no download
    python tools/vaani_harvest.py --workflows 5 1 6   # harvest those
    python tools/vaani_harvest.py --audio             # also fetch user.wav

Runs are cached under .tmp/harvest/runs/, so re-running costs nothing and an
interrupted harvest resumes where it stopped.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vaani_eval import hard_rules  # noqa: E402  -- one definition of "clean"

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

env: dict[str, str] = {}
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

PROD_BASE = "https://voice.bswealthfinance.com"
PROD_KEY = env["DOGRAH_API_KEY"]
CACHE = Path(".tmp/harvest/runs")


def get(path: str, timeout: int = 90):
    r = urllib.request.Request(f"{PROD_BASE}{path}", headers={"X-API-Key": PROD_KEY})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def cached_run(wid: int, rid: int) -> dict | None:
    """One run, from disk if we already have it. Production is asked once."""
    path = CACHE / f"{wid}_{rid}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    try:
        d = get(f"/api/v1/workflow/{wid}/runs/{rid}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"    run {rid}: {e!r}")
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return d


def list_runs(wid: int, limit: int) -> list[dict]:
    out, page = [], 1
    while len(out) < limit:
        d = get(f"/api/v1/workflow/{wid}/runs?page={page}&limit=50")
        batch = d.get("runs") or []
        if not batch:
            break
        out += batch
        page += 1
    return out[:limit]


def conversation(run: dict) -> list[dict]:
    """(what the caller said, what the agent replied) pairs, in order."""
    events = (run.get("logs") or {}).get("realtime_feedback_events") or []
    pairs, pending = [], None
    for e in events:
        t, p = e.get("type") or "", e.get("payload") or {}
        if t == "rtf-user-transcription" and p.get("final"):
            pending = (p.get("text") or "").strip()
        elif t == "rtf-bot-text":
            pairs.append({
                "user": pending,
                "bot": (p.get("text") or "").strip(),
                "user_start": p.get("timestamp"),
                "user_end": p.get("end_timestamp"),
            })
            pending = None
    return pairs


def survey(workflows: list[int], limit: int) -> None:
    print(f"{PROD_BASE}  (read-only)\n")
    grand = Counter()
    for wid in workflows:
        runs = list_runs(wid, limit)
        stats = Counter()
        for meta in runs:
            stats["runs"] += 1
            if meta.get("mode") == "textchat":
                stats["textchat"] += 1
        print(f"  workflow {wid}: {stats['runs']} listed "
              f"({stats['textchat']} text chat)")
        grand.update(stats)
    print(f"\n  {grand['runs']} run(s) listed across {len(workflows)} workflow(s)")


def harvest(workflows: list[int], limit: int, out: Path, want_audio: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    sft, dpo, stops, audio = [], [], [], []
    seen_runs = 0
    with_convo = 0

    for wid in workflows:
        metas = list_runs(wid, limit)
        print(f"\nworkflow {wid}: {len(metas)} run(s)")
        for n, meta in enumerate(metas, 1):
            rid = meta.get("id")
            if rid is None:
                continue
            run = cached_run(wid, rid)
            seen_runs += 1
            if n % 50 == 0:
                print(f"    {n}/{len(metas)}  clean={len(sft)} rejected={len(dpo)} "
                      f"utterances={len(stops)}", flush=True)
            if not run:
                continue
            pairs = conversation(run)
            if not pairs:
                continue
            with_convo += 1

            if want_audio and run.get("user_recording_public_url"):
                # The BOT leg is indexed alongside the caller's, because the
                # four-state turn ontology (complete / incomplete / backchannel
                # / wait) cannot be labelled from the caller alone. A
                # backchannel is defined by OVERLAP -- a short sound made while
                # the agent was talking, after which the caller does not take
                # the floor -- and "while the agent was talking" is only
                # knowable from the agent's own track. The URL was in every run
                # log all along and was simply never written down.
                audio.append({"run": rid, "workflow": wid,
                              "url": run["user_recording_public_url"],
                              "bot_url": run.get("bot_recording_public_url")})

            history = []
            for turn in pairs:
                said = turn["user"]
                if said:
                    history.append({"role": "user", "content": said})
                    # Agent-independent: this is how Telugu speakers end a turn.
                    stops.append({"run": rid, "workflow": wid, "text": said,
                                  "start": turn["user_start"],
                                  "end": turn["user_end"],
                                  "was_turn_end": True})
                reply = turn["bot"]
                if not reply:
                    continue
                violations = hard_rules(reply)
                row = {"run": rid, "workflow": wid, "messages": list(history)}
                if violations:
                    dpo.append({**row, "rejected": reply, "chosen": None,
                                "why": violations})
                else:
                    sft.append({**row, "completion": reply})
                history.append({"role": "assistant", "content": reply})

    for name, rows in (("sft", sft), ("dpo", dpo),
                       ("turnstops", stops), ("audio_index", audio)):
        path = out / f"{name}.jsonl"
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8")
        print(f"  {len(rows):>6} -> {path}")

    print(f"\n  {seen_runs} run(s) read, {with_convo} had a real conversation")
    if dpo:
        why = Counter(w for r in dpo for w in r["why"])
        print("\n  what the old agents actually did wrong (free negatives):")
        for reason, count in why.most_common(8):
            print(f"    {count:>5}  {reason}")

    print("\n  readiness:")
    for job, have, need in (("distillation / SFT", len(sft), 500),
                            ("preference tuning", len(dpo), 200),
                            ("Telugu turn detector", len(stops), 500)):
        mark = "READY" if have >= need else f"need ~{need}"
        print(f"    {job:<24} {have:>6}   {mark}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflows", type=int, nargs="*",
                    default=[1, 5, 6, 7, 3, 2, 8, 4])
    ap.add_argument("--limit", type=int, default=1200,
                    help="max runs per workflow")
    ap.add_argument("--out", default=".tmp/harvest")
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--audio", action="store_true",
                    help="also index the separated user recordings")
    a = ap.parse_args()

    if a.survey:
        survey(a.workflows, a.limit)
        return 0
    harvest(a.workflows, a.limit, Path(a.out), a.audio)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
