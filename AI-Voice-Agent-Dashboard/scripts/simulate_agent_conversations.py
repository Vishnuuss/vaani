#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Free bulk testing for the Dograh voice agent, using its text-chat API instead of
placing real phone calls. Every conversation here costs zero telephony minutes -
only Groq LLM tokens, which is the real ceiling (see BUDGETING below).

WHAT THIS DOES NOT TEST: text-chat bypasses speech-to-text entirely, so it cannot
catch STT misrecognition (e.g. "home loan" heard as "phone loan") or audio-level
issues (TTS clipping, barge-in, dead air from telephony). It IS the right tool for
everything about the agent's dialogue logic: does it read option lists, does it ask
things outside the five-item script, does it ask two questions at once, does it
handle a customer who gives several facts in one sentence, does it stay in Telugu,
does it leak forbidden topics (interest rate, approval).

USAGE
    export DOGRAH_API_URL=https://voice.bswealthfinance.com
    export DOGRAH_API_KEY=...              # the live workflow's Dograh key
    python3 simulate_agent_conversations.py [--workflow-id 1] [--limit N]

Reads persona utterances from PERSONAS below (extend this list freely - that is
the actual value of this script: more variety, not more repetitions of the same
line). Writes full transcripts + grading to ./harness_results.json and prints a
one-line summary per conversation as it runs.

BUDGETING (as measured live on 2026-07-31, llama-3.3-70b-versatile, ~2,300-token
Telugu prompt set):
  - ~2,900-3,600 tokens per turn (session-create + one reply = ~5,800-7,400 tokens)
  - Groq free tier: 12,000 tokens/minute, 100,000 tokens/day
  - -> about 12-16 two-turn conversations per minute before 429s start, and only
       ~13-17 conversations per day before the daily cap is hit - shared with any
       real calls placed that day.
  - Running "1000 conversations" needs either the Groq Dev Tier (paid, removes the
    daily cap - console.groq.com/settings/billing) or spreading the run across
    many days on the free tier. This script does not attempt to work around that;
    it stops cleanly on a 429/quota error instead of burning the day's budget on
    retries.

