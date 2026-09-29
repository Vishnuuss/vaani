# 37 — Soniox measured on MB Solar: the endpoint is rejected, the ear is not

*2026-09-18. Offline only. No live calls spent, no config changed.*

## What was asked

Trial Soniox on MB Solar (wf2) alone. If it works, roll to the other four.

## What was measured

114 MB Solar caller bursts from `turnstops_real.jsonl`, the hand-corrected
corpus, filtered to the ones that carry a transcript. 63 labelled real turn
ends, 51 labelled mid-turn. Audio streamed at wall-clock speed at 8 kHz, the
real telephony rate. `stt-rt-v5`, `language_hints=["te","en"]`.

Two ways of using Soniox were measured separately, because Pipecat offers both
and they are not variations of one thing. They are opposite architectures.

## Mode A — Soniox owns the turn. REJECTED.

`vad_force_turn_endpoint=False`. Soniox's semantic endpointing decides when the
caller has finished and emits `<end>`.

| | Soniox | live detector | dumb 0.6s timer | bar |
|---|---|---|---|---|
| false cutoffs | **30.6%** | 6.45% | 19.1% | 2.0% |
| wait on real ends, p50 | 0.480 s | — | 0.80 s | — |
| under 0.6 s | 36 of 37 | — | — | — |
| real ends detected | 37 of 62 (60%) | — | — | — |

The speed is real and it is the best this project has ever measured. 36 of 37
detected ends came in under 0.6 s, against production's 0 of 69.

It is bought entirely with interruptions. **30.6% false cutoffs is five times
the live detector and worse than a stopwatch.** It also misses 40% of real ends
outright.

The cause is visible in the corpus before any API call is made:

    mid-turn pause, p50     0.44 s     <- the caller is thinking, not finished
    Soniox fires at         0.48 s

Those two numbers are 40 ms apart. Soniox cannot separate a Telugu thinking
pause from a finished sentence, so it fires on nearly a third of them. This is
the project's standing lesson arriving on schedule: *"Faster endpointing buys
interruptions. A latency win purchased with interruptions is NOT A WIN."*

Two knob findings, both of the kind this codebase keeps tripping over:

- **`max_endpoint_delay_ms` does not bind.** 500 ms and 2000 ms produced
  identical waits, 0.480 s p50, at both sample rates. The semantic decision
  fires long before the cap, so the cap never applies. Setting it does nothing.
- **Aggressive settings add variance, not speed.**
  `endpoint_latency_adjustment_level=3` with `endpoint_sensitivity=0.8` left the
  median at 0.480 s and spread the range from 0.060 s to 1.140 s. That is how
  cut-offs are bought.
- **8 kHz and 16 kHz measured identical.** No reason to upsample phone audio.

## Mode B — our VAD owns the turn, Soniox flushes on demand. VIABLE.

`vad_force_turn_endpoint=True`, which is Pipecat's default. Soniox endpointing
is switched off entirely. When `VADUserStoppedSpeakingFrame` fires, the service
sends `{"type": "finalize"}` and Soniox returns final tokens immediately.

Measured flush latency, speech end plus Silero's 0.2 s, then finalize:

| | ms |
|---|---|
| p50 | 307 |
| p90 | 358 |
| min | 280 |
| max | 387 |

n=5 of 8 bursts; 3 returned no final after the finalize and that is an open
item below.

**Turn-taking does not change at all.** Silero plus `telugu_turn_assist` keep
deciding when the caller has finished, so the false cutoff rate stays whatever
it is today. Nothing in the rejected Mode A result applies here. This also
respects the standing instruction not to rewrite the turn-stop path, which
measured worse on runs 893, 894 and 895 and was reverted.

What it buys is the transcript arriving sooner, which is what
`dograh_speech_timeout_secs` is currently padding for. MB Solar's live config:

    dograh_speech_timeout_secs   0.7      <- the binding lever
    Silero stop_secs             0.2      <- hardcoded, no config key
    endpoint floor               0.9

Sarvam delivers a final transcript in 0.373–0.45 s, p99 1.17 s, which is why
the timer sits at 0.7. Soniox delivers in 0.307 s, p90 0.358 s. That leaves room
to bring the timer down to roughly 0.45 s with margin over the p90, giving an
endpoint floor near **0.65 s against today's 0.9 s**.

