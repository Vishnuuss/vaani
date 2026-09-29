# 38 — Soniox live on MB Solar: faster, and it cuts people off

*2026-09-18. Two real calls, runs 977 and 978, build `40006cb00546`.*

## What was run

MB Solar (wf2) only, via a per-workflow `model_overrides.stt`. The other four
agents stayed on Sarvam throughout. Mode was `stt-rt-v5-turns`, meaning Soniox's
semantic endpointing owned the turn and `stt_uses_external_turns` returned True,
which bypasses both the 0.7s Dograh timer and the Telugu assist model.

## Speed: a real, measured win

| | baseline run 972 | run 977 | run 978 |
|---|---|---|---|
| endpoint p50 | 0.906 s floor | **0.439 s** | 0.767 s |
| turns under 0.6 s | 0 of 69 (8 calls) | **10 of 13** | 3 of 6 |
| total p50 | 1.534 s | **1.021 s** | 1.344 s |
| LLM | 0.453 s | 0.607 s | 0.519 s |
| TTS | 0.183 s | 0.107 s | 0.088 s |

Run 977's endpoint p50 of 0.439 s is the fastest this project has recorded. The
0.9 s floor that 69 consecutive turns could not break is gone: it was Silero's
0.2 s plus `dograh_speech_timeout_secs` 0.7, and handing the turn to Soniox
removes both.

## Quality: it cut him off, and the double replies came back

The transcripts say it plainly. Run 978:

    USER : ఆ, ప్రస్తుతానికి అయితే మాది—
    USER : అమరావతి అండి, మాది.
    BOT  : మంచిది, మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?
    BOT  : ఇల్లు, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా ఏది మీకు ఉంది?

The dash is the turn ending mid-sentence. Two bot messages with no caller
utterance between them is a **double reply**, which `call_grade.py` counts
because it is the "it is not listening" complaint in its purest form. The
mechanism is visible: Soniox ends the turn early, the model answers a fragment,
the rest of the sentence arrives, and it answers again.

Run 977 carries the caller's own verdict:

    USER : ప్రశ్న క్వశ్చన్ కి ఆన్సర్ ఇస్తారా?
           ఇంకా మాట్లాడేది కంప్లీట్ చేయలేదు కదా

"I have not finished talking yet." That is the complaint this whole project
exists to fix, produced by the change meant to fix it.

Corroborated by the timings: both calls show **negative endpoints**, -0.208 s on
run 977 turn 6 and -0.177 s on run 978 turn 6. The turn ended before the
reference speech-end.

And one hang: run 977 turn 4 waited **8.489 s**. Handing turns to Soniox raises
the stop backstop from 5 s to 30 s, so a missed endpoint is no longer bounded at
five seconds.

## The double replies are a known regression, re-created

`dograh_speech_timeout_secs` was raised 0.35 → 0.7 on 13 September precisely
because it killed the double replies and took capture from 2/6 to 5/6. Running
Soniox in turns mode bypasses that timer entirely, so the guard is switched off
and the failure it was guarding against returned on the first call.

## Field capture

Both calls captured 3 of 6, against the baseline's 6 of 6. That comparison is
not yet fair: the caller spent most of run 977 arguing rather than answering,
and `property_type` and `monthly_bill` were never actually given. It needs a
call where the questions are answered before it means anything.

## Verdict

**Turns mode is rejected on live evidence.** It is the fastest configuration
ever measured here and it is worse to talk to, which is the trade
`latency_budget.yaml` warned about in writing: *"A latency win purchased with
interruptions is NOT A WIN."*

The offline probe in doc 37 called this right by accident. Its 30.6% false-cutoff
figure came from a broken harness and should not be quoted, but the direction
held.

**Ears-only mode is the configuration to test next**, and it is a one-word config
change from `stt-rt-v5-turns` to `stt-rt-v5`. Soniox endpointing switches off,
`stt_uses_external_turns` returns False, Silero and the Telugu model keep owning
the turn, and the 0.7 s timer comes back. What survives is the better transcript
and a measured 307 ms flush against Sarvam's 0.373–0.45 s.

## The other finding, which may outlast the latency work

Soniox transcribes Telugu numbers far better than Sarvam, and capturing a bill or
loan figure is this agent's entire job.

    soniox:  15 లక్షలు కావాలి, 10 లక్షలు వద్దు.
    sarvam:  ఆ అది, పైకి వస్తుంది కదా. కదండీ లక్షలు కావాలి, పది లక్షలు వద్దు.

Sarvam lost the figure 15. On another burst Sarvam emitted the same garbage token
five times where Soniox wrote `11, 15 అండి`. This is worth a measured comparison
of field-capture rate over three calls each way, independent of any latency claim.

## Infrastructure problems found on the way

- **Cartesia is out of credit**, and the error is explicit: *"Model credits limit
  reached: Please upgrade your subscription."* Five calls returned HTTP 402 with
  no voice at all. Two different Cartesia keys were tried and both are
  exhausted. Every agent on the account is affected, not just MB Solar.
- **The server is flapping.** Roughly half of all requests to
  `vaani-api`, `storage`, `voice` and `coolify` time out, all four on one IP.
  Every tool in `tools/` needed retry loops today. This is worth diagnosing on
  its own.

## Three registrations, none of which announces itself

Adding an STT provider to this codebase needs four edits, and three of them fail
with messages that point somewhere else:

| missing | symptom |
|---|---|
| `ServiceProviders` enum | provider simply unknown |
| `REGISTRY` via `@register_stt` | 422 *"Pipeline legacy configuration is incomplete"* |
| `_validator_map` | 422 *"Invalid soniox API key"* |
| `STTConfig` union | 422 *"Input tag ... does not match any of the expected tags"* |

Only the last names the real problem.

## Operational note

`PUT /api/v1/workflow/{id}` **replaces** `workflow_configurations` wholesale. A
write containing one key wipes the other twenty-nine. That happened today and was
restored within a minute from a copy read earlier in the session. Always send the
full object; `tools/vaani_config.py` does, a hand-rolled curl does not.
