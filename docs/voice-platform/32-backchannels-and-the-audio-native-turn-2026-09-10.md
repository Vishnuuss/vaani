# Backchannels, barge-in, and the audio-native turn model

**10 September 2026 · Project Vaani**

Three things the client asked for in one loop: read the turn from the audio
rather than from 16 prosody numbers, stop the agent being interrupted by "haa"
and "avunu", and keep the latency honest. This is what was measured and what
was decided.

---

## 1. The lexicon was a guess. It isn't any more.

`barge_in.BACKCHANNELS` held 40 words written from memory of Telugu phone
manners. That is a guess, and a guess is expensive in both directions here: a
word wrongly **in** the set means the caller cannot interrupt with it, and a
word wrongly **out** means the bot is cut off by someone who was only saying
*go on*.

2,097 run logs hold every `rtf-user-transcription` this project has ever
produced. The words were already there. `tools/mine_backchannels.py` counts,
for every short caller utterance, how often it was said **and** how often it
directly followed a bot *question*. That second number is the discriminator:
"సరే" after *"shall we book a survey?"* is the answer **yes**, and gating it
would lose the booking.

### The first finding argues against the obvious change

The request was "add 20+ more words like haa, avunu, sare". The data says
**don't** — at least not the way it sounds.

Across 2,097 calls there are only **106 distinct one-word caller utterances**.
The frequent ones that are *not* already in the lexicon are all **content**:

| utterance | meaning | n | said in answer to a question |
|---|---|---:|---:|
| సొంతమే | "it's owned" | 79 | 85% |
| చెప్పండి | "go ahead / tell me" | 102 | 90% |
| అవునండి | "yes" *with* the polite particle | 49 | 100% |
| లేదండి | "no" | 19 | 95% |
| ఉంది | "I have" | 9 | 100% |

Every one of them is an **answer**. Adding them would make the agent deaf to a
reply — the same class of defect as run 853's repeat-question bug, arriving
through a different door.

So the set grew **sideways only**: 40 → 70 words, all of them transcription
variants of sounds already accepted (`ఆహా`, `ఊం`, `హూ`, `ఉమ్`, `acha`, `aan`,
`hoon`, `umm`, `sure`…). No new meanings entered the set.

### The second finding is the half the hand-written list missed

There is a class that was in **neither** lexicon: the caller signalling that
the **channel** has failed.

> **"హలో" is the single most common thing anyone says to this agent — 381
> times, three times more than the next.**

