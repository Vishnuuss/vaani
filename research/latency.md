# Beating the 0.61s LLM Floor — Speculative Execution & Endpointing

Research date: 2026-08-27. Target: Pipecat-based Telugu voice agent, India PSTN.
Every latency claim is labelled **[vendor claim]**, **[independent benchmark]**, or **[docs]**.
Numbers that could not be sourced are listed in "What I could not verify" rather than invented.

## Measured baseline (this repo, not re-researched)

| Stage | Measured |
|---|---|
| Groq gpt-oss-120b, `reasoning_effort=low` — TTFB | 0.194s |
| Groq gpt-oss-120b — time to first **content** token | 0.603s (0.41s CoT burn) |
| `reasoning_effort="none"` | rejected 400 by Groq |
| TTS TTFB + network transit | ~0.26s |
| **Total perceived (caller stops → first audio)** | **~0.87s** |

The 0.61s is not tunable. The only levers left are (A) overlapping it with the caller's own
speech and (B) endpointing sooner/smarter.

---

## Bottom line

**Best achievable perceived latency, given a fixed 0.61s LLM: ~0.55–0.65s server-side
(from 0.87s today), and ~1.05–1.15s as the caller actually hears it on an Indian mobile.**

**The technique that gets us there is speculative LLM execution (Area A), and only that.** It is
the one lever that removes the 0.61s from the critical path instead of trimming around it.
Everything else in this document is worth 50–150ms.

Four things follow from the research, in order of how much they should change the plan:

1. **Speculation is the only lever with the 0.61s in its blast radius — but its ceiling is our own
   silence window, not the LLM.** Speculation starts the LLM during the silence we were going to
   wait through anyway. Recover at most the endpointing wait; recover it *fully* only on turns where
   the speculation was right. **Area A and Area B are not additive** — they spend the same
   milliseconds. Budget them as one line.

