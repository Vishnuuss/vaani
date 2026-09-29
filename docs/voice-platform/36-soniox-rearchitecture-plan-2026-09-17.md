# 36 — Vaani v2: Soniox as the ear and the turn-taker

*Plan, 2026-09-17. Not yet approved, nothing implemented.*

## Context

The four BS Wealth agents are live and calling, and six complaints stand against
them: the agent cuts callers off, re-asks answered questions, answers the wrong
thing, goes quiet, runs slow, and gets Telugu turn boundaries wrong. The decision
is to stop building the hearing layer and buy it: **Soniox** for Telugu
speech-to-text and for end-of-turn detection, replacing Sarvam plus the custom
Telugu detector.

That decision is well founded. This project's own measurement table ranks shipped
STT finalisation p99 at **Soniox 0.35 s against Sarvam 1.17 s**, and for a period
the pipeline was waiting out Sarvam's 1.17 s p99 on every single turn. Soniox is
also the only production speech-to-text with semantic endpointing that covers
Telugu; Deepgram Flux, the nearest equivalent, has model-integrated end-of-turn
detection in ten languages and Telugu is not among them.

Three things I assumed when this planning started were wrong, and the correction
matters more than the plan:

**This is Pipecat, not LiveKit.** There is no `AgentSession`, no
`turn_detection="stt"`, no `preemptive_generation` flag. The equivalents are
`UserTurnStrategies(start=…, stop=…)` and `LLMUserAggregatorParams`. Everything
below is written against Pipecat.

**The live code is in a different checkout.** `bswealthfinance/vaani/` is an older
copy that nothing deploys; its engine subdirectories are gitignored and exist only
on this laptop. The live brain is `dograh-vapi/api/services/vaani/`, 11,096 lines,
which Coolify builds from `Vishnuuss/vaani@main`. The local clone is **9 commits
behind** that branch, and the missing commits touch the exact files this work
changes.

**The prompt-cache argument is closed, not open.** Cache hit rate is already
85–97.5%, and the "0.43 s per cache miss" figure was a cache-warmth artefact that
this project measured properly and retracted: controlled and interleaved, the
across-tier effect was 0.339 s inside a within-tier noise floor of 0.786 s.
Trimming prompts for latency is cancelled and prompt caching is recorded as "not a
lever, do not re-propose". Both are dropped from this plan.

## The honest arithmetic

Current baseline is run 972, the best call on record: **1.555 s total server-side,
p50 1.534**, split endpoint 0.906 · LLM 0.453 · TTS 0.183. The budget target is
800 ms p50. So the gap is about 730 ms.

Almost all of it is in one place. The endpoint leg of 0.906 s is **entirely a
blind timer**: Silero VAD at 0.2 s plus `dograh_speech_timeout_secs` at 0.7 s is a
0.9 s floor, and the measurement confirms a floor rather than a distribution —
across 8 calls and 69 turns, **0 of 69 turns had an endpoint under 0.6 s**, with
per-workflow minima of 0.831 to 0.954 s.

If Soniox's semantic endpoint replaces that timer and lands near its published
0.35 s, total goes from 1.555 s to roughly **1.0 s**. That is the prize, and it is
worth having: it is the single largest available win and it is the one the callers
are complaining about.

Soniox alone does not reach 800 ms. It gets to about 1.0 s. The second half of the
gap is the LLM leg, and it is available.

**800 ms is reachable, and it needs two independent changes rather than one.**

| leg | run 972 | change | after |
|---|---|---|---|
| endpoint | 0.906 s | Soniox semantic endpoint | ~0.35 s |
| LLM to first spoken word | 0.453 s | a non-reasoning model | ~0.20 s |
| TTS to first audio | 0.183 s | unchanged | 0.183 s |
| **total server-side** | **1.555 s** | | **~0.73 s** |

The LLM number is not a guess about a faster machine. It is about deleting work
that is currently being done and thrown away. `gpt-oss-120b` is a reasoning model,
and its measured 0.453 s is time to the first *audible* word, which means it
includes the burn through an inaudible analysis channel before any speakable token
appears. A non-reasoning model has no such channel. That is where the ~250 ms is.

The earlier conclusion in this project, *"700 ms is not reachable while the LLM is
a reasoning model,"* was correct and was read as a dead end. It is the opposite: it
names the fix. The recorded blocker was that the account carried no suitable model,
but Groq's production catalogue lists two non-reasoning instruct models,
`llama-3.3-70b-versatile` at 280 tokens/second and `llama-3.1-8b-instant` at 560,
both on 131k context. The account's model list needs re-checking rather than
trusting a note from early September.