A caller saying *"hello?"* while the bot is speaking is a caller who **cannot
hear it**. Continuing to talk over them is the worst available response, and
until today it was the guaranteed one. The same for the repair words —
"ఏమన్నారు" *(what did you say)* 23, "ఏంది" 16, "ఏంటమ్మా" 18, "అర్థం కాలేదు"
*(I didn't understand)* 9.

And two that are not conversation at all but **compliance**:

- **"నాకు call చేయొద్దు"** — *don't call me* — **26 times**
- **"నాకు ఇంట్రెస్ట్ లేదు"** — *not interested* — **40 times** across two spellings

A do-not-call request landing after the bot finishes its paragraph is a
regulatory problem, not a UX one. These now stop the bot on the first syllable,
because the lexical check runs **before** the duration floor and bypasses it.

Finally, the client's own complaint, quantified:

> **"ఆగండి మాట్లాడనివ్వట్లేదు"** — *"wait, you're not letting me speak"* —
> said in **25 calls**.

---

## 2. The barge-in floor was 0.35 s because someone picked it

It is now measured. `tools/label_backchannels.py` labelled **705 caller bursts
across 179 calls** using **both** legs of the audio, so "he spoke while we were
speaking" is *known* rather than assumed — the bot's own track was indexed in
every run log and had never once been fetched.

| floor | backchannels held | real interrupts delayed |
|---:|---:|---:|
| 0.35 s | 37% | 8.5% ← the guess |
| **0.50 s** | **53%** | **12.8%** ← chosen |
| 0.70 s | 74% | 17.4% |
| 1.00 s | 97% | 22.3% |

0.50 s is the first floor above the backchannel median (0.46 s), which is why
it is the first that holds more than half of them.

**"Delayed" is the honest word** in the right-hand column, and it is why the
step is affordable. The gate suppresses only `enable_interruptions`. The
caller's turn still starts, his words are still transcribed and still answered
— the bot merely finishes the sentence it had already begun. Nothing is lost.

### And the honest limit

Duration alone cannot solve this. From the same labels:

    backchannel   p50 0.46 s   p90  0.93 s
    wait          p50 2.38 s   p90 21.42 s

They separate — but **22% of real interruptions fall inside the backchannel
duration range**. No single number is ever clean. That is the measured argument
for a four-state model rather than a better constant, and it is the next piece
of work, not this one.

---

## 3. The audio-native turn model

`telugu_turn.extract_features` describes a turn with 16 hand-built prosody
numbers — energy slope, f0 slope, voicing fraction, spectral centroid. **How**
the voice moved, never **what** was said. It cannot separate the two cases the
agent gets wrong all day, because they are acoustically alike and differ only
in meaning:

    "నాకు కావాలి..."   (I want...)   NOT finished
    "అవును"            (yes)         finished

The fix follows the 2026 work (Smart Turn v3, FastTurn, Easy Turn): keep the
pretrained speech encoder, retrain the head. Smart Turn v3's own head covers 23
languages and **not Telugu** — measured, doc 30, it is effectively a 2.2 s
stopwatch on our calls. So the Whisper-tiny **encoder** was kept and the head
retrained on **2,950 real Telugu bursts from 647 real calls**, split by *call*,
selected at the declared 2% false-cutoff bar:

| | endable early | AUC |
|---|---:|---:|
| prosody 16 (what runs today) | 2.7% | 0.550 |
| frozen encoder + linear probe | 8.3% | 0.619 |
| **fine-tuned encoder** | **10.8%** | **0.670** |

### The measurement that actually decides it

A better classifier is not a better agent. The control this project now insists
on is a **model-free stopwatch** made to wait the *same* amount of time — the
comparison that reversed the linear model's promotion in doc 30.

On 30 real recordings (`tools/sweep_audio_native.py`) — superseded by the
60-call numbers in section 4, and kept because the two disagree by more than
noise, which is itself the lesson about subset sizes:

| setting | cut off | wait p50 | stopwatch at same wait | verdict |
|---|---:|---:|---:|---|
| inherited | 14.8% | 1.09 s | 16.2% | beats |
| **unsure band 0.90, min 0.30** | **16.0%** | **0.76 s** | **20.8%** | **beats** |
| min 0.25 / max 1.20 | 18.5% | 0.70 s | 23.4% | beats |
| min 0.40 / max 1.40 | 19.8% | 0.90 s | 17.9% | *worse* |
| min 0.20 / max 0.90 | 22.2% | 0.62 s | 26.8% | beats |

Against what runs today — prosody at **23.0% cut off, 0.48 s wait** — the
chosen setting is **7 points better** for 0.28 s more patience, and it beats the
free stopwatch at its own wait by 4.8 points. **This is the first turn model on
this project to do that.**

### The cost, stated plainly

The encoder is not free. Measured on this machine over 40 real mel windows:

| runtime | per decision |
|---|---:|
| PyTorch fp32 | 148 ms |
| **ONNX fp32** | **69 ms idle, 113 ms p50 under load, 169 ms p90** |
| ONNX int8 | **discarded** — 93.3% verdict agreement *and* slower |

The int8 path was refused automatically by the export guard, which requires
**identical verdicts** on real windows before it will keep a graph. Recorded so
nobody spends the afternoon on it again.

**That inference time is additive to the wait**, and the sweep above does not
include it — a replay measures decision points in audio time, not wall clock.
This is stated rather than buried: the honest end-to-end figure is the sweep's
wait *plus* roughly one to three probes of encoder time.

---

## Status

| | |
|---|---|
| Backchannel lexicon | 70 words, mined from 2,097 calls |
| Stop-signal lexicon | 38 words + 31 phrases, incl. do-not-call and "hello?" |
| Barge-in floor | 0.35 s → 0.50 s, measured from 705 labelled bursts |
| Barge-in gate | wired, config-gated |
| `turn_model: audio-native` | selectable via config; **not yet deployed** |
| Chosen endpoint | band 0.90, min 0.30, **ceiling 2.20, unsure floor 0.90** |
| Measured, 60 calls | **14.5% cut off** vs the live agent's 23.0% |
| Encoder cost | **1.5 inferences per turn** (was 64), 0.2-4.9% of realtime |
| Four-state model | data foundation complete (705 bursts); head not trained |

Tests: 55 existing pass, 8 new lock what the mining decided — including that a
content answer is never gated as noise.

## What is not claimed

- The 4-state labels are **heuristic**, from energy on two legs, not human
  annotation. Good enough to train on; not good enough to quote as truth.
- The sweep above is **30 calls**. A 60-call verification is the gate before
  this reaches a live client.
- Sub-600 ms server latency is **not** solved by any of this. Run 853 measured
  p50 1.055 s of which the LLM was 0.561 s, `reasoning_effort: low` already
  applied. The remaining lever is a non-reasoning model, which is a quality
  trade on Telugu and a decision to be made against measurements.

---

## 4. "if I lag more like ahhhhhhhhhhhhh, it is not waiting"

The client's report, and the most useful sentence of the day, because chasing
it found a dead guard and an unexamined ceiling.

### What a drawn-out filler does to the endpoint

`completeness.HESITATIONS` already knows "ఆ" and "hmm" are stalling noises, but
it reads TEXT, and production runs `saarika:v2.5`, which emits no interim
transcripts — no text exists until after the caller has stopped, which is after
the decision was taken.

The 16 prosody features cannot see it either. A three-second "aaaahhhh" is
long, steady, voiced and level, which reads as a calm finished sentence in every
one of them. Worse, because it is *long*, the fragment floor — the one guard
that buys extra patience — does not apply: `_bar()` only raises the bar when
`_speech_secs < fragment_secs`. **The case that most obviously needs patience is
the case that asks for least.**

### A hypothesis, measured and dropped

The first idea was to detect the filler acoustically: a filled pause is a
*steady state* — pitch, formants and energy all stop moving, while real speech
is continuous change. `tools/measure_held_vowel.py` scores four stationarity
numbers over the last 0.5 s of every labelled burst.

**It does not separate.** Across 574 bursts from 120 calls the distributions for
"kept going" and "finished" sit on top of each other, and spectral flux runs the
*wrong* way (p10 0.392 continued vs 0.295 finished). Recorded here so nobody
rebuilds it: the idea is plausible, phonetically sound, and not supported by
this data.

### The two real defects

Probing the detectors directly with a synthetic held vowel found both.

**The long-answer floor is dead code.** `_wait_secs` computes
`hi - frac*(hi-lo)` = `1.40 - frac*1.10`, which cannot fall below 0.30 for any
`frac ≤ 1`. So `unsure_floor_secs = 0.30` **never binds**. The protection built
for run 319's caller — *"I can't even finish answering, why are you moving to the
next question?"* — has been inert ever since `min_endpoint_secs` rose off 0.05.

**`max_endpoint_secs` is a hard ceiling on patience.** `_wait_secs` ends with
`return min(wait, hi)`. Every sweep this project has ever run held `hi` at
1.40 s, so *"wait longer"* has never actually been on the table. A caller who
stalls past 1.4 s gets talked over however unfinished the model says he is.

### Swept, on 60 calls, against the stopwatch control

| setting | cut off | wait p50 | stopwatch | verdict |
|---|---:|---:|---:|---|
| floor 0.30 (dead), ceiling 1.40 | 17.4% | 0.88 s | 18.1% | beats |
| floor 0.70, ceiling 1.40 | 16.6% | **0.88 s** | 18.1% | beats |
| floor 0.90, ceiling 1.80 | 15.7% | 1.08 s | 16.2% | *worse* |
| **floor 0.90, ceiling 2.20** | **14.5%** | 1.08 s | 16.2% | **beats** |

Raising the floor to 0.70 is **free** — 0.8 points of cut-off at an identical
median wait — because the floor only binds on the turns the model is unsure
about. That is the property the run-319 comment claimed and never had.

The chosen setting is **floor 0.90, ceiling 2.20**: 14.5% against the live
agent's 23.0%, **8.5 points fewer interruptions**, still beating a stopwatch
made to wait the same 1.08 s. Note the median wait is 1.08 s and not 2.20 s —
the ceiling is reached only where the model says clearly-unfinished. Fast when
sure, patient when not.

On the client's actual case, a held vowel now gets **1.3–1.9 s** instead of the
1.40 s hard cap:

| held "ahhh" | live (prosody) | new |
|---:|---:|---:|
| 0.4 s | 1.40 s | 1.94 s |
| 3.0 s | 1.40 s | 1.71 s |
| 8.0 s | 1.40 s | 1.63 s |

Nothing here is a script or a word list. It is the same acoustic verdict feeding
timers that were already there — two of which turned out not to be reachable.
