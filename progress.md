# Progress log — Vaani

## Session 2026-08-27
- Read run 7 raw events. Found the latency metric measures from caller speech START.
- Real perceived latency: 0.900s avg, not 2.459s.
- Confirmed `call_disposition = end_call` → EndCallBridge works.
- Wrote tools/vaani_runs.py (points at the Vaani server, not old Dograh).
- Created task_plan.md / findings.md / progress.md.

## Session 2026-08-27 (continued)
- Benchmarked Groq: reasoning_effort="none" is REJECTED (400). `low` is the floor.
- The MODE header costs 6ms, not ~100ms. Cancelled the plan to restructure it.
- Prompt-size tiering CANCELLED: a controlled interleaved rerun showed the effect
  (0.339s) sits inside the noise floor (0.786s). The standing "never trim prompts
  for latency" rule is correct; my truncation test had measured cache warmth.
- Found the real enemy: VARIANCE. Same request returns 0.289s-1.450s.
- Built HedgedGroqLLMService. Measured p50 0.517->0.355, p90 1.132->0.390.
- Full suite: baseline 49F/1962P/98E, with changes 49F/1973P/98E. No regressions.
  (All 147 failures are Docker being down - Postgres/Redis/MinIO refused.)
- Two research reports landed: research/vendors.md, research/latency.md.