The measurement tool for this already exists and already names the right
candidates. `tools/ttfw.py` times the first *content* token, explicitly excluding
the reasoning channel, across `gpt-oss-120b`, `llama-3.3-70b-versatile`,
`llama-4-maverick` and `llama-3.1-8b-instant`, and reports easy and hard medians
plus the spread, on the grounds that *"latency must not depend on how hard the
question is."* Running it settles the LLM leg in an afternoon and spends no calls.

Two real risks attach to the model swap, and they are quality risks, not speed
risks. Telugu generation quality on Llama against `gpt-oss-120b` is unmeasured and
is exactly what the 27-case battery exists to judge. And 280 tokens/second against
500 is slower *generation*, which matters because Telugu tokenises expensively and
a slow tail can open gaps mid-sentence even when the first word is early. Both are
measurable before anything ships.

Cerebras was considered and rejected on its own numbers: 490 ms time-to-first-token
at 10k input tokens is worse than what Groq already delivers here.

One thing genuinely is not available: 700 ms *at the caller's ear*. Telephony
overhead is measured at 490 ms and is not engineerable. At a perfect 800 ms
server-side the caller hears about 1.29 s. The 800 ms target is the server-side
number, which is what `latency_budget.yaml` has always meant by it.

## What Soniox fixes, and what it does not

Soniox addresses the cut-offs and the Telugu turn boundaries, and it is the larger
half of the speed win. It does nothing for the re-asking or the wrong answers. Those live
in the conversation layer, and this project has already proved the point: on the
turns where the customer asked something off-script, *"the detector fired
correctly on turns 10 and 13, and the agent asked its own question anyway."*
Buying a better ear will not change that. Phases 3 and 4 are where those two
complaints are actually addressed, and they are the phases most likely to be
under-resourced if the Soniox swap is mistaken for the whole job.

## The trap that has already killed this exact change twice

Faster text did not help, twice. Sarvam's realtime model measured a genuine 3×
improvement offline — speech-end to final transcript 0.115 s against 0.373 s — and
the endpoint leg went the wrong way, 0.888 s to **1.789 s** on run 969 and 1.824 s
on run 970. The cause was found later and committed as `5efdedd7`: **Sarvam's
server-side voice-activity detection defaults to 500 ms and was stacking its wait
on top of ours.** The fix was to send `silence_duration_ms: 100`.

Soniox arrives at the same seam with a worse default: `max_endpoint_delay_ms`
defaults to **2000 ms**. Left alone it does not merely fail to help, it exceeds the
entire turn budget on its own.

The project's standing lesson applies with full force: *"a stored config value is
not a running config value."* Every endpoint key on MB Solar — `turn_model`,
`endpoint_min_secs`, `endpoint_max_secs`, `endpoint_unsure_floor_secs`,
`semantic_turn_completion` — was **dead config for weeks**, read only inside a
branch that never executed. So the first measurement after Phase 1 is not "is the
key set", it is **"did the 0.9 s floor move"**, taken from a real call.

## Architecture

### L1 Hearing and L2 turn-taking — one change, three files

Pipecat already vendors the client. `pipecat/src/pipecat/services/soniox/stt.py`
(777 lines) carries `SonioxSTTService` and `SonioxSTTSettings`, defaults to
`stt-rt-v5`, and exposes exactly the knobs this needs: `max_endpoint_delay_ms`,
`endpoint_sensitivity`, `endpoint_latency_adjustment_level`, `language_hints`,
`language_hints_strict`, `context`. There is a reference example at
`pipecat/examples/voice/voice-soniox-turn-detection.py`. No new client code.

**`vad_force_turn_endpoint` is the whole decision.** Verified directly in the
vendored file: it is `True` by default, and the docstring states the three endpoint
settings *"only take effect when `vad_force_turn_endpoint=False`; otherwise Soniox
endpoint detection is disabled"*. Set to `False`, the service emits its own
started- and stopped-speaking frames. Shipping at the default would buy the
accuracy and none of the turn-taking, which is the headline reason for the change.

The websocket URL is also a constructor argument, defaulting to the US host, so
pointing at the India region is a one-line change once access is granted.

Three files change together, and skipping the middle one reproduces the Sarvam
stacking failure exactly:

- `api/services/configuration/registry.py` — add Soniox to `ServiceProviders`.
  It is absent today; the provider does not exist to the system yet.
- `api/services/pipecat/service_factory.py`, `stt_uses_external_turns()` — add
  Soniox. Without this the external-turn strategies are never selected and the
  local timer stacks on top of Soniox's wait.
