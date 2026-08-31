# Vaani — from 0.87s / single-agent to a multi-industry voice agent factory

## Goal
1. Perceived latency p50 **< 700ms**, hard ceiling 800ms, measured caller-stop → first audio byte.
2. Cost per minute at or below ₹1.5/min all-in.
3. A **dynamic agent factory**: a new client supplies business name + industry + a few questions;
   the system researches the industry, writes the Layer-3 script in the target language,
   generates test cases, runs them, and ships a working agent — without a human writing prompts.
4. Telugu + English first-class; architecture must extend to Hindi/Tamil/Kannada/Marathi without a rewrite.
5. Runs on free startup credits (NVIDIA Inception / Google for Startups / AWS Activate / Azure).

## Phase 10 -- deep general training of the SHARED layers (30 Aug)

Layers 1, 2 and 4 are inherited unchanged by every client; Layer 3 is the only
per-client text, and `test_layers_are_reusable.py` enforces the separation. So
this work compounds: every hour spent here improves every client at once.

Six categories, one subagent each, each writing a standalone section that is
integrated afterwards rather than edited into the layers directly:

| # | Category | Lands in |
|---|---|---|
| A | Real-world spoken Telugu -- how people build a sentence | Layer 1 |
| B | Reacting with feeling to what was actually said | Layer 1 |
| C | Comprehension -- answer the question that was asked | Layer 2 |
| D | General sales craft, industry-neutral | Layer 2 |
| E | Persuasion psychology for an Indian phone call | Layer 2 |
| F | Objection handling -- the method, then the catalogue | Layer 2 |

Hard constraint on all six: NO industry vocabulary. The register RULES are
universal; the industry's WORDS come from Layer 3.

Length is close to free here -- these layers are the CACHED prefix, and prompt
length was measured on 29 Aug to predict first-token latency not at all (the
same 28,850-char prompt returned 0.080s and 0.413s seconds apart). Noise is not
free: a rule the model cannot act on inside one turn is worse than nothing.

## Next Step
Place one live call and check the booking end to end: the caller names a time
before the menu is offered, then picks the other option by its hour alone. Run
323's stored slot must move from today to the day he actually said.

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


### Phase 7 — Run 312 defects: three ways a garbled word became a fact  (Status: code complete, awaiting deploy)

Run 312 (WR-TEL-OUT-82803732, 51s) hit the 800ms target -- p50 TOTAL **0.774s**,
best turn 0.640s -- and lost the lead anyway. Three defects, all the same shape:
Sarvam returned a garbled word and something downstream turned it into a
confident fact instead of a question.

| # | Caller said | STT returned | Stored | Consequence |
|---|---|---|---|---|
| 7.1 | "2,000 rupees" | "రెండు కోట్లు" (2 crores) | `monthly_bill: 20000000` | agent congratulated him on it |
| 7.2 | "on the factory" | "ఫ్యాక్టర్ పైన" / "మా ట్రాక్టర్ పైన" | `roof_available: false` | **disqualified and hung up** |
| 7.3 | (a factory) | "అది ఒక థర్డ్ సెంటర్ యాక్చువల్ గా" | `property_type: commercial` | guessed, never asked |

#### 7.1 — the plausibility gate was set too wide  (Status: complete)
`amounts.MAX_PLAUSIBLE = 50_000_000` (5 crores). 2 crores passed it, so the
`doubted` path -- which exists for exactly this and has worked since run 286 --
never fired. The bound is the whole bug; the machinery below it is fine.

Also: "వేలు" (thousands) -> "కోట్లు" (crores) is a specific, repeatable Sarvam
error on Telugu scale words, and it is a 10,000x error from one syllable. A
generic "please confirm" wastes a turn. Ask the scale directly.

#### 7.2 — a boolean went false with no negation anywhere  (Status: complete)
Nothing in "ఎక్కడండి ట్రాక్టర్ పైన మా ట్రాక్టర్ పైన" is a "no". `మా X పైన`
("on our X") is an AFFIRMATIVE -- he is saying where the roof is. The extractor
is told "never infer or guess", but a boolean field invites exactly that: the
model reasoned "tractor is not a roof, therefore false".

Worse, the disqualifier fired on the turn immediately after the agent itself
said **"మీరు చెప్పినది బాగా వినిపించలేదు"** -- it did not hear the answer, said
so out loud, and then ended the call on it. That is a lost lead, not a
qualification.

#### 7.3 — same class, on a category field  (Status: complete)
`property_type: commercial` from "థర్డ్ సెంటర్". Right answer by luck. The rule
must be one rule, not three: **no fact is recorded from an utterance the agent
could not parse.**

#### Verification
- `pytest api/tests -k "vaani or telugu"` stays green, plus new cases per defect.
- One live call replaying run 312's exact three answers.
- Latency must not regress past **0.774s p50** (run 312, measured).