This is smaller than Mode A's headline and it is the one that can actually ship.
It must still be verified on a real call, because raising this same timer from
0.35 to 0.7 is what killed the double replies and took capture from 2/6 to 5/6.
Lowering it is walking back toward that failure, so it goes one step at a time.

## The finding that may matter more than latency

Soniox's Telugu is better than Sarvam's, and the gap is largest on exactly the
thing this agent exists to capture: numbers.

    soniox:  15 లక్షలు కావాలి, 10 లక్షలు వద్దు.
    sarvam:  ఆ అది, పైకి వస్తుంది కదా. కదండీ లక్షలు కావాలి, పది లక్షలు వద్దు.

Sarvam lost the figure 15 entirely and padded with noise. Another:

    soniox:  11, 15 అండి.
    sarvam:  పదైది అండి పదైది, పదైది, పదైది, పదైది, పది లక్షలు.

Sarvam repeated a garbage token five times. And on code-switching:

    soniox:  లైక్, నాకు బిజినెస్ అయితే సరిపోతుందండి, బిజినెస్.
    sarvam:  ఆ like ఆ నాకు business ఏ సరిపోతుంది. Business

Soniox writes digits where Sarvam writes words or fails outright. Given that
`amounts.py` has to parse a bill or a loan figure out of this text, and given
the "that cannot be your bill" defect that threw a lead's data away, transcript
accuracy on numbers is plausibly worth more here than 250 ms.

That claim is a read of six samples and is **not yet measured**. The proper test
is field-capture rate over at least three calls each way, which is the gate the
last STT swap was held to.

Soniox also streamed non-final tokens on 62 of 111 bursts, with the first token
arriving during speech on 47 of them. `saarika:v2.5` emits none at all. That
reopens preemptive generation as a question, though the measured match ceiling
of 1-in-6 still stands until re-measured.

## Correction to yesterday's plan

Yesterday's plan said the speech-to-text setting is organisation-level and that
changing it moves all five agents together. **That is wrong.**
`workflow_configurations.model_overrides` carries an `stt` section that is
deep-merged per workflow, and `resolve.py` explicitly handles a provider change
there. MB Solar can run Soniox while the other four stay on Sarvam, which is
exactly what was asked for.

## Recommendation

Ship Mode B to MB Solar only. Do not ship Mode A at all.

1. Add `SONIOX = "soniox"` to `ServiceProviders` in
   `api/services/configuration/registry.py`.
2. Add a Soniox branch to `create_stt_service` in
   `api/services/pipecat/service_factory.py`, with
   `vad_force_turn_endpoint=True` and `language_hints=["te","en"]`, not strict.
3. Leave `stt_uses_external_turns()` **untouched**. It returns False for Soniox
   today, which is correct for Mode B and is what keeps turn-taking unchanged.
4. Put Soniox on wf2 through `model_overrides`, leaving the org config alone.
5. Only then, and as a separate step with its own call, lower
   `dograh_speech_timeout_secs` from 0.7 toward 0.45.

Step 5 is a separate call from steps 1 to 4 on purpose. Landing the provider and
the timer together would make a regression unattributable.

## Open items

- **3 of 8 bursts returned no final token after `finalize`.** Unexplained.
  Likely the finalize landed after the clip's audio was exhausted, but it must
  be understood before this ships: a finalize that returns nothing is a silent
  turn, and silent turns are already an unexplained failure on this project.
- One burst returned HTTP 408 `request_timeout` from Soniox. One in 114.
- `context.terms` was not exercised. It is free and should carry the lexicon and
  the lead's name before any live call.
- India region not requested yet. Everything above ran against the US endpoint,
  so the measured flush of 307 ms includes a trans-Pacific round trip and should
  improve.

## Tooling added

`tools/probe_soniox.py` — replays labelled bursts and reports false cutoffs and
wait as two separate numbers, per `model_registry.py`'s promotion rule. It
measures in the **audio timeline**, not wall clock: pacing 100 ms chunks with
`asyncio.sleep` drifts about 10% on Windows, which inflated the first run by
0.4 s and would have condemned a config that was fine.