- `api/services/pipecat/service_factory.py`, `create_stt_service()` — the new
  provider branch.

Settings to start from, every one a hypothesis to sweep rather than a
recommendation: `language_hints=["te","en"]` and **not** strict, because callers
code-switch and speak numbers in English; `max_endpoint_delay_ms` set explicitly
and low; `endpoint_sensitivity` and `endpoint_latency_adjustment_level` swept from
their neutral defaults.

`context` is the cheapest accuracy win available and nothing equivalent is wired
today. The `dictionary` config key exists and is threaded as `keyterms`, but the
Sarvam branch ignores it entirely and the value is empty on all six agents. Soniox
`context.terms` should carry the client lexicon: company and product names, Telugu
financial vocabulary, and the caller's own name from the lead row. Two recorded
failures were pure transcription defects — "rented" transliterated so that no
pattern matched, and a caller never disqualified as a result.

Consequences to handle in the same change:

- **`PartialResponder` must be re-examined or removed.** It exists only because
  `saarika:v2.5` emits no interim transcripts at all; it promotes the newest
  partial and *suppresses the genuine final*. With Soniox streaming real tokens
  that behaviour is redundant at best and harmful at worst.
- **Silero VAD `stop_secs=0.2` is hard-coded** with no config key, and Pipecat
  warns when it diverges from the STT's p99. If Soniox owns endpointing this needs
  a deliberate decision, not inheritance.
- **External turns silently raise the stop backstop** from 5 s to 30 s. The
  endpoint spikes already on record — 8.56 s, 13.66 s, 10.82 s — become more
  dangerous under a 30 s backstop, and **nothing currently logs which cause
  fired**. That logging goes in before the flag flips, not after.
- **Speech-to-text is an organisation-level setting, not per-workflow.** Changing
  it moves all five live agents at once. This is the single largest operational
  risk in the plan and it shapes the rollout.

The retired Telugu detector, its ONNX and GBM artifacts, and the detector-
comparison tooling all stay on disk. The revert has to be real.

### L3 Thinking — leave the prompt alone, fix one duplication

No prompt trimming, no cache work, no retrieval justified on latency. All three
are closed by measurement.

One genuinely unexamined finding is worth a look: **the system prompt appears to
be sent twice per turn.** It is installed both as an LLM setting and as the first
context message, and Pipecat's adapter keeps both. The outgoing body would then
carry the ~35 KB prompt twice. This does not break cache stability, but it would
roughly double prefill and would mean the prewarm request warms only the first
copy. One log line of `prompt_tokens` settles it before anyone changes code.

The one prompt effect that *is* real and was reproduced: instruction **volume** on
a reasoning model is latency. Layers 1 and 2 growing from 26,216 to 112,526
characters took the LLM leg from 0.337 s to 1.884 s. The remedy is already built
and shipped — the coach moves conditional playbook rows out of the prompt and
injects one or two matched lines per turn, about 220 characters against the 86,310
it would have cost inline. That pattern is the template for anything added later.

### L4 Conversation control — the real fix is a wiring gap, not another guard

This is where the re-asking lives, and the instinct to add a guard is measurably
wrong. Five overlapping re-ask mechanisms already exist — a one-turn suppression, a
two-ask cap per field, a two-answer cap per field, a reworded-subject matcher, and
a wording-similarity guard — plus two counter-forces that refund asks and can chain
to make the loop unbounded. Adding a sixth was tried on 12 September and
**reverted five days ago** with the measurement attached: field capture fell from
6/6 to 2/5 as guards accumulated. The commit message is the lesson: *"a guard that
refuses to ask a question is also a guard that stops a field ever being filled."*

The guards fight each other because they are all proxies for a fact the system does
not have. **`known` is essentially never populated on the live path.** The
extractor exists and writes it, but nothing on the live path calls it — it was
wired into the simulator only. The only live writer is the money parser. So every
non-money field is retired by *counters* rather than by *knowledge*, and the
"already answered" guard can only ever fire for bill amounts.

That single wiring gap is the highest-value change in this plan. Filling it moves
`still_need`, the phase machine, the already-known guard and the lead record at
once, and it makes the existing guards unnecessary instead of adding a new one.
There is a regression test for exactly this defect, but it asserts against the dead
checkout's pipeline, so it does not protect production. It needs re-pointing.

A second precise gap: **`question_variants` has no writer anywhere.** It is
declared, read by the variant selector, specified by the bookish-Telugu analysis,
and pinned by a test — and nothing populates it, so every re-ask falls back to the
single written question. That is the direct cause of the measured drift: the first
ask is 90–100% verbatim and every re-ask drifts toward written Telugu, because the
anti-repetition rule forces a paraphrase and nothing anchors it to spoken register.
Wiring the variants is the fix, and the underlying trigger is the turn-taking work
in Phase 1.