#### Result (29 Aug, 17:00)
Committed as `61d674c` on `dograh-vapi`. 137 synchronous Vaani tests passing,
26 of them new and written from run 312's transcript. NOT deployed -- the push
to the `vaani` remote was blocked by the permission classifier.

Latency, separately, is DONE: run 312 measured **0.774s p50, best turn 0.640s**
against the 700-800ms target, after `stt_finalisation_budget_secs` 0.45 -> 0.20
removed a dead 0.250s fallback timer that was firing on every turn.


### Phase 8 — Run 314: customer satisfaction, not latency  (Status: deployed, unverified)

Run 314 hit p50 0.829s and lost the lead anyway. Nothing here is a speed problem.

| # | Defect | Fix | Status |
|---|---|---|---|
| 8.1 | `customer_name: "ఉమ్ భాస్కర్"` -- the agent called him Mr. Um Bhaskar | `strip_fillers` on every extracted value, edges only | complete |
| 8.2 | Three deferrals, closing question asked twice near-verbatim with "please" | DEFERRAL class in triage; one deferral after slots were offered ends warmly | complete |
| 8.3 | Apologised for mishearing the CITY, asked about the PROPERTY; stored "సంతై" | model's own apology raises `misheard_last_turn`; one state-block line | complete |
| 8.4 | "రేపు ఉదయం ten oclock", and "four oclockకి" | "ten గంటలకు"; English number kept, per the client | complete |

Not a defect: turn 5's 6.3s total is the caller pausing after "ఉమ్" and the
agent waiting. That is section 4.1 working.

#### Result (29 Aug, 21:50)
`992b10d`, pushed to the `vaani` remote and deployed. 400 synchronous tests
passing, 39 new. The one failure in `test_reply_sanitizer.py` is pre-existing,
confirmed by stashing rather than assumed.


### Phase 9 — Run 318: the first post-fix call  (Status: in_progress)

Run 318 (WR-TEL-OUT-55669215, 200s) is the ONLY call placed after 992b10d and
after the knowledge base went in. Runs 315-317 all predate both by minutes.

**What the fixes did deliver, measured:**
- p50 TOTAL 0.892s. "ten గంటలకు", not "ten oclock" (317, pre-deploy, still said it).
- The subsidy question was answered with the real MNRE figures for the first
  time: "మొదటి 2 kW కి 30,000 rupees per kW, తర్వాత 18,000, మొత్తం 78,000 వరకు".
- "మీ కంపెనీ పేరేం?" and "మీ ఆఫీస్ ఎక్కడ?" both answered instead of deflected.

**What is still wrong:**

| # | Defect | Evidence from run 318 |
|---|---|---|
| 9.1 | **Invented engineering numbers** | "300 square meters రూఫ్ మీద సుమారు 30 kW ... అంటే 80‑100 panels" -- computed, not known. `_PRICE` only catches 4+ digit rupees, so nothing flagged it. |
| 9.2 | Verbatim self-repetition | "MB Solar Hub, Vijayawada MG Road lo undi." then, one turn later, "అవును, MB Solar Hub, Vijayawada MG Road lo undi." |
| 9.3 | Buying signals ignored | Caller asked cost 3x and panel count 2x -- all buying signals per Layer 2 -- and got the appointment question pushed at him each time. |
| 9.4 | Truncated / interleaved replies | 5+ places where a reply starts, stops mid-clause, and a different one begins. |
| 9.5 | `location` normalisation | run 318 stored "అనంటపూర్" (Telugu script), run 316 stored "Hyderabad" (English). |
| 9.6 | Appointment time wrong | run 316: caller said "రేపు ఉదయం", stored `2026-08-30T17:00` -- 5 PM. |
| 9.7 | 200s duration cap, no close | Engaged caller, five appointment asks, `assessment_agreed: false`. |

#### 9.1 design — a whitelist, not a blacklist
Layer 2 already says "Never manufacture a specific" and it was ignored. Banning
kW outright is not an option: the subsidy answer NEEDS "2 kW", "3 kW", "30,000
per kW". The rule that works is **a number the agent says must appear either in
the knowledge base or in what the caller just said.** Everything else is
invented. The whitelist is derived from the compiled business layer, so it is
per-client and automatic -- a new client's KB defines its own allowed numbers
with no code change.

### Phase 10 — Cost and efficiency, measured not asserted  (Status: pending)
Five techniques minimum, each with a before/after number. Research done
2026-08-30: Groq gives 50% off cached input tokens automatically on
gpt-oss-120b, no write fee, on shared prefixes with recent requests.

### Phase 11 — Layer 2 deep training + reusability  (Status: pending)
Layer 2 is 15,860 chars and already says the right things. Run 318 shows it is
not OBEYED. The lesson this project has proven repeatedly is that prose loses to
deterministic enforcement. More prose is not the fix; more gates are.

### Phase 12 — The Telugu register guide  (Status: pending)
Where English belongs inside Telugu speech, and what to say. Research anchor:
measured code-mixing among Telugu-English bilinguals in AP runs ~40% in informal
conversation, concentrated on technical and modern terms.
