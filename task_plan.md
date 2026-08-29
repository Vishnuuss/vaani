# Vaani — from 0.87s / single-agent to a multi-industry voice agent factory

## Goal
1. Perceived latency p50 **< 700ms**, hard ceiling 800ms, measured caller-stop → first audio byte.
2. Cost per minute at or below ₹1.5/min all-in.
3. A **dynamic agent factory**: a new client supplies business name + industry + a few questions;
   the system researches the industry, writes the Layer-3 script in the target language,
   generates test cases, runs them, and ships a working agent — without a human writing prompts.
4. Telugu + English first-class; architecture must extend to Hindi/Tamil/Kannada/Marathi without a rewrite.
5. Runs on free startup credits (NVIDIA Inception / Google for Startups / AWS Activate / Azure).

## Next Step
Phase 7.3 — make the disqualifier require an explicit negation, then place one
verification call.

## Current Phase
Phase 1

## Ground truth measured 2026-08-27 (run 7, WR-TEL-OUT-84050495)
Raw event timestamps, workflow 2 on vaani.bswealthfinance.com:

| segment | t2 | t3 | t4 | avg |
|---|---|---|---|---|
| final transcript → LLM first token | 0.408 | 0.152 | 0.114 | 0.224 |
| **LLM first token → first audio** | **0.672** | **0.586** | **0.757** | **0.672** |
| perceived (transcript→audio) | 1.084 | 0.743 | 0.872 | 0.900 |

`rtf-latency-measured` reported 2.79 / 2.33 / 2.26 — it starts its clock while the
caller is STILL SPEAKING, so every previous number in this project was inflated by
the caller's own utterance length. Real latency was never 2.4s.

`call_disposition = end_call` → the EndCallBridge works. Confirmed fix, keep.

## Phases

### Phase 1 — Latency to the floor  (Status: in_progress)
- 1.0 DONE Hedged LLM requests: p90 1.132s -> 0.390s measured. Shipped behind
      `llm_hedge` (default 2, set 1 to disable). 11 tests, no regressions.