Two things already exist and must not be re-proposed as new. **Answer-then-bridge
is built**: when the caller asks something, the checklist is suppressed entirely
and the pending question is re-attached second, explicitly subordinate. And the
grader **over-counts re-asks** — the subject matcher matches sentences that merely
name a field without asking about it, and two decisions were nearly taken on that
misreading. Fix the grader before trusting a re-ask count.

The one structural change worth making here: the policy engine is a single
480-line if-else chain, and every fix has added a branch and a gate. It needs an
explicit priority table. That is a refactor with no behaviour change, gated by the
existing run-numbered test corpus.

### L5 Speaking — unchanged, deliberately

Cartesia `sonic-3` stays, per the decision already taken. It is measured at
0.046–0.063 s first audio on a real call, and token-mode aggregation is already on
and is what makes that real. Soniox text-to-speech gets its own A/B later, against
the same gate, never in the same change as the ear.

Filler clips stay off. They were shipped twice, heard by the client both times, and
the defect is upstream of timing: a pre-recorded word does not sound like the
sentence it precedes. Pre-recorded replies stay rejected. The voice stays on the
slower stock option that was chosen on measurement, because the previous clone was
the fastest talker of six candidates and rushed speech on a narrowband line is what
"not clear" sounds like.

### Preemptive generation — re-openable, but on a measured prior of one in six

This is the mechanism that would take the LLM off the critical path, and its record
here deserves bluntness. It is disabled on all six agents, its builder deliberately
raises rather than running, and it has been killed three times: there were no
interim transcripts to work from; a hit would have spoken a reply built with an
empty state block, bypassing the answer-first rule, the ask cap and the repeat
guard; and the ceiling was **measured** — the last partial equalled the final
transcript on 2 of 10, and 1 of 6 on the most recent attempt.

Soniox genuinely changes the first objection, since it streams real tokens where
Sarvam streamed none. So the match rate is worth re-measuring. But it is a Phase 5
hypothesis with a measured prior near 17%, not the load-bearing mechanism, and the
correctness objection is unaddressed: any revival must build the speculative
request through the *same* state block as the real one, and a test must assert that
with the turn-end event suppressed, no audio is ever synthesised. A pass-through
observer in this project once issued real generations and produced a call with zero
pipeline events.

## The seven standing techniques

| | verdict |
|---|---|
| Retrieval over prompt-stuffing | **Used, on relevance grounds only.** The FAQ is a hand-written block inside a 13.8 KB per-client prompt with no structure the code can address. Retrieval gives the off-script answer a real path. The latency and cache justifications are retracted and dropped. It must inject one or two matched rows the way the coach does — a list placed in the prompt gets recited, measured at 20/25 falling to 15/25. |
| Semantic embeddings | **Used, and must beat a strong baseline.** The 37 existing cues are regex, but several were mined from 3,104 real caller utterances and one replaced a rule that fired zero times on real speech. Embeddings ship only if they beat that, measured. |
| Model routing / cascading | **Used, and it is half the latency target.** Moving the reply off a reasoning model onto a non-reasoning one is worth ~250 ms, because the current measurement includes burning through an inaudible analysis channel. `ttfw.py` already benchmarks the candidates. This is the lever, not a cost optimisation. |
| Classical ML for scoring | **Skipped.** No labelled outcome volume, and simulating one is forbidden. The stronger local lesson: this project already shipped a classical turn model that lost to a flat timer end-to-end, so any such model is scored against the dumb baseline or not at all. |
| Bandits | **Deferred.** The natural home is `question_variants` once it has a writer. Adding variance while chasing a turn-taking regression would corrupt the measurement. |
| Eval harness | **Used, and it exists.** Reuse the 27-case text-chat gate with its baseline comparison, the per-call grader, the CI budget test, and the model promotion rule. Run the case battery **twice**; the noise band is ±3 cases and a single run proves nothing. |
| Observability and versioning | **Used, with one real hole.** Nothing logs which cause fired on an endpoint decision, which is why the 8.56 s and 13.66 s spikes are unexplained. That logging lands before the flag flips. |

## Phases

**Phase 0 — unblock, then measure offline. No agent changes, no calls spent.**
Pull the 9 missing commits. Create the Soniox account and **request the India
region**, which is a precondition rather than an optimisation: vendor round-trip
from India is already measured at 30.8 ms for Groq and 21.1 ms for Cartesia, while
Deepgram from the US measures 267.6 ms. Serving Soniox from outside India would
spend a quarter of the budget on travel. Then two measurements in parallel, both
offline:

