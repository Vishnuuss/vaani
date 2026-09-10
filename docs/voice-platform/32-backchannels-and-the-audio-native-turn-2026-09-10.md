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

On 30 real recordings (`tools/sweep_audio_native.py`):

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
| `turn_model: audio-native` | selectable via config; nothing selects it yet |
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