GRADING is intentionally cheap regex/keyword checks, not another LLM call (that
would double the token cost of every test). It catches the mechanical faults
reliably (list-reading, forbidden topics, question count) but WILL miss subtler
issues - e.g. a confirmation-question pattern ("...కదా?") that wastes a turn, or
an invented off-script question phrased in a way the keyword list doesn't cover.
Always read a sample of the actual transcripts in harness_results.json; do not
trust "issues: []" alone. Two such misses were found and fixed by hand in the
first run of this harness (2026-07-31): the agent asked "...కదా?" confirmation
questions instead of moving on, and asked about "వార్షిక టర్నోవర్" (annual
turnover) - not one of the five allowed items - when a customer gave loan type,
amount and occupation in a single sentence.
"""

import argparse
import json
import os
import subprocess
import sys
import time

PERSONAS = [
    # (label, what a real Telugu customer might actually say)
    ("code_switch_home_loan", "నాకు home loan కావాలి"),
    ("pure_telugu_personal", "పర్సనల్ లోన్ కావాలి"),
    ("indirect_vehicle", "వాహనం కొనాలని ఉంది, లోన్ కావాలా అని అడుగుతున్నాను"),
    ("question_back", "గోల్డ్ లోన్ ఇస్తారా మీరు?"),
    ("vague_uncertain", "ఏమో నాకు సరిగా తెలియదు"),
    ("deferral", "ఇప్పుడు వద్దు, తర్వాత కాల్ చేయండి"),
    ("identity_question", "మీరు బ్యాంకు నుంచా మాట్లాడుతున్నారు?"),
    ("already_has_loan", "నేను ఇప్పటికే లోన్ తీసుకున్నాను ఒకటి"),
    ("forbidden_topic", "వడ్డీ రేటు ఎంత ఉంటుంది చెప్పండి"),
    ("hearing_issue", "నాకు సరిగా వినపడలేదు, మళ్ళీ చెప్పండి"),
    ("multi_info_at_once", "బిజినెస్ లోన్ కావాలి, యాభై లక్షలు, నేను వ్యాపారం చేస్తాను"),
    ("opt_out", "నాకు లోన్ వద్దు, ఇక కాల్ చేయొద్దు"),
    # Add more personas here for wider coverage - rude/frustrated tone, a customer
    # who interrupts with "wait wait", numbers spoken in English digits, a customer
    # who asks the same question back, silence-then-hello, etc. Do not duplicate an
    # existing phrasing; the value of this script is variety.
]

FORBIDDEN_TOPIC_WORDS = ["వడ్డీ రేటు", "ఆమోదం", "అర్హత", "గ్యారంటీ"]
EXTRA_QUESTION_WORDS = [
    "ఎక్కడ", "ఎప్పుడు", "ఎందుకు", "కొనాలా", "మరమ్మత", "ఏ వాహనం", "ఏ ఇల్లు",
    "టర్నోవర్", "రెవెన్యూ",
]
LOAN_LIST_WORDS = ["వ్యక్తిగత", "ఇంటి లోన్", "వ్యాపార లోన్", "వాహన లోన్"]
CONFIRMATION_SUFFIXES = ["కదా?", "అవునా?", "కదండి?"]


def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60).stdout


def api(base_url: str, api_key: str, method: str, path: str, body=None) -> dict:
    hdr = f'-H "X-API-Key: {api_key}" -H "Content-Type: application/json"'
    if body is not None:
        with open("/tmp/_harness_body.json", "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        cmd = f'curl -s -m 45 -X {method} {hdr} --data-binary @/tmp/_harness_body.json "{base_url}{path}"'
    else:
        cmd = f'curl -s -m 45 -X {method} {hdr} "{base_url}{path}"'
    out = sh(cmd)
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out}


def grade(reply: str) -> list[str]:
    if not reply:
        return ["NO_REPLY"]
    issues = []
    if sum(1 for w in LOAN_LIST_WORDS if w in reply) >= 3:
        issues.append("READ_OPTION_LIST")
    if any(w in reply for w in FORBIDDEN_TOPIC_WORDS) and "మా టీమ్" not in reply:
        issues.append("FORBIDDEN_TOPIC_LEAK")
    if any(w in reply for w in EXTRA_QUESTION_WORDS):
        issues.append("EXTRA_QUESTION")
    if any(reply.rstrip().endswith(s) for s in CONFIRMATION_SUFFIXES):
        issues.append("CONFIRMATION_QUESTION")
    if reply.count("?") + reply.count("？") >= 2:
        issues.append("TWO_QUESTIONS")
    words = len(reply.split())
    if words > 28:
        issues.append(f"TOO_LONG({words}w)")
    if any((not (0x0C00 <= ord(c) <= 0x0C7F)) and (not c.isascii()) and (not c.isspace()) for c in reply):
        issues.append("UNEXPECTED_SCRIPT")
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow-id", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None, help="Only run the first N personas")
    ap.add_argument("--out", default="harness_results.json")
    args = ap.parse_args()

    base_url = os.environ.get("DOGRAH_API_URL")
    api_key = os.environ.get("DOGRAH_API_KEY")
    if not base_url or not api_key:
        print("Set DOGRAH_API_URL and DOGRAH_API_KEY first.", file=sys.stderr)
        sys.exit(1)

    personas = PERSONAS[: args.limit] if args.limit else PERSONAS
    results = []
    total_tokens = 0

    for i, (label, utterance) in enumerate(personas):
        t0 = time.time()
        sess = api(
            base_url, api_key, "POST", f"/api/v1/workflow/{args.workflow_id}/text-chat/sessions",
            {"name": f"harness-{label}", "initial_context": {"phone_number": f"+9190000000{i:02d}", "customer_name": "Sim"}},
        )
        run_id = sess.get("workflow_run_id")
        t_greet = time.time() - t0

        if not run_id:
            print(f"[FAIL] {label}: session create failed -> {sess}")
            results.append({"persona": label, "error": "session_create_failed", "raw": sess})
            continue

        greeting = next(
            (t["assistant_message"]["text"] for t in sess.get("session_data", {}).get("turns", []) if t.get("assistant_message")),
            None,
        )

        t1 = time.time()
        resp = api(
            base_url, api_key, "POST",
            f"/api/v1/workflow/{args.workflow_id}/text-chat/sessions/{run_id}/messages",
            {"text": utterance},
        )
        t_reply = time.time() - t1

        reply = None
        for t in resp.get("session_data", {}).get("turns", []):
            am = t.get("assistant_message")
            if am:
                reply = am.get("text")

        issues = grade(reply or "")

        run_info = api(base_url, api_key, "GET", f"/api/v1/workflow/{args.workflow_id}/runs/{run_id}")
        usage = (run_info.get("usage_info") or {}).get("llm") or {}
        tok = sum(v.get("total_tokens", 0) for v in usage.values())
        total_tokens += tok

        results.append({
            "persona": label, "run_id": run_id, "utterance": utterance,
            "greeting": greeting, "reply": reply,
            "greeting_latency_s": round(t_greet, 2), "reply_latency_s": round(t_reply, 2),
            "tokens": tok, "issues": issues,
        })
        print(f"[{i+1:2d}/{len(personas)}] {label:<24} greet={t_greet:.1f}s reply={t_reply:.1f}s tok={tok} issues={issues or 'OK'}", flush=True)

        # Groq free tier is 12,000 tokens/minute and each conversation here costs
        # ~5,800-7,400 tokens, i.e. only ~2 fit per minute. Without pacing,
        # requests after the 4th-5th conversation start hitting 429 and Dograh
        # retries with backoff, which shows up as growing latency rather than a
        # clean error - measured live: latency crept from 0.8s to 7-12s at that
        # point. 35s keeps a margin under the per-minute cap.
        time.sleep(35)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"results": results, "total_tokens": total_tokens}, f, ensure_ascii=False, indent=1)

    print(f"\nTOTAL TOKENS USED: {total_tokens}")
    print(f"TOTAL CONVERSATIONS: {len(results)}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