- 1.5 NEXT Traceroute the three vendors; move any US-served one to an India region
      (research ranks this #1 by ms-saved/risk: 100-250ms, near zero risk).
- 1.1 Fix `rtf-latency-measured` to clock from user-stop, and emit TTS TTFB (currently absent).
- 1.2 Kill the reasoning burn: gpt-oss-120b emits chain-of-thought before the answer.
      Either force `reasoning_effort=low` + `include_reasoning=false`, or move to a
      non-reasoning model. Measure both.
- 1.3 Take MODE off the critical path — spoken text must be token #1, marker last.
- 1.4 Re-measure on a real call. Loop until p50 < 700ms.

### Phase 2 — Model & vendor selection on evidence  (Status: pending)
- 2.1 Bench Telugu TTS: Cartesia vs Sarvam Bulbul vs ElevenLabs Flash v2.5 vs Smallest.ai
      on TTFB, cost/min, and Telugu-English code-switch quality.
- 2.2 Bench STT: Sarvam saarika vs Deepgram nova-3 vs ElevenLabs Scribe on Telugu WER + finalisation lag.
- 2.3 Bench LLM: gpt-oss-120b vs llama-3.3-70b vs kimi-k2 on Groq/Cerebras for TTFT + Telugu instruction following.
- 2.4 Decide the v2 reference stack on numbers, not vendor claims.

### Phase 3 — The Agent Factory  (Status: pending)
- 3.1 Intake schema: business name, industry, geography, language, goal, qualifying questions.
- 3.2 Industry researcher: builds a knowledge pack in the TARGET language.
- 3.3 Script compiler: Layer 3 generated from intake + knowledge pack, into the 4-layer prompt.
- 3.4 Language pack abstraction so a new language is a data file, not code.

### Phase 4 — Eval harness / the loop  (Status: pending)
- 4.1 Simulated-caller harness (LLM plays the lead) — no telephony cost.
- 4.2 Scorecard: extraction accuracy, goal completion, latency, cost, language purity, hallucination.
- 4.3 Auto-generated test cases per agent from the intake.
- 4.4 Regression gate: no deploy if the scorecard drops.

### Phase 5 — Cost & infra on free credits  (Status: pending)
- 5.1 Cost model per minute, per component.
- 5.2 Free-credit programmes: eligibility, GPU hours, what can be self-hosted.
- 5.3 Self-hosting decision: which components move in-house and when.

### Phase 6 — UI  (Status: pending)
- 6.1 Agent factory wizard.
- 6.2 Live latency/cost dashboard fed by the real metrics from 1.1.

## Decisions Made
| # | Decision | Why |
|---|---|---|
| 1 | Keep Pipecat, do not move to LiveKit | LiveKit is Layer 2 like Pipecat; a rewrite buys no latency |
| 2 | Keep the MODE protocol but move it off token #1 | 3 tokens beats a second LLM round-trip; only its POSITION is the problem |
| 3 | Trust raw event timestamps, not `rtf-latency-measured` | The metric includes caller speech duration |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| Optimised PartialResponder/TOKEN-aggregation against an inflated metric | 1 | Metric was wrong; re-derived truth from raw timestamps |


### Phase 7 — Run 312 defects: three ways a garbled word became a fact  (Status: in_progress)

Run 312 (WR-TEL-OUT-82803732, 51s) hit the 800ms target -- p50 TOTAL **0.774s**,
best turn 0.640s -- and lost the lead anyway. Three defects, all the same shape:
Sarvam returned a garbled word and something downstream turned it into a
confident fact instead of a question.

| # | Caller said | STT returned | Stored | Consequence |
|---|---|---|---|---|
| 7.1 | "2,000 rupees" | "రెండు కోట్లు" (2 crores) | `monthly_bill: 20000000` | agent congratulated him on it |
| 7.2 | "on the factory" | "ఫ్యాక్టర్ పైన" / "మా ట్రాక్టర్ పైన" | `roof_available: false` | **disqualified and hung up** |
| 7.3 | (a factory) | "అది ఒక థర్డ్ సెంటర్ యాక్చువల్ గా" | `property_type: commercial` | guessed, never asked |

#### 7.1 — the plausibility gate was set too wide  (Status: pending)
`amounts.MAX_PLAUSIBLE = 50_000_000` (5 crores). 2 crores passed it, so the
`doubted` path -- which exists for exactly this and has worked since run 286 --
never fired. The bound is the whole bug; the machinery below it is fine.

Also: "వేలు" (thousands) -> "కోట్లు" (crores) is a specific, repeatable Sarvam
error on Telugu scale words, and it is a 10,000x error from one syllable. A
generic "please confirm" wastes a turn. Ask the scale directly.

#### 7.2 — a boolean went false with no negation anywhere  (Status: pending)
Nothing in "ఎక్కడండి ట్రాక్టర్ పైన మా ట్రాక్టర్ పైన" is a "no". `మా X పైన`
("on our X") is an AFFIRMATIVE -- he is saying where the roof is. The extractor
is told "never infer or guess", but a boolean field invites exactly that: the
model reasoned "tractor is not a roof, therefore false".

Worse, the disqualifier fired on the turn immediately after the agent itself
said **"మీరు చెప్పినది బాగా వినిపించలేదు"** -- it did not hear the answer, said
so out loud, and then ended the call on it. That is a lost lead, not a
qualification.

#### 7.3 — same class, on a category field  (Status: pending)
`property_type: commercial` from "థర్డ్ సెంటర్". Right answer by luck. The rule
must be one rule, not three: **no fact is recorded from an utterance the agent
could not parse.**

#### Verification
- `pytest api/tests -k "vaani or telugu"` stays green, plus new cases per defect.
- One live call replaying run 312's exact three answers.
- Latency must not regress past **0.774s p50** (run 312, measured).