- Replay harvested Telugu call audio through Soniox and measure the
  endpoint-decision distribution and transcript accuracy against Sarvam on the
  same clips.
- Re-check the account's live model list, then run `ttfw.py` over the
  non-reasoning candidates to settle the LLM leg.

Go/no-go on those numbers, and they decide the order of Phases 1 and 2 since the
two legs are independent.

**Phase 1a — the LLM leg.** Swap to whichever non-reasoning model wins on
`ttfw.py`, then gate it on the 27-case battery run twice, because the risk here is
Telugu quality and not speed. Watch the generation tail as well as the first word:
a slower tokens-per-second can open gaps mid-sentence even when the first word
arrives early. This is config-level and independent of everything Soniox.

**Phase 1b — the provider swap and external turns**, the three files, behind config.
The acceptance test is that the 0.9 s floor moved, measured on a real call.
Gate on **transcript accuracy and field capture, not latency** — that is the rule
the last speech-to-text swap was held to, and it is the right one. Compare capture
over at least three calls each way. Remember the change is org-wide.

**Phase 2 — tune the boundary.** Sweep the three Soniox endpoint knobs, settle
Silero's stop window, and resolve `PartialResponder`. Watch the false-interruption
rate as closely as the latency, using the echo-immune comparison that reads
cut-offs only from the caller's own burst boundaries.

**Phase 3 — wire `known`, and wire `question_variants`.** The re-ask fix and the
bookish-Telugu fix. Re-point the regression test at the live pipeline. Fix the
grader's over-counting first, so the before-and-after is trustworthy.

**Phase 4 — retrieval for the off-script answer**, plus the priority-table refactor
of the policy engine.

**Phase 5 — optional and hypothesis-led.** Re-measure the preemptive match rate on
Soniox partials. A/B Soniox text-to-speech.

## Gate

- endpoint floor demonstrably moved below 0.6 s on real calls, which nothing in
  69 measured turns has yet achieved
- false-interruption rate at or under 0.02, measured from both audio legs
- field capture not below the run-972 baseline of 6/6
- zero double replies
- the 27-case battery run twice with no case regressing against the baseline
- one-word Telugu turns still register
- **server-side p50 at or under 800 ms**, with each leg reported separately so a
  regression can be attributed rather than argued about

## Verification

Offline first, on harvested audio, spending no calls. Then the free text-chat
probe, which hits the deployed code. Then **one** real call, transcript and latency
table read, before the next change — one call, one change, never a batch.

`latency_budget.yaml` is loaded at runtime and validated in the build, so the
component list gains a Soniox endpoint entry and every `measured: false` this work
touches becomes `measured: true` with a named source. Four of its five components
are currently self-declared estimates.

Deploys place real billed calls. A git push alone does not deploy; the Coolify
trigger has to run and the build hash be checked. Read config back with the config
tool, then distrust it and confirm by measurement.

Revert: a tagged rollback point before Phase 1, the provider behind config, the
retired detector and its artifacts kept on disk, and the speech-to-text setting
recorded before it changes, since it is org-wide.

## Named risks

- **800 ms needs both legs, and Soniox is only one of them.** Soniox buys about
  0.55 s; the non-reasoning model buys about 0.25 s. Shipping only the first lands
  near 1.0 s and will read as a failure against the target even though it is the
  larger win. Plan and report them as one goal in two parts.
- **The model swap's risk is Telugu quality, not speed.** It is the one change in
  this plan that could make the calls worse while making the numbers better. It
  does not ship on `ttfw.py` alone; it ships on the case battery.
- **The org-wide speech-to-text setting** moves all five agents together.
- **Soniox's 2000 ms default** exceeds the whole turn budget.
- **`vad_force_turn_endpoint` at its default** silently discards the entire reason
  for the change.
- Turn-stop code rewrites measured worse three times and were reverted with the
  instruction to put the path back exactly as it was. This plan adds a branch and
  changes no existing path; that boundary is deliberate and should hold.

## Decisions taken while planning

- Live code is the deployed repo, not the `vaani/` folder in this project.
- No Soniox account yet, so Phase 0 starts with sign-up and the India request.
- Cartesia stays for now; the voice is not changed in the same round as the ear.
- 800 ms stands as the target. An earlier draft of this plan called it
  unreachable, on the strength of a note that the Groq account carried no
  non-reasoning model. That note is from early September, Groq's production
  catalogue lists two, and the benchmark tool for choosing between them is already
  written. The target is held.
