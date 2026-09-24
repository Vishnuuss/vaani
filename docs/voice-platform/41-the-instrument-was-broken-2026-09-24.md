# 41 — The instrument was broken, and it had been for a month

*2026-09-24. No calls placed. Everything here was measured offline.*

## The question this project could not answer

"Does the turn detector cut callers off?" has been asked since 28 August, when
the client first said the agent interrupts him. It was answered three times by
placing a phone call, listening, and arguing. All three attempts measured worse
and were reverted. On 23 September six more calls were spent learning that
`endpoint_min_secs` does nothing at all.

None of that was bad judgement. The measuring instrument was broken.

`replay_turns.py` reads `.tmp/audio/caller`, and **651 of its 654 recordings are
dated 2026-08-28** — the day the Telugu detector went on the call path. So the
corpus predates the thing it is used to judge. Every cut-off percentage ever
quoted in this project describes audio the current detector never handled.

`harvest_vaani_audio.py` says this in its own docstring, in August:

> Every recording this project holds stops on 2026-08-27. Our Telugu detector
> went on the call path on 2026-08-28. So the entire corpus [...] comes from
> calls that happened before the detector existed.

It was written, it was right, and nobody had run it in the month since.

## What was missing to make fresh audio usable

`harvest_vaani_audio.py` pulls both audio legs. `replay()` needs one more thing:
the run's `logs.realtime_feedback_events`, which is where the final caller
transcripts live and therefore what `transcripts(run)` reads. Without it a
recording is not replayable. That is why the harvest sat unused.

`tools/fetch_run_logs.py` closes that gap. 143 records fetched, 0 failures.

## The sweep was also on the wrong axis

`sweep_endpoint.py` varies `min_endpoint_secs`. `TeluguTurnAnalyzer.append_audio`
has two exits:

    fast   telugu_turn.py:711   p >= bar and not blind   -> COMPLETE
    timed  telugu_turn.py:715   silence >= _wait_secs()  -> COMPLETE

`_wait_secs()` is called from :715 and nowhere else, so `min_endpoint_secs`,
`max_endpoint_secs`, `unsure_floor_secs` and `unsure_band` are **unreachable from
a confident turn**, which exits at :711 without ever calling it. Every sweep this
project has run varied a parameter that cannot move the fast path.

`unsure_floor_secs` can never bind on a confident turn under any value: the
branch is `elif frac < self._band()`, frac is 1.0 and `_band()` maxes at 1.0.

Two knobs do reach the fast exit:

    need  = blind_min_silence_ms                                      (250)
    if speech_secs < fragment_secs: need = max(need, blind_short_silence_ms)
    blind = not text_is_fresh and silence_ms < need

`tools/sweep_real_levers.py` sweeps those, prints both failure modes side by
side, and defaults to the fresh corpus.

## The result, on audio the detector actually handled

145 recordings, 923 bursts, `.tmp/audio_new/caller`:

| setting | CUT OFF | wait p50 | wait p90 |
|---|---|---|---|
| **LIVE (blind 250 / frag 0.65)** | **26.5%** | **0.54** | 1.14 |
| blind 180 | 27.2% | 0.50 | 1.14 |
| blind 150 | 27.6% | 0.50 | 1.14 |
| frag 1.00 | 26.5% | 0.54 | 1.14 |
| frag 1.20 | 26.5% | 0.62 | 1.14 |
| blind 180 + frag 1.00 | 27.2% | 0.53 | 1.14 |
| blind 180 + frag 1.20 | 27.1% | 0.60 | 1.14 |

**1. The analyzer would cut callers off on more than one burst in four.** That is
the client's complaint, quantified. It is why `turn_stop_strategy:
turn_analyzer` produced cut-offs on run 1022. The 0.25s endpoint it offers is
real and is not available at an acceptable quality. The old corpus said 20.2%,
so every earlier figure flattered it.

**2. `turn_fragment_secs` is inert.** 26.5% at 0.65, 1.00 and 1.20 alike, and it
only adds wait. It was proposed as the anti-cutoff knob on the strength of a code
reading. It is not one. That is a negative result worth more than a config
change, because it is an experiment nobody now has to pay for.

**3. `blind_min_silence_ms` 250 → 180 buys 40 ms for +0.7pp of cut-offs**, and
150 costs more cut-offs for no further speed. Not taken.

**No row is better on both axes.** The live timer config is the correct
production choice, and now there is evidence for it rather than an argument.

## What this means for 800 ms

Not reachable through turn detection. The endpoint is 0.7s timer + 0.2s Silero,
and the only alternative cuts off a quarter of all bursts.

What is left:
- `speculation_enabled` — was stored `false`, enabled 23 Sep. Worth 0.25-0.33s on
  hit turns at zero cut-off risk, because it changes when the agent THINKS, never
  when it speaks. Not yet measured live.
- The LLM is already at Groq's published floor. `reasoning_effort` is already
  `low` and `none` is not offered on gpt-oss-120b; hedge-3 measures p50 0.325s,
  better than Groq's own published p50. Groq has **no India data centre**, so
  150-250ms of every TTFB is transport that cannot be configured away.

And one framing that should be settled before anything is promised to a client:
0.768s is measured **server-side**. The only independent audio-based benchmark of
commercial platforms — 2,078 turns over real phone calls — puts every one of
Telnyx, ElevenLabs, Bland, Vapi and Retell at **1.3-1.7s median as the caller
hears it**, and found vendor self-reported figures run ~490ms optimistic. Sub-800ms
server-side is at the frontier. Sub-800ms caller-experienced on an Indian PSTN
leg has not been publicly demonstrated by anyone.

## Also shipped since doc 40

| fix | why it mattered |
|---|---|
| `whitelist_numbers` over both prompt halves | the client's own PM Surya Ghar figures were tripping `no_invented_quantity`, so the subsidy answer could be replaced with "I cannot give you the correct figure right now" — intermittently, which is why it read as randomness |
| `triage.SAY_IT_AGAIN` | "హలో"/"ఏమన్నారు"/"అర్థం కాలేదు" interrupted the bot and refunded no ask, so every one cost a question the caller never heard. Run 1031: twelve turns, every field null |
| `_next_needed_question` | a blocked re-ask became the next needed question instead of an apology or a repeat |
| REPAIR_LINE one-shot | run 1023 said "I could not hear you" four times to a caller it had transcribed perfectly |
| `SAFE_CLOSE` / `SAFE_FALLBACK` rewritten | "మంచి రోజు సార్" is a calque nobody says, `సార్` is wrong for half a call list, and it was the last thing every caller heard |
| office-row TRIGGER | `'మీరు ఎక్కడి నుంచి, మీరు?'` matched nothing — Telugu vowel signs attach directly to the consonant, so `ఎక్కడ\s*` fails on `ఎక్కడి`. 6 improvements, 1 correction, 0 regressions |
| Soniox India region URL | without it the agent was deaf; STT 0.686s -> 0.23s |

## Still open

- **3,758 characters of the prompt are unreachable** — the entire PM Surya Ghar
  section and eleven written answers sit below `## Reference` in no `**bold**`
  row, so the model has never seen them. A caller asking about net metering gets
  "I will find out and tell you" while the answer sits in the file.
- The negative `user_turn_secs` clamp is written but lives in the `pipecat`
  submodule and ships separately. It is measurement-only: the number subtracts a
  back-dated VAD anchor from a raw clock and can invert. Run 1022's -0.282s was
  never a caller being cut off.
- No live call has yet verified any of yesterday's fixes. Three attempts ended at
  9s, 23s and no-answer.
