# Phase 0 — Stack Benchmark

This measures the three vendors **before** we build any pipeline around them.

It answers one question: **can this stack actually do sub-600ms, and does
`gpt-oss-120b` get to stay on the realtime path?**

Nothing in Phase 1 gets built until this has been run.

---

## Step 1 — Add the missing API keys

Your `.env` currently has **no Groq, Cartesia or Sarvam keys**. Add these five
lines to `.env` (in the project root, next to `README.md`):

```
GROQ_API_KEY=gsk_...
CARTESIA_API_KEY=sk_car_...
CARTESIA_VOICE_ID=<the exact Telugu/Hindi voice you will ship>
SARVAM_API_KEY=...
SARVAM_LANGUAGE=te-IN
```

`CARTESIA_VOICE_ID` matters: time-to-first-byte varies by voice, so benchmarking
a different voice than the one you ship gives a number you cannot reproduce.

## Step 2 — Install the two libraries it needs

```
pip install httpx websockets
```

## Step 3 — Run it

```
python -m vaani.bench.run
```

Takes about two minutes. It prints a table and saves the raw numbers to
`.tmp/bench/phase0-<timestamp>.json`.

To run just one vendor:

```
python -m vaani.bench.run --only groq
```

## Step 4 — Run it again from the Mumbai box

**The numbers from your laptop are not the real numbers.** Latency is mostly
geography. Once the Mumbai server exists, run the exact same command there — that
result is the one we design against.

---

## How to read the output

Look at three lines only:

**1. `t_first_FINAL` under GROQ** — the one that decides everything.

`gpt-oss-120b` thinks before it answers, and we have to throw that thinking away.
So `t_first_delta` will look fast and *lie to you*. `t_first_FINAL` is when the
real answer starts, which is the only moment TTS can begin.

If `gpt-oss-120b` is much slower here than `llama-3.3-70b`, we move 120b off the
live call and use it for post-call work instead. That decision gets made from
this number, not from opinion.

**2. `=> VERDICT` under NETWORK** — confirms whether Groq's US distance is as
expensive as expected. If it says "speculation is MANDATORY", that is the plan's
Finding 2 proven with real numbers.

**3. `=> DECISION` under ROLL-UP** — adds the measured parts to the four
assumed parts and tells you whether 600ms is reachable.

The roll-up marks every line `[measured]` or `[assumed]`. The four assumed ones
(PSTN in/out, our endpointer, first clause) need a live Vobiz leg before they
become real. They are listed openly rather than hidden in a total.

---

## What is deliberately not measured yet

- **The PSTN legs.** Blocked on the Vobiz answer (SIP trunk vs websocket vs
  REST-only). Until then they are assumptions, and they are labelled as such.
- **Our own endpointer.** It does not exist yet — Phase 2 builds it. The 120ms
  in the roll-up is the target it has to hit, not a measurement.

---

## Already learned (from the Sarvam spec, confirmed 2026-08-25)

Three things that improve the approved plan:

1. **`encoding=mulaw`, `sample_rate=8000` are supported natively.** So there is
   no resample on the way *in* either — mu-law 8k runs end to end, both
   directions, untouched. The plan assumed we'd have to upsample once.
2. **`endpointing=manual` exists.** Our semantic endpointer can decide the turn
   is over and send `flush`, instead of paying Sarvam's built-in VAD wait
   (`silence_duration_ms`, default **500ms**). That default is exactly the fixed
   silence tax Finding 1 is about — manual mode deletes it. The benchmark
   measures both so we can see the saving.
3. **`stream_type=fast`** exists alongside `balanced`. We use `fast`.

Sources:
- [Sarvam realtime STT WebSocket reference](https://docs.sarvam.ai/api-reference/speech-to-text/transcribe/realtime/ws)
- [Sarvam STT API overview](https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/overview)