2. **Pipecat does not ship this, and neither does anything else we could switch to for Telugu.**
   pipecat-ai#3321 is open and unassigned; Pipecat's own Flux integration has a literal
   `TODO: Implement proper EagerEndOfTurn support with cancellable processing pipeline`. LiveKit's
   shipped version has a known state-corruption defect (livekit/agents#3414) that would be fatal to
   a node-graph agent. The `api/services/pipecat/speculation/` module already in this repo is the
   right architecture — cache the generation, gate it, swallow the trigger frame on a hit, fall
   through on a miss. **This research validates that design rather than replacing it.**

3. **There is no Telugu turn detector in existence.** Smart Turn v3 (23 languages), LiveKit v1
   (14), Deepgram Flux (10), AssemblyAI streaming (4–6) — none includes Telugu. The
   `eager_eot_threshold: 0.5` already sitting in `service_factory.py` is dead code on a Telugu call.
   `latency_budget.yaml` flags this as the largest risk in the budget; this research confirms it and
   cannot resolve it.

4. **Our 250ms endpointing budget and our 2% false-interruption floor cannot both hold.** The only
   published cross-provider curve (LiveKit v1) reaches 10% false cutoffs at 295ms and 5% at 543ms,
   *in languages the model was trained on*. There is no published operating point at 2%. One of
   those two numbers has to move, and it should be decided deliberately rather than discovered on a
   live call.

### Ranked by (ms saved) ÷ (implementation risk)

| # | Technique | Est. saving | Risk | Note |
|---|---|---|---|---|
| **1** | **Traceroute the three vendors; move any US-served one to an India/Singapore region** | **100–250ms** | **near zero** | Not what the question asked, but it dominates everything below on ratio. `latency_budget.yaml` already calls it "an hour of work". Do this first. |
| **2** | **Run `SpeculationProbe` on live calls and get the real hit rate** | 0ms directly | zero (pass-through observer) | The whole Area A case rests on a number nobody in the industry has published for any language. This is the highest-information hour available. Everything below #2 is a bet until this returns. |
| **3** | **Speculative LLM, buffered, gated, never touching flow state** | **up to the full endpointing wait (~200–400ms) on hit turns** | moderate | Already built in `speculation/`. Harden it: cap in-flight speculations at one; copy LiveKit's `max_speech_duration` guard (10s) so long turns don't spray requests; match on a normalised prompt hash rather than exact transcript equality, or the hit rate will be artificially near zero. |
| **4** | **Adaptive endpointing (Vapi's three rules, ported, with Telugu-specific cues)** | 200–400ms on clean turns; mostly buys *quality headroom* from our current flat 0.2s | low–moderate | Start from punctuation 0.1s / trailing number 0.5s / no-punctuation 1.5s. Add Telugu finality morphology (-అండి, -గా, -లేదు, -ఉంది) vs. continuation particles (-అంటే, -మరి, trailing conjunctive participle). The one place where knowing Telugu beats having a bigger model. |
| **5** | **Calibrate the Smart Turn threshold on our own Telugu recordings** | unknown, possibly large | low (offline) | The model has no Telugu, but the threshold is ours. 200 labelled turns gives us our own version of the LiveKit curve and turns the budget's biggest estimate into a measurement. |
| **6** | **Check the VPS instance class and log per-turn detector inference time** | 60–80ms if we're on a burstable core | near zero | Smart Turn v3 is 12ms on a c7a.2xlarge and **94.8ms on a t3.medium**. On a small burstable core the detector eats half the endpointing budget, inconsistently. |
| **7** | **Speculative TTS (LiveKit's `preemptive_tts` pattern: synthesise the speculative reply, buffer the audio, discard on cancel)** | up to 0.26s more on hit turns | **high** | Only after #3 is proven in production. This is the one variant where a cancellation bug becomes *audible*. |
| **8** | Pin the Pipecat version / assert `VAD_STOP_SECS` | 0ms (prevents a 600ms regression) | zero | The default moved 0.8 → 0.2 between releases. A bump in the other direction is a silent 600ms. |
| — | Filler / backchannel audio | negative at 0.87s | — | Reserve for 2s+ tool calls only. See "Techniques that sound good but don't work". |
| — | Pre-synthesising guessed opening words | unmeasurable | — | No production implementation exists anywhere. Do not build. |

---

## Area A — Speculative / predictive execution

### A.0 The one thing to understand first

Every production implementation of this idea is the *same* trick under three different names:

| Name | Who | Fires on |
|---|---|---|
| **Preemptive generation** | LiveKit Agents | a user transcript arriving, before end-of-turn is confirmed |
| **Eager end of turn** | Deepgram Flux | an `EagerEndOfTurn` event (medium-confidence EOT) |
| **Pre-emptive generation** | AssemblyAI (recommended pattern, not a shipped feature) | an immutable partial / `utterance` field |

None of them predicts *what the caller will say*. They all just start the LLM **earlier than the
endpointer would have allowed**, and throw the work away if the caller keeps talking. That
distinction matters for us: the saving is bounded by how much silence-wait we skip, not by the
LLM's 0.61s.

### A.1 Who actually ships it

**LiveKit Agents — `preemptive_generation`. The only mature implementation.**

- "Preemptive generation speculatively starts an LLM response before the user's end of turn is
  confirmed, reducing perceived latency in back-and-forth conversation." **[docs]**
  (docs.livekit.io/agents/build/audio/#preemptive-generation)
- Session reference: "Speculatively begins LLM requests before end-of-turn is detected to reduce
  response latency. **Increases LLM token usage because speculative responses may be discarded.**
  Configured via `turn_handling`. **Default: enabled.**" **[docs]**
- Trigger: "the agent sends inference calls **as soon as a user transcript is received**" — i.e. on
  the STT transcript event, not on a stability heuristic of LiveKit's own. The stability judgement
  is delegated to the STT vendor. **[docs]**
- TTS is *not* speculative by default: "Only the LLM runs preemptively — TTS waits until the turn is
  confirmed. For the lowest possible latency, enable `preemptive_tts` to also run TTS
  speculatively, at the cost of higher wasted compute when the response is discarded." **[docs]**
- Guard rail in the docs: `max_speech_duration` = 10.0s — "skip if user speaks longer than 10s",
  i.e. don't speculate on long rambling turns. **[docs]**
- Known defect worth copying the fix from: **livekit/agents issue #3414**. Because the *whole* reply
  pipeline runs speculatively, "similar transcription results may trigger `on_user_turn_completed`
  and `tts_node` multiple times", corrupting state machines that carry business logic in those
  hooks. The reporter's proposed architecture is the right one for us: **"simply call the large
  model in advance, cache the result, and only go through those node processes when it is confirmed
  that the preemptive generation data will be used."** Separately, **livekit/agents-js issue #773**:
  preemptive generation is **not implemented in the Node SDK at all**. **[docs / issue]**

**Deepgram Flux — `eager_eot_threshold`. The cleanest protocol design.**

Flux is an STT model that emits turn events rather than plain transcripts. Three events matter:

- `EagerEndOfTurn` — medium confidence, "begin processing"
- `TurnResumed` — "**Treat `TurnResumed` as a cancellation signal. Be ready to discard or revise any
  LLM replies in progress.**"
- `EndOfTurn` — high confidence, commit.

Two thresholds: `eot_threshold` (default **0.7**) and an opt-in `eager_eot_threshold` ("Lower values
→ earlier triggers, but more false starts"). EagerEndOfTurn events are **off by default**. **[docs]**

Deepgram's own framing of the payoff is deliberately modest: eager processing can "cut hundreds of
milliseconds" and is "good for trimming that last **100–200ms** of end-to-end latency" **[vendor
claim]**. Flux's headline turn-detection latency is **~260ms**, median EOT detection **under 300ms**,
**p95 1.5s** **[vendor claim]**.

Deepgram's own cost mitigation is also the answer to "does the wasted spend cost more than it
saves": **"Use smaller/faster models for EagerEndOfTurn drafts. Only call the full LLM on
`EndOfTurn`."** **[docs]**

**AssemblyAI — enables it, doesn't ship it.**

AssemblyAI's argument is that speculation is only *safe* if partials never get rewritten: their
Universal-Streaming line emits **immutable** transcripts plus an `utterance` field, which "enable
pre-emptive generation — starting LLM processing before the user finishes speaking", whereas
"traditional mutable partials add complexity". They then tell you to switch on LiveKit's flag rather
than shipping their own orchestration. **[docs]**

Their Pipecat integration for Universal-3.5 Pro Realtime is more interesting to us than the LiveKit
flag (see Area B): EOT is **punctuation-based**, `min_turn_silence` default **100ms**,
`max_turn_silence` default **1000ms**, first partial at **~750ms**, network RTT **~50ms**, STT
processing **~200–300ms**. **[docs]**

**Pipecat — does NOT have this. This is the load-bearing finding.**

pipecat-ai/pipecat issue **#3321, "Preemptive speech generation option for seamless conversation"**,
requests exactly a `preemptive_generation` flag on the Pipeline (default False), LLM firing on the
STT transcript instead of on the VAD/turn-detection stop, plus cancellation when the context
changes. It is **open, unassigned, no PR, no team commentary, no workaround posted**.
**[docs / issue]**

So on Pipecat this is not a config flag. It is something we build — see A.4.

**Vapi / Retell / Bland — no exposed speculative-LLM control.**

I found no documented speculative-generation switch on any of the three. What they expose is
*endpointing* tuning (Area B), which is a different lever. Vapi does let you delegate EOT to
`deepgram-flux` or `assembly` as a `smartEndpointingPlan` provider **[docs]**, so a Vapi user gets
the eager-EOT benefit only indirectly, through the transcriber. Retell's marketed end-to-end figure
is **~600ms** **[vendor claim]**.

**Cartesia** — no speculative-execution feature found. Cartesia's latency story is TTS-side
(streaming synthesis), which we have already accounted for in the 0.26s.

**Academic.** The idea is old in the incremental-dialogue literature (Schlangen & Skantze on
incremental processing; the USC ICT incremental-interpretation work, which showed "relatively high
accuracy … in understanding of spontaneous utterances *before* utterances are completed", and
explicitly motivates "responsive overlap behaviors" — interrupting, acknowledging or completing a
user's utterance mid-flight). The most directly relevant modern paper is **Zink, Higuchi, Mullov,
Waibel & Kobayashi, "Predictive Speech Recognition and End-of-Utterance Detection Towards Spoken
Dialog Systems", arXiv:2409.19990 (submitted 2024-09-30, ICASSP 2025)**: an encoder-decoder ASR
trained with masked future segments, plus a cross-attention EOU detector combining acoustic and
linguistic information, which can **"predict upcoming words and estimate future EOU events up to
300ms prior to the actual EOU."** **[independent — but note the paper quantifies prediction ability,
not end-to-end ms saved; it is a research prototype, not a deployable component, and there is no
Telugu evaluation.]**

### A.2 What actually triggers the speculative fire

Ranked by how well-evidenced each trigger is:

1. **A vendor-emitted medium-confidence EOT event** — Deepgram Flux `EagerEndOfTurn`. Best design:
   the model that owns the acoustics decides, and the same model owns the cancel signal
   (`TurnResumed`).
2. **An immutable partial / transcript arriving** — LiveKit + AssemblyAI. Works *only* because the
   STT guarantees no rewrite. With a mutable-partial STT you are speculating on text that changes
   under you.
3. **A stable-prefix heuristic** — fire when the tail of the partial has not changed for X ms. This
   is the roll-your-own version, and it is what we would have to build on Pipecat. I found **no
   production system that publishes specific N-words / X-ms values** (see "What I could not
   verify").
4. **VAD silence onset** — this is what Pipecat does *today*, and it is not speculation at all; it
   is the normal path.
5. **A semantic-completeness classifier** — used for *endpointing* (Smart Turn, LiveKit turn
   detector) rather than as a speculative trigger. Nobody in this research uses the semantic
   classifier to trigger speculation, largely because the classifier only runs once VAD has already
   stopped, which is too late to buy anything.

### A.3 Hit rates and net saving — claimed vs. actually measured

| Claim | Source | Label |
|---|---|---|
| eager EOT trims "the last **100–200ms**" of end-to-end latency | Deepgram docs | **[vendor claim]** |
| "user-perceived TTFT drops by **150–350ms**"; discarded LLM calls "usually sits below **5%** of turns" | futureagi.com LiveKit-tuning blog | **[vendor claim — third-party blog, no methodology published; treat as folklore]** |
| vanilla LiveKit loop **1.2–1.4s p95** → **500–650ms p95** | same blog | **[vendor claim — attributed to a stack of 12 changes, not to speculation alone]** |
| "correct **80–90%** of the time; **200–400ms** saved when correct, **300–500ms** penalty when wrong" | surfaced only by search-engine summarisation; **no primary source located** | **[unverified — do not quote]** |

**Honest read:** the only number I would plan against is Deepgram's **100–200ms**, because it is
published by the party that built the mechanism and it is conspicuously modest. The 150–350ms and
the 80–90% hit rate could not be traced to any primary source.

**Why the saving is small — and why that is exactly the right way to think about it.**
Speculation does not remove LLM time. It removes *waiting-for-the-endpointer* time, by starting the
LLM during the silence window instead of after it. If our endpointer waits 400ms of silence and we
instead fire at silence onset, we recover at most 400ms — and only when the caller really had
stopped. Our 0.61s does not shrink; it starts 400ms sooner and therefore finishes 400ms sooner.

**The upper bound of Area A for us is our own silence threshold, and not one millisecond more.**

The corollary matters more than the technique: **Area A and Area B are not additive.** Shortening
the silence threshold and speculating into the silence window both consume the same 400ms.
Whichever you implement first, the second one has far less left to give. Do not budget them
separately.

### A.4 Clean cancellation — the part that decides whether this is safe

The failure mode is not latency, it is the caller hearing half of a wrong answer. Three rules, each
supported by a source above:

1. **Never let speculative work reach the TTS.** LiveKit's default is exactly this (`preemptive_tts`
   off) **[docs]**. Buffer the LLM token stream in memory; release it into the TTS only on confirmed
   EOT.
2. **Never let speculative work mutate state.** This is livekit/agents#3414 verbatim: run the model
   call, cache the result, and only run the pipeline nodes (function calls, context updates, field
   extraction, node transitions) once the speculation is confirmed. For a Dograh-style flow agent
   with node transitions and variable extraction, this is not optional — a speculative call that
   fires a node transition twice corrupts the call.
3. **Cancel on a real signal, and validate the premise.** `TurnResumed` (Flux) is the model-provided
   signal. Without Flux the equivalent is: caller speech resumes, OR the confirmed transcript
   differs from the partial the speculation was built on. On mismatch, abandon and re-fire — which
   means keeping the exact prompt (or its hash) that each speculation was built on.

Follow all three and a wrong speculation is **inaudible**: it costs tokens and nothing else. That
property is the whole reason the technique is acceptable.

**Concrete implementation note (Pipecat).** Add a processor between the STT and the context
aggregator that (a) on each transcription frame, computes the would-be prompt and fires an
`asyncio.Task` calling Groq with the same params, keyed by prompt hash; (b) accumulates tokens into
a buffer, never yielding `LLMTextFrame`s downstream; (c) on the real end-of-turn, compares the
committed prompt hash — on a hit, replays the buffer downstream as if it had just been generated;
on a miss, cancels the task and falls through to the normal path. Cap in-flight speculations at one,
and copy LiveKit's `max_speech_duration` guard so long turns don't spray requests. Nothing in this
touches the flow nodes, which satisfies rule 2 by construction.

### A.5 Does the wasted token spend cost more than it saves?

Do the arithmetic on our stack rather than in the abstract. Groq gpt-oss-120b, a typical Indian
outbound call, ~12 caller turns. If speculation misfires on 20% of turns (deliberately pessimistic —
the LiveKit-ecosystem folklore says <5%, unverified), that is ~2.4 extra prefills per call. Prefill
is the expensive part, because it carries the whole conversation context; the discarded *decode* is
short because we cancel early. On Groq's per-token pricing that is cents-level per call, and it is
dwarfed by the per-minute telephony cost of an India PSTN call.

**Cost is not the reason to avoid this.** Deepgram's mitigation ("smaller/faster model for the eager
draft, full LLM only on `EndOfTurn`") is a real option, but it buys budget we do not need to buy, at
the price of maintaining two prompts — and, critically for a Telugu agent, a second model that must
also speak Telugu well. **Skip the two-model variant.**

### A.6 Speculative TTS

LiveKit ships it (`preemptive_tts`) and flags it as "the lowest possible latency … at the cost of
higher wasted compute when the response is discarded" **[docs]**. Note what it is *not*: LiveKit's
`preemptive_tts` synthesises the **speculative LLM's actual output** early. It does not guess
opening words.

The "synthesise the likely opening words before the LLM finishes" variant: I found **no production
implementation and no benchmark anywhere**. It is also structurally wrong for us. Our TTS TTFB is
0.26s and it starts the moment the first *content* token lands, so pre-synthesising an opener saves
at most that 0.26s, and only when the guess is right. In Telugu the natural opener swings with the
answer's polarity ("అవును…" vs "లేదు…"), so a wrong guess is audible. **Do not build this.**

Speculative TTS in the LiveKit sense (synthesise the speculative reply, buffer the audio, discard on
cancel) is safe and is the second thing to enable — but only after cancellation (A.4) is proven in
production, because this is the one variant where a cancellation bug becomes *audible*.

### A.7 Filler / backchannel audio as a latency mask

**What the evidence supports.** It works on *perception*, not on latency. The most concrete industry
claim is that filler sounds "can make 1000ms delays feel like 500ms" **[vendor claim — blog, no
methodology]**. Backchannels ("yeah", "uh-huh", "got it") are described as reducing perceived latency
specifically **on long tool calls** **[vendor claim]**. The academic reference point is
**"Toward Enabling Natural Conversation with Older Adults via the Design of LLM-Powered Voice Agents
that Support Interruptions and Backchannels", CHI 2025 (dl.acm.org/doi/10.1145/3706598.3714228)**
**[independent — but an older-adults HCI study of naturalness, not a latency benchmark]**.

**Three documented ways it backfires:**

1. **Poor selectivity.** A provider comparison found OpenAI's stack scored latency 0.90s and
   responsiveness 100% but **selectivity 6%** — it responded to nearly *all* backchannels and
   non-directed speech **[independent benchmark — provider comparison; full methodology not
   published]**. An agent that emits fillers *and* reacts to the caller's own "hmm" produces
   crosstalk.
2. **It only works if there is no gap after it.** The one operational rule every source agrees on:
   move directly from the thinking phrase into the real response with no gap. A filler followed by
   400ms of silence is *worse* than the original 400ms of silence, because it has promised a
   response that hasn't arrived.
3. **Repetition.** A fixed filler on every turn is the single most recognisable "this is a bot"
   tell, and it accumulates over a call.

**For our agent specifically:** at 0.87s we are already near the "sub-700ms feels natural" line the
industry quotes **[vendor claim]**, and a spoken filler costs ~300–500ms of speaking time that must
then be *finished* before the real answer can start. **Filler masks multi-second tool calls, not
870ms.** It is the wrong lever at our current number. Keep it for turns sitting behind a genuinely
slow lookup (CRM, payment status, 2s+), and there use a *varied* Telugu set — "ఒక్క నిమిషం",
"చూస్తున్నాను", "సరే" — rather than one fixed token.

---

## Area B — Endpointing / turn detection

### B.0 What this repo is already running

Worth stating before the survey, because it changes which findings matter:

- `api/services/vaani/turn_taking.py` — `turn_stop_strategy` defaults to `"turn_analyzer"`,
  i.e. `LocalSmartTurnAnalyzerV3`, with `smart_turn_stop_secs` = **0.2**.
- `api/services/pipecat/run_pipeline.py` — `SileroVADAnalyzer(params=VADParams(stop_secs=0.2))`.
- `api/services/pipecat/service_factory.py` — the Deepgram Flux branch already sets
  `eot_threshold: 0.7`, `eager_eot_threshold: 0.5`, `eot_timeout_ms: 3000`.
- `api/services/pipecat/speculation/` — a stable-prefix speculator, gate, coordinator and a live
  hit-rate probe already exist.

So we are not starting from a VAD-timer baseline. We are already on the best-in-class open
endpointer at an aggressive setting. That materially caps what Area B has left to give.

### B.1 The options, with what each one actually costs

| Detector | Decision signal | Cost per decision | Telugu? |
|---|---|---|---|
| **Silero VAD v5** | acoustic energy/speech only | **189µs per 31.25ms chunk (ONNX), 325µs (JIT)**, "<1ms on a single CPU thread" **[docs — snakers4/silero-vad Performance Metrics wiki]** | trained on 6000+ languages; **language-agnostic**, so yes |
| **Smart Turn v3 / v3.1** (Pipecat) | semantic + prosodic, audio-only, 8MB int8 ONNX | **12ms** modern CPU (AWS c7a.2xlarge 12.6ms); **33.8ms** t3.2xlarge; **59.8ms** c8g.medium; **94.8ms** t3.medium **[independent benchmark — Daily's published per-instance table]** | **23 languages, Telugu NOT among them.** Hindi and Marathi are; Marathi is the weakest Indic at 87.60%. v3.1 (2025-12-03) lifted English 88.3%→94.7% and Spanish 86.7%→90.1%; "the remaining 21 languages have similar performance in v3.1 compared to v3.0" **[vendor benchmark]** |
| **LiveKit turn detector v1** | parallel semantic + acoustic branches, fused; runs on audio directly, no transcript wait | text detector "<500 MB RAM", per-turn latency **~50–160ms** **[docs]**; older multilingual text model ~400MB / ~25ms, English-only ~200MB / ~10ms **[docs]** | **14 languages: en, ar, de, es, fr, hi, id, it, ja, ko, nl, pt, tr, zh. No Telugu.** **[docs]** |
| **Deepgram Flux** | conversational STT with native turn events | server-side; ~260ms EOT detection **[vendor claim]** | Flux language hints in this repo: de, en, es, fr, hi, it, ja, nl, pt, ru. **No Telugu.** |
| **Deepgram `utterance_end_ms`** (classic) | gap between finalised words; requires `interim_results=true` | server-side, no local cost | works with `multi`, but is a pure silence timer |
| **AssemblyAI U3.x** | **punctuation-based** EOT — checks for terminal `.` `?` `!` after silence | server-side, decision lands ~300ms **[vendor claim]** | streaming supports **en, es, de, fr (+ it, pt on some models)**. **No Telugu.** |

**The finding that dominates Area B: there is no turn detector in existence, open or commercial,
that is trained on Telugu.** Smart Turn v3, LiveKit v1, Flux and AssemblyAI all stop short of it.
`latency_budget.yaml` already flags this ("No Telugu turn detector exists yet… LARGEST RISK IN THE
BUDGET"), and this research confirms it rather than resolving it. Everything below has to be read
through that lens.

### B.2 The accuracy/latency tradeoff curve — the one real dataset

LiveKit's Turn Detector v1.0 evaluation is the only published, methodology-backed curve I found
that puts several detectors on the same axes. **[independent benchmark — LiveKit's own evaluation,
so read it as vendor-run but cross-provider and quantitative]**

At a fixed **300ms** latency budget, false-cutoff rate:

| Detector | False cutoffs @300ms |
|---|---|
| LiveKit Turn Detector v1 | **9.9%** |
| Deepgram Flux | **12.9%** |
| ultraVAD | **27.7%** |

At a fixed **600ms** budget:

| Detector | False cutoffs @600ms |
|---|---|
| LiveKit Turn Detector v1 | **4.5%** |
| Soniox | **5.5%** |
| Deepgram Flux | **9.9%** |

Read the other way round — fix the error rate and read off the latency: LiveKit v1 hits a **5%
false-cutoff rate at 543ms mean latency**, and **10% at 295ms**.

**This is the single most useful number set in this whole document, and it is bad news for our
budget.** Our own quality floor is `max_false_interruption_rate: 0.02` — **2%**. The best detector
anyone has published needs **543ms just to reach 5%**, in languages it was actually trained on.
There is no published operating point at 2%. Extrapolating the curve's shape, 2% is somewhere
north of 700–800ms of endpointing latency. Our `endpoint_detection: budget_ms: 250` and our 2%
false-interruption floor are, on the only public evidence available, **mutually unreachable at the
same time** — in a supported language, let alone Telugu.

That contradiction has to be resolved deliberately: either the 250ms budget moves, or the 2% floor
moves, or the saving has to come from somewhere that isn't endpointing (which is Area A, and which
is why Area A matters more than Area B here).

### B.3 What production agents actually set

| System | Setting | Value |
|---|---|---|
| Pipecat | `VADParams.stop_secs` | **0.2s** (source constant `VAD_STOP_SECS = 0.2`; `confidence` 0.7, `start_secs` 0.2, `min_volume` 0.6) **[docs]** — note older Pipecat releases shipped **0.8**, so a version bump silently changes this |
| Pipecat + Smart Turn | recommended VAD `stop_secs` | "**0.2 seconds, which is the default value**"; `SmartTurnParams.stop_secs` **3.0**, `pre_speech_ms` **500**, `max_duration_secs` **8.0** **[docs]** |
| LiveKit (no detector) | `min_delay` / `max_delay` | **0.5s / 3.0s** **[docs]** |
| LiveKit (audio turn detector) | `min_delay` / `max_delay` | **0.3s / 2.5s** — "the model provides a confident end-of-turn signal, so the session commits sooner" **[docs]** |
| Vapi | `startSpeakingPlan.waitSeconds` | **0.4s** **[docs]** |
| Deepgram classic | `utterance_end_ms` | **1000ms** default, `interim_results=true` required **[docs]** |
| AssemblyAI U3 Pro Realtime | `min_turn_silence` / `max_turn_silence` | **100ms / 1000ms** default; presets **min_latency 128/640**, **balanced 128/1280**, **max_accuracy 512/2560** **[docs]** |
| This repo | Silero `stop_secs` / smart-turn `stop_secs` | **0.2 / 0.2** |

The industry's centre of gravity for a *pure silence timer* is **400–1000ms**. Everyone who has gone
below 400ms has done it by putting a trained detector in front of the timer — which is exactly what
this repo already did.

### B.4 Adaptive endpointing — shortening the wait when the answer already looks complete

This is the strongest idea in Area B and it is genuinely productionised, in two different shapes.

**Shape 1 — rule-based on the transcript text. Vapi's `transcriptionEndpointingPlan`.** **[docs]**
Rules applied in priority order, with these published defaults:

| Condition | Wait |
|---|---|
| transcript ends with a **number** — `onNumberSeconds` | **0.5s** |
| transcript contains **punctuation** — `onPunctuationSeconds` | **0.1s** |
| **no punctuation** (fallback) — `onNoPunctuationSeconds` | **1.5s** |

Plus `customEndpointingRules`, which override everything and are regex-driven. Vapi's own worked
example is a phone number: `{"type": "user", "regex": "\\d{3}-\\d{3}-\\d{4}", "timeoutSeconds": 2.0}`
**[docs]** — note the direction: they *lengthen* the wait for a pattern known to arrive in chunks.

**The 15× spread between 0.1s and 1.5s is the actual measured shape of this technique in
production.** That is far more headroom than anything in Area A. It is also why the "short numeric
reply" case is not automatically a *short* wait: Vapi deliberately gives a trailing number **0.5s**,
five times the punctuation case, because callers reading out digits pause between groups. Our
instinct — "a short numeric reply to a direct question must be complete, so cut fast" — is the
opposite of what the one production system that publishes its defaults does. **Trust Vapi's
direction, not the instinct.**

**Shape 2 — probability-mapped delay. LiveKit / Vapi's `livekit` smart endpointing.** The detector
emits a continuous end-of-turn probability `x`, and the silence wait is a *function* of it. Vapi
publishes the actual default curve: **`200 + 8000 * x`**, i.e. a wait between **200ms** (x=0) and
**8200ms** (x=1) **[docs]**. LiveKit's own version of this is the `min_delay`/`max_delay` pair plus
a per-language calibrated `unlikely_threshold` — "lower values make the detector more eager to
respond while higher values make it more patient", overridable "globally with a scalar" or
"per language (unmapped languages keep the default)" **[docs]**.

That per-language override is the concrete hook for a Telugu deployment: even where the detector
has no Telugu training, the *threshold* is a knob we own and can calibrate from our own call
recordings.

**Shape 3 — punctuation as the finality signal.** AssemblyAI's U3 Pro family decides EOT by
"the punctuation it predicts for what was just said" — terminal `.` `?` `!` means complete, comma
means keep waiting — landing the decision at **~300ms**, using acoustic cues like pitch contour to
choose the punctuation **[docs / vendor claim]**. This is elegant and it is why their
`min_turn_silence` can be as low as **100ms**. It is unavailable to us: no Telugu, and Telugu
punctuation prediction in a streaming STT is not something any vendor offers.

**No measured false-cutoff rate for adaptive endpointing was published by anyone.** Vapi publishes
the thresholds but not the error rate they produce; LiveKit publishes the error curve but for the
detector, not for the adaptive mapping. That gap is real and is listed under "What I could not
verify".

### B.5 Ranked recommendations for Area B

**B-1. Port Vapi's three-rule adaptive endpointing onto our turn-stop strategy.**
*Estimated saving: 200–400ms on the majority of turns (the ones that end in a clear finality
cue). Risk: low-moderate. Best ratio in this document.*
Implementation: wrap `TurnAnalyzerUserTurnStopStrategy` so that the effective `stop_secs` is chosen
per turn from the running transcript rather than fixed at 0.2. Start from Vapi's published numbers
(punctuation 0.1 / number 0.5 / bare 1.5), and add a Telugu-specific rule set the vendors cannot
give us: Telugu sentence-final verb morphology and the discourse particles that end a turn
(-అండి, -గా, -లేదు, -ఉంది) versus the ones that signal more is coming (-అంటే, -మరి, and a trailing
conjunctive participle). This is the one place where knowing Telugu beats having a bigger model,
and it is the highest-value Telugu-specific work available.
**Important:** because we currently sit at a *flat* 0.2s, the adaptive rules will mostly make us
**slower and safer** on ambiguous turns and only marginally faster on clean ones. That is the
correct trade against a 2% false-interruption floor — count it as buying quality headroom that then
lets Area A spend latency, not as a raw latency win.

**B-2. Calibrate the smart-turn decision threshold on our own Telugu recordings.**
*Estimated saving: unknown, potentially large. Risk: low (offline work).*
Smart Turn v3 has no Telugu, so its raw probability is out-of-distribution — but the *threshold*
applied to that probability is ours, exactly as LiveKit exposes per-language thresholds. Take 200
recorded Telugu turns, label true turn ends, sweep the threshold, and plot our own version of the
LiveKit curve. This converts `latency_budget.yaml`'s "nobody has measured that" into a number, and
it is the prerequisite for defending *any* endpointing budget below 600ms in Telugu.

**B-3. Do not switch STT to Flux or AssemblyAI for the endpointing.**
*Saving: 0. Risk: breaks the agent.* Neither supports Telugu. The Flux `eager_eot_threshold: 0.5`
already sitting in `service_factory.py` is dead code for a Telugu call. Keep it for the English/Hindi
verticals; it is genuinely the best eager-EOT design available and is worth having wired.

**B-4. Leave Silero VAD alone.** At 189µs/chunk it is free, and it is the only component in the
stack that is language-agnostic by construction. `stop_secs=0.2` under a detector is correct. The
one thing to guard: pin the Pipecat version or assert `VAD_STOP_SECS`, because the default moved
from 0.8 to 0.2 between releases and a bump in the other direction would silently add 600ms.

**B-5. Smart Turn v3 CPU cost is not the problem — but check which instance we are on.**
12ms on a modern CPU is negligible against a 0.2s window. **94.8ms on a t3.medium is not.** On a
small VPS core, the detector alone can eat half the endpointing budget, and burstable instances
(t3.*) will do it inconsistently — a p95 spike in turn latency that looks like a network problem
and isn't. Verify the actual instance class, prefer a non-burstable one, and log per-turn detector
inference time. This is a 30-minute check with a real chance of finding 60–80ms.

---

## Area C — The realistic floor

### C.1 Published end-to-end breakdowns for cascaded stacks

**Coval, "How to Measure Voice AI Latency"** — the clearest methodology statement I found.
Clock definition: start at "user finishes speaking (end of utterance detected)", stop at "AI agent
begins speaking (first audio played)". Their published cascaded breakdown **[vendor claim, but with
a stated methodology]**:

| Component | Coval's range |
|---|---|
| STT | 300–500ms |
| LLM | 600–1200ms |
| TTS | 300–500ms |
| Network / orchestration | 200–400ms |
| **Total** | **1400–2600ms** |
| (speech-to-speech, for contrast) | 900–2000ms |

**Hamming**, from "10K+ voice agents (2025–2026)": industry **median 1.4–1.7s**, with **10% of calls
exceeding 3–5s** **[vendor claim — large sample, methodology not fully published]**.

**LiveKit ecosystem**: vanilla agent loop **1.2–1.4s p95**, tuned **500–650ms p95** **[vendor claim,
third-party blog]**.

**Retell**: "~600ms" marketed **[vendor claim]**.

Our measured **0.87s** sits *below* every published median and roughly at the tuned-stack figure.
It is worth saying plainly: **we are already in the top decile of deployed cascaded agents.** The
remaining work is not catch-up, it is squeezing an already-good number, and the marginal returns
reflect that.

### C.2 What PSTN/SIP transit adds, and what it adds in India

**What is standardised.** ITU-T G.114: one-way mouth-to-ear delay should stay **under 150ms** for
normal conversation; **above 400ms is unacceptable** **[docs — ITU-T standard]**. Measured endpoint
contributions: hardware IP phones **45–90ms** average M2E under low jitter; software clients
**65ms to over 400ms** depending on implementation **[independent — VoLTE/VoIP measurement
literature]**. "A clean call on a good path is 60 to 120 milliseconds mouth to ear."

**What is not published.** I could find **no independent, methodology-backed measurement of
India-specific PSTN or VoLTE one-way delay** for the Indian mobile carriers, and none for the
Indian SIP providers (Exotel, Plivo, Twilio-India, Vobiz). The academic VoLTE delay literature is
not India-segmented. This repo's own `latency_budget.yaml` carries
**`telephony_overhead_ms: 490`** with `caller_p50_ms: 1290`; that 490ms is the right order of
magnitude for a mobile-originated Indian call with a jitter buffer, a transcode and a media leg to
a cloud region, but **I could not corroborate it against any external source** and it should be
treated as our own estimate until traceroute/RTP-timestamp measurement replaces it.

**The structural point that matters more than the exact number.** Telephony overhead is
symmetric-ish and it sits *outside* everything Area A and Area B can touch. If the caller
experiences ~1.29s and we cut 300ms of server-side latency, they experience ~0.99s — a 23%
improvement, not a 34% one. Every percentage in this document should be quoted against the
**server-side 0.87s**, never against the caller-side number, and the two must never be mixed in the
same sentence. `latency_budget.yaml` already gets this right by separating `p50_ms: 800` from
`caller_p50_ms: 1290`; keep it that way.

**The one India-specific lever that is real:** provider serving region. `latency_budget.yaml` lists
`transport: budget_ms: 50, measured: false, "Traceroute the providers. This is an hour of work to
make measured."` Groq, Cartesia and Deepgram serving from US regions to an Indian media server adds
a round trip per hop that no amount of speculation recovers. **Confirming which region each of our
three vendors actually serves us from is the cheapest unclaimed latency in this whole document** —
possibly 100–250ms, at zero implementation risk, for an hour of work. It is not glamorous and it is
not what the question asked about, but it outranks everything in Area A on ms-per-unit-risk.

### C.3 A defensible floor for an India-PSTN Telugu agent

Building it up from components that are either measured here or externally benchmarked:

| Component | Floor | Basis |
|---|---|---|
| Endpoint decision | **250–400ms** | LiveKit v1 curve: 295ms at a 10% false-cutoff rate, 543ms at 5%, in *supported* languages. Telugu has no trained detector, so the honest floor is the upper half of this. |
| STT finalisation | **100–150ms** | AssemblyAI publishes ~50ms RTT + 200–300ms STT processing; a finalisation-only slice is smaller. Unmeasured for Telugu. |
| LLM to first content token | **0.61s** | measured here; irreducible for gpt-oss-120b at `reasoning_effort=low` |
| TTS TTFB + transit | **0.26s** | measured here. Coval's independent TTS benchmark puts Cartesia Sonic-3 at **188ms p50** (English); the fastest measured is 155ms. |
| **Server-side total** | **~1.2s naive, ~0.87s as actually built** | |
| Telephony transit (India, mobile) | **+~490ms** | our own estimate, uncorroborated |

**The defensible server-side floor, without speculation: ~0.85s.** That is where we already are —
the current implementation is already overlapping components rather than summing them.

**With speculation working at a decent hit rate: ~0.55–0.65s server-side**, because the LLM's 0.61s
moves off the critical path on hit turns and what remains is endpoint decision + TTS. Below that,
nothing in the current stack can go without changing the LLM.

**Caller-perceived, India mobile: ~1.05–1.15s at best**, against ~1.29s today. Anyone promising a
caller-perceived sub-700ms Indian PSTN call with a cascaded stack and a reasoning LLM is quoting the
server-side number and hoping nobody checks.

---

## Techniques that sound good but don't work

**1. Predicting the caller's words and pre-synthesising an opening phrase.**
No production system does this — every "speculative" feature shipped by LiveKit, Deepgram or
AssemblyAI starts the *real* LLM earlier; none guesses content. The only genuine word-prediction
work is arXiv:2409.19990, a research ASR that predicts up to 300ms ahead, with no deployment path
and no Telugu. And in Telugu specifically the opener swings with polarity ("అవును…" / "లేదు…"), so a
wrong guess is audible rather than merely wasteful. **Evidence: absence of any implementation
despite the idea being obvious and the ecosystem being latency-obsessed.**

**2. Treating Area A and Area B savings as additive.**
Both spend the same silence window. Speculating at silence onset while *also* cutting the silence
threshold to 0.1s recovers 0.1s, not 0.5s. Budget them as one line item. This is the most common
way a latency plan on paper fails to reproduce in a call.

**3. Filler audio at our current latency.**
Fillers mask *seconds*. A 300–500ms spoken filler in front of an 870ms gap makes the turn longer,
not shorter, and its only documented benefit is on long tool calls. **Evidence: every source that
recommends fillers scopes them to tool calls; the one operational rule ("no gap after the filler")
is unsatisfiable when the gap you are masking is shorter than the filler.** Keep them for 2s+ lookups.

**4. Switching STT to Flux / AssemblyAI to get the good endpointer.**
Both have the best turn detection available and **neither supports Telugu**. The
`eager_eot_threshold: 0.5` already in `service_factory.py` never fires on a Telugu call. **Evidence:
Flux language-hint map in this repo (10 languages, no `te`); AssemblyAI streaming supports 4–6
European languages.**

**5. Using a smaller/faster draft model for speculative turns** (Deepgram's own suggested cost
mitigation). Correct in general, wrong for us: it introduces a second model that must speak fluent
Telugu, doubles the prompt-maintenance surface, and the cost it saves is cents per call — a cost we
were not struggling with. **Evidence: the arithmetic in A.5.**

**6. Chasing the LLM further.** Already settled empirically upstream of this research, but worth
recording alongside it: `reasoning_effort="none"` is a 400 from Groq, and the 0.41s CoT burn is
structural to the model. Area A moves that 0.61s off the critical path; nothing shortens it.

**7. Turning `preemptive_generation` on and letting it drive the whole pipeline.**
This is LiveKit's actual shipped behaviour and it is the subject of livekit/agents#3414: speculative
runs re-fire `on_user_turn_completed` and `tts_node`, corrupting flow state. For a node-graph agent
with extraction and transitions, the naive version is worse than no version. **Evidence: the issue
itself, plus the fact that Pipecat's own Flux integration explicitly punts with
"TODO: Implement proper EagerEndOfTurn support with cancellable processing pipeline".**

---

## What I could not verify

1. **Any primary source for the "80–90% hit rate, 200–400ms saved, 300–500ms penalty" figures.**
   These circulate widely and appear in search summarisation, but I could not trace them to a
   vendor doc, a paper, or a benchmark. **Do not put them in a plan document.**
2. **The "<5% discarded speculative calls" and "150–350ms TTFT reduction" figures.** Sourced only
   to a third-party SEO blog (futureagi.com) with no published methodology.
3. **Any measured hit rate for stable-prefix speculation on any language**, let alone Telugu. No
   production system publishes the N-words / X-ms parameters of a stable-prefix trigger. The
   `SpeculationProbe` already in this repo is, as far as I can tell, the only way we will ever get
   this number — which makes running it the highest-information action available.
4. **Any measured false-cutoff rate for adaptive (rule-based) endpointing.** Vapi publishes the
   thresholds; nobody publishes the error rate those thresholds produce.
5. **Smart Turn v3/v3.1 behaviour on Telugu.** Not in the 23-language list, no evaluation exists,
   and the nearest Indic proxies diverge sharply (Hindi high, Marathi 87.60%, Bengali 84.10%).
   `turn_taking.py` already says "nobody has measured that"; this research does not change it.
6. **Whether Smart Turn v3.1's language list is identical to v3.0's.** The v3.1 post says "still
   23 languages" without re-listing them.
7. **India-specific PSTN/VoLTE one-way delay from any independent source.** The 490ms in
   `latency_budget.yaml` is plausible but uncorroborated.
8. **Which region each of our vendors actually serves us from.** Not researchable from outside —
   it needs a traceroute.
9. **Cartesia's Telugu TTS TTFB.** The 188ms p50 Coval figure is English. Indian-language voices
   frequently have different serving characteristics.
10. **Whether any Telugu-capable STT emits immutable partials.** This is the precondition that makes
    AssemblyAI-style speculation safe, and I could not establish it for any Telugu-supporting
    provider (Deepgram `multi`, Google, Sarvam, ElevenLabs, Gladia). If our STT revises partials
    backwards, the stable-prefix tracker in `stable_prefix.py` is doing exactly the right thing and
    its `HOLD`-on-shrink behaviour will be load-bearing.

---

## Sources

**Primary vendor documentation [docs]**
- LiveKit, Agent speech and audio (preemptive generation, `preemptive_tts`, `max_speech_duration`) — https://docs.livekit.io/agents/multimodality/audio/
- LiveKit, Agent session (`preemptive_generation`, `turn_handling`) — https://docs.livekit.io/agents/build/sessions
- LiveKit, Turn detector plugin (14 languages, `min_delay`/`max_delay`, `unlikely_threshold`) — https://docs.livekit.io/agents/build/turns/turn-detector/
- LiveKit, Silero VAD plugin — https://docs.livekit.io/agents/logic/turns/vad/
- Deepgram, Optimize Voice Agent Latency with Eager End of Turn — https://developers.deepgram.com/docs/flux/voice-agent-eager-eot
- Deepgram, Flux end-of-turn configuration — https://developers.deepgram.com/docs/flux/configuration
- Deepgram, Utterance End (`utterance_end_ms`) — https://developers.deepgram.com/docs/utterance-end
- AssemblyAI, Universal 3.5 Pro Realtime on Pipecat (`min_turn_silence`, `max_turn_silence`, punctuation EOT) — https://www.assemblyai.com/docs/voice-agents/pipecat-universal-3-5-pro
- AssemblyAI, streaming language support — https://www.assemblyai.com/docs/faq/language-support-for-real-time-transcription
- Vapi, Voice pipeline configuration (`onPunctuationSeconds` 0.1 / `onNumberSeconds` 0.5 / `onNoPunctuationSeconds` 1.5 / `waitSeconds` 0.4, `customEndpointingRules`) — https://docs.vapi.ai/customization/voice-pipeline-configuration
- Vapi, Speech configuration (LiveKit wait function `200 + 8000 * x`) — https://docs.vapi.ai/customization/speech-configuration
- Pipecat, Smart Turn overview (`SmartTurnParams`, recommended VAD `stop_secs` 0.2) — https://docs.pipecat.ai/api-reference/server/utilities/turn-detection/smart-turn-overview
- Pipecat, `VADParams` source constants — https://reference-server.pipecat.ai/en/stable/_modules/pipecat/audio/vad/vad_analyzer.html
- Silero VAD, Performance Metrics wiki — https://github.com/snakers4/silero-vad/wiki/Performance-Metrics

**Issues / implementation evidence**
- pipecat-ai/pipecat #3321, "Preemptive speech generation option for seamless conversation" (open, unassigned) — https://github.com/pipecat-ai/pipecat/issues/3321
- livekit/agents #3414, preemptive generation re-fires turn hooks — https://github.com/livekit/agents/issues/3414
- livekit/agents-js #773, preemptive generation missing in Node — https://github.com/livekit/agents-js/issues/773
- This repo: `pipecat/src/pipecat/services/deepgram/flux/base.py` — `TODO: Implement proper EagerEndOfTurn support with cancellable processing pipeline`

**Benchmarks and evaluations**
- LiveKit, Solving end-of-turn detection: Turn Detector v1.0 (false-cutoff curve vs. Flux, Soniox, ultraVAD) — https://livekit.com/blog/solving-end-of-turn-detection
- Daily, Announcing Smart Turn v3, with CPU inference in just 12ms (per-instance timings, 23-language accuracy table) — https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/
- Daily, Improved accuracy in Smart Turn v3.1 (2025-12-03) — https://www.daily.co/blog/improved-accuracy-in-smart-turn-v3-1/
- Coval, How to Measure Voice AI Latency — https://www.coval.ai/blog/how-to-measure-voice-ai-latency-the-complete-guide/
- Coval public benchmarks (TTS/STT latency, pinned dataset) — https://benchmarks.coval.ai/overview
- Hamming, Voice AI Latency: What's Fast, What's Slow — https://hamming.ai/resources/voice-ai-latency-whats-fast-whats-slow-how-to-fix-it

**Academic**
- Zink, Higuchi, Mullov, Waibel, Kobayashi, "Predictive Speech Recognition and End-of-Utterance Detection Towards Spoken Dialog Systems", arXiv:2409.19990 (ICASSP 2025) — https://arxiv.org/abs/2409.19990
- "Toward Enabling Natural Conversation with Older Adults via the Design of LLM-Powered Voice Agents that Support Interruptions and Backchannels", CHI 2025 — https://dl.acm.org/doi/10.1145/3706598.3714228
- USC ICT, Incremental Dialogue Processing — https://nld.ict.usc.edu/group/research/incremental-dialogue-processing
- ITU-T G.114, One-way transmission time — https://www.itu.int/rec/T-REC-G.114

**Low-confidence / third-party, cited only where labelled as such**
- futureagi.com, "How to Optimize LiveKit Voice Agent Latency in 2026" (source of the 150–350ms and <5% figures; no published methodology) — https://futureagi.com/blog/how-to-optimize-livekit-latency-2026/

**Internal (this repo, not re-researched)**
- `api/services/vaani/latency_budget.yaml`, `api/services/vaani/turn_taking.py`, `api/services/pipecat/service_factory.py`, `api/services/pipecat/speculation/`
