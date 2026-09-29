# 42 — Condition check, the upgrade plan, and TurnSense

*2026-09-29. No calls placed: the server was unreachable all evening. Everything
here was built and measured offline, on 145 real recent calls.*

## In one paragraph

The Vaani server (200.141.7.188, Hostinger Mumbai) is down: no port answers, from
this laptop or from seven other countries, so nothing live could be checked,
changed or deployed. Offline, three things were found and three things were
built. Found: the replay instrument was still wrong in two ways; the live
"Telugu assist" adds interruptions and buys no speed; and `speculation_enabled`
has never done anything. Built: **TurnSense-te**, a Telugu end-of-turn model that
reads the words Soniox hands over at each pause (results in section 4); a fixed
MB Solar answer bank (v5); and a benchmark of every LLM reachable from India.
Nothing is live. Section 7 is the ship list for when the server is back.

---

## 1. Condition today

| | state | evidence |
|---|---|---|
| Server | **DOWN** | TCP 443 and 22 time out from laptop + CY, DE, IR, VN, CA, CH, IL; ICMP from HU, IL, US |
| Code on `vaani/main` | 4c6f2ac9 (24 Sep), healthy | 14 gate suites: 94 pass, 1 xfail |
| Pre-existing failure | `test_run870...retire_the_field_even_with_no_value` | fails on main with no local change |
| OpenAI account | out of credits | HTTP 429 `insufficient_quota` |
| Sarvam LLMs | `sarvam-m`, `sarvam-30b` retired | only `sarvam-105b(-conversations)` remain |
| Groq catalogue | gpt-oss-120b/20b, qwen3.6-27b, qwen3.8-27b | llama models gone |

The server needs a human at hPanel: VPS status, a restart, or a billing
suspension. There is no Hostinger API token in `.env` and the Hostinger connector
needs a sign-in, so it could not be diagnosed further from here.

## 2. The instrument was wrong twice more

Doc 41 fixed the corpus. Building a bench that replays the *production*
arrangement exposed two more defects in how every earlier number was measured:

1. **The energy VAD is not production's VAD.** `true_latency.regions` thresholds
   at 0.35 x the 75th-percentile energy of the caller track. A caller track is
   mostly line noise, so that threshold sits inside the noise: on run 1021 it
   drew a 5.1 s "speech burst" around a one-second "ఆ... మాట్లాడొచ్చు". The bench
   now runs pipecat's own `SileroVADAnalyzer` with production's `VADParams`.
   Cut-off rates fall roughly threefold (the 0.7 s timer: 21.0% -> 7.5%), because
   most "resumptions" the old VAD counted were noise.
2. **The recording clock drifts 0.8-1.4% against the event log** (fitted slopes
   1.0082 on run 1021, 1.0141 on run 992). Two minutes into a call the logged
   transcript "arrives" half a second *before* the caller stops talking, which
   flatters any policy that reads words. Each call now gets its own linear fit.

And transcripts: the run log holds one text per *turn*, never per pause (no two
logged finals are less than 0.75 s apart). So every caller recording was
re-streamed through Soniox `stt-rt-v5` on the India host, finalizing at every
Silero stop exactly as production does, to recover the text the detector would
really have had at each pause (`tools/turnsense/soniox_rt_segments.py`).

## 3. The honest baseline

145 fresh calls (13-23 Sep), 1,086 pauses, production VAD, per-pause text:

| arrangement | cut off | wait p50 (last word -> turn end) |
|---|---|---|
| timer 0.30 s | 12.3% | 0.48 s |
| timer 0.50 s | 9.7% | 0.68 s |
| **timer 0.70 s (what 23 Sep calls ran: flat 0.90 s endpoints)** | **7.5%** | **0.88 s** |
| timer 1.00 s | 5.9% | 1.18 s |
| timer 1.50 s | 4.5% | 1.68 s |
| live: timer 0.7 racing the Telugu assist (stored 23 Sep config) | 9.5% | 0.88 s |

**The assist adds two points of interruptions and buys nothing on the median.**
That is what `turn_taking.py` itself predicts ("two detectors do not check each
other, the eager one simply wins"). One caveat, stated rather than hidden: run 997
(20 Sep) logged assist endpoints of 0.30-0.38 s on 19 of 20 turns, which this
replay does not reproduce with the stored 23 Sep config. The deployed model or
its thresholds evidently differed that day, and the server was not reachable to
read what is live now. The 0.7 s timer is the baseline that reproduces exactly.

**Dead config #3.** `build_speculation_processors()` raises `NotImplementedError`
by design (a hit would speak a reply built without the Vaani prompt and state
block), and `run_pipeline` catches it and logs "Speculation DISABLED". So
`speculation_enabled: True`, set on 23 Sep and pinned by `pre_call_gate`, has
never done anything. Doc 41's "worth 0.25-0.33 s, not yet measured live" was not
live at all.

## 4. TurnSense-te

### What it decides, and when

Silero stops 0.2 s after the caller's last word. pipecat sends Soniox a finalize
and the pause's text lands ~0.25 s later. TurnSense scores that text — with the
agent's last line as context and this caller's own pausing habit so far — as
P(finished), and turns it into a wait:

    p >= 0.85          end NOW, on the words        ~0.45 s after the last word
    0.6 < p < 0.85     wait, interpolated           1.1 -> 1.8 s after the last word
    p <= 0.6           wait 1.6 s of silence        he is mid-sentence
    no text            today's 0.7 s timer          never worse than live

Any speech resets it, exactly as the timer does. It never races another
detector — racing is what made the assist harmful.

### How it was trained

Not on the logs. A logged transcript is a whole TURN (no two finals are less than
0.75 s apart), so the logs cannot say what the caller had said at a short pause,
which is where people get cut off. Instead:

1. every caller recording (797 calls) was replayed through production's Silero
   to find every pause, and through Soniox `stt-rt-v5` on the India host,
   finalizing at each pause exactly as production does, to get the text the
   detector would really have had;
2. each pause was labelled by what the caller DID: spoke again within 1.5 s of
   his last word = not finished; silent 2.5 s or more = finished; in between =
   not trained on. One correction: when his next words were a re-prompt
   ("హలో", "వినపడుతుందా") he was waiting on us, so that pause counts as finished;
3. features: the tail characters of the caller's text (Telugu is verb-final, so
   a finished clause is written in its last few characters), Soniox's own end
   punctuation, the grammar classes production already knew (hesitation,
   connective, dangling number, postposition), what the agent had asked, and the
   caller's pausing habit so far in this call, and whether the answer the agent
   asked for is already in the words (a name after "మీ పేరు?");
4. regularised logistic regression, exported as plain JSON and scored in pure
   Python in well under a millisecond. The server imports the SAME feature code
   the trainer used.

A second model was tried: 3,034 grammar labels from gpt-oss-120b, trained into a
text scorer (grouped-CV AUC 0.884 on those labels) and stacked in. **It changed
nothing** on the replay (within 0.3 points everywhere on the grid), so the
simpler model ships and the teacher is kept as tooling.

### Measured

Five-fold cross-validation by call: every one of the 145 fresh calls is scored by
a model that never saw it. Baselines replayed in the same run on the same calls:

| arrangement | cut off | wait p50 | wait p90 |
|---|---|---|---|
| live: timer 0.7 + assist | 9.7% | 0.88 s | 0.88 s |
| timer 0.7 alone | 7.5% | 0.88 s | 0.88 s |
| a stopwatch as fast as TurnSense (0.3 s) | 12.3% | 0.48 s | 0.48 s |
| **TurnSense, shipped defaults (0.85 / 0.6 / mid 0.9 / max 1.6)** | **5.7%** | **0.46 s** | 1.44 s |
| TurnSense, less patient (max 1.3, mid 0.7) | 6.8% | 0.46 s | 1.18 s |
| TurnSense, slower but safer (fast 0.9, max 1.6) | 4.6% | 1.12 s | 1.50 s |
| the ceiling: an oracle that knows if he continues | 0.2% | 0.46 s | 0.46 s |

Fold AUC 0.72-0.83 (grouped-CV 0.77 on all pauses); the rules production reads
today score 0.53 and Soniox's punctuation alone 0.58 on the same pauses.

**41% fewer interruptions than live, and 0.42 s faster on the median turn.**
Against a stopwatch that is equally fast it cuts interruptions by more than half
(12.3% -> 5.7%). The price is paid where it should be: the 90th-percentile wait
rises from 0.88 s to 1.44 s, on the turns the words say are unfinished.

What the shipped model says about the cases this project was built around:

| agent asked | caller said | P(finished) | action |
|---|---|---|---|
| మీ పేరు చెప్పగలరా? | నా పేరు సురేష్. | 0.93 | answer now |
| మీ పేరు చెప్పగలరా? | నా పేరు రవి. | 0.88 | answer now (was 0.85 -- see below) |
| బిల్లు ఎంత వస్తుంది? | 2 లక్షలు వస్తున్నాయి. | 0.92 | answer now |
| బిల్లు ఎంత వస్తుంది? | మాది. (run 1021) | 0.50 | wait for the amount |
| బిల్లు ఎంత వస్తుంది? | అంటే. | 0.56 | wait |
| సొంత ఇల్లా, అపార్ట్‌మెంటా...? | మాది సొంత ఇల్లే, కానీ మాది కొంచెం. | 0.73 | wait a little |
| — | హలో. నేను— | 0.49 | wait |

Scoring costs 0.10 ms per decision and the model loads in 22 ms at call setup.

One weakness was found and fixed on the way. A character model reads the end of
a word, and a name ending on the vowel sign "ి" (రవి, హరి) ends exactly like a
non-finite verb ("చేసి" -- "having done", the sentence continues). "నా పేరు రవి."
scored 0.85 against 0.91 for "నా పేరు సురేష్." -- a coin-flip on how a caller's
name happens to end. A feature for "the agent asked for a name and the caller
gave one" moved ten common names to 0.85-0.93 with no change to the replay, and a
test now pins it.

Stated plainly, so nobody over-reads it:
- the setting was chosen from 32 on these same calls. The models are
  out-of-sample; the choice is not. Neighbouring settings sit at 6.6-7.5% at the
  same 0.46 s, so the surface is smooth and the optimism is small (the final
  grid was 26 settings on the full data; the first, on partial data, 32);
- "cut off" means the caller's voice returned within 1.0 s of the turn ending,
  on recordings where the LIVE agent was answering at 0.9 s. A caller who went
  quiet because the agent spoke is counted as finished. Every row carries that
  bias equally; only a live call removes it;
- the oracle row says the architecture is right — deciding when the words land
  can get interruptions to almost zero at 0.46 s — and the whole remaining gap is
  the classifier.

### What a caller would feel

Server-side, today's turn is ~0.90 endpoint + ~0.35-0.46 LLM + ~0.10 TTS ≈ 1.4-1.5 s.
With TurnSense a confident turn is ~0.46 + 0.35-0.46 + 0.10 ≈ **0.95 s**; a turn the
words read as unfinished waits longer, by design. That is
still ~0.2 s above the 700-800 ms target; section 7 says where the rest comes from.

## 5. Consistency: MB Solar's answer bank

The prompt the gate pins as published (`.tmp/wf2_agent_prompt_v4.md`) already had
most of doc 41's orphaned answers as rows (35). Two defects remained, both
measured over 971 real MB Solar caller lines from the Soniox replay:

- **The roof row fired on every roof answer.** Its trigger was any mention of a
  roof, and the agent asks every caller about their roof. It fired on 10 real
  lines, all plain answers ("సొంత రూఫ్ ఉంది."), and on no roof question. So on
  most calls the model was handed the "small or shaded roof" FAQ exactly when it
  should have moved on. The "what else do you do" row fired on a bare "కెమెరా",
  and the family row on any "ఆయన" ("he"). All three now require a question form.
- **Four facts were still unreachable:** what the platform is, the four-step
  process, "most vendors reply within 2 to 4 hours", and the scheme's approval
  date. Each is now a row in the client's own words.

`tools/turnsense/verify_wf2_reference_v5.py` builds v5 (versioned at `clients/reference/wf2_mbsolar_prompt_v5.md`) and checks: head
byte-identical, every TRIGGER compiles, every row under 700 chars, no role
labels, no MODE lines, no answer loses its tail at a "?", 17/17 test questions
fire their row, 21/21 real plain answers fire nothing, no new row fires on more
than 0.4% of real lines. **SAFE**; written to `.tmp/wf2_agent_prompt_v5.md`,
not published. The four BS Wealth banks were checked the same way and fire only
on real questions.

## 6. The LLM leg

Real 27,248-character MB Solar prompt, the first turns of run 1021, eight warm
draws per model, interleaved, from a laptop in India:

| model | first spoken word p50 | p90 |
|---|---|---|
| sarvam-105b-conversations (hosted in India) | 1.04 s | 1.35 s |
| sarvam-105b | 1.14 s | 1.44 s |
| **groq gpt-oss-120b, reasoning low (LIVE)** | 1.31 s | 2.19 s |
| groq qwen3.6-27b, no thinking | 1.50 s | 1.58 s |
| groq gpt-oss-20b, reasoning low | 1.56 s | 1.63 s |
| groq qwen3.8-27b, no thinking | 1.58 s | 1.84 s |

These cannot be compared with the server's 0.33-0.46 s for the same model: from
this laptop Groq's floor on a one-word prompt is 0.12-0.15 s, so the gap is not
the network, and the server also hedges two requests. What the table does say:
Sarvam's floor is ~0.83 s even for a one-word prompt, so an India-hosted model
is **not** automatically faster; the qwen models answer the subsidy with numbers
in English, gpt-oss-20b is not faster than 120b. **No switch is recommended until
the same bench runs on the Mumbai server.** OpenAI could not be tested (credits).

## 7. The plan, ranked by measured payoff

### When the server is back — one change per call, gate green before each

| # | step | why | revert |
|---|---|---|---|
| 0 | Vishnu: hPanel -> VPS -> status / restart / billing | nothing else is possible until it answers | — |
| 1 | `python tools/pre_call_gate.py --skip-tests`, then `python tools/vaani_config.py --workflow 2` | record what is ACTUALLY live; settles the run-997 question in section 3 | read-only |
| 2 | merge branch `turnsense` into `vaani/main`, `coolify.py deploy`, check `/health` build | ships the code; the model is off by default, so no call changes | redeploy the previous build |
| 3 | MB Solar only: `turn_stop_strategy=turn_analyzer`, `turn_model=turnsense`, `telugu_turn_assist=false` (the turnsense_* defaults ARE the measured point); publish; verify the PUBLISHED snapshot | the measured win: 9.7% -> 5.7% interruptions, 0.88 -> 0.46 s | `turn_stop_strategy=transcription` (+ assist back if wanted), publish |
| 4 | ONE test call, `call_grade.py`, server log lines `[turnsense] end:` | the only thing that removes the replay's bias | as 3 |
| 5 | publish MB Solar prompt v5 (`clients/reference/wf2_mbsolar_prompt_v5.md`) with `patch_node_prompt.py`, then `publish_workflow.py`; separate call | roof row fired on every roof answer; 4 unreachable facts | republish v4 |
| 6 | after 2-3 good MB Solar calls, the same config on wf3/4/5/6 | the model is Telugu, not solar-specific | per workflow |
| 7 | drop `speculation_enabled` from the gate's EXPECTED_CONFIG | dead config claiming a behaviour the agent does not have | — |
| 8 | run `tools/turnsense/llm_ttft_bench.py` ON the server | laptop numbers cannot rank the LLMs | read-only |

### The next builds, in order of payoff

1. **Speculation, rebuilt properly** (~0.25 s, the step from ~0.95 s to ~0.70 s).
   Soniox streams non-final tokens during speech, so at the Silero stop (0.2 s
   after the last word) the text is mostly already known. Start the LLM there on
   `compile_vaani_system_prompt` + `state.render()` + MODE_PROTOCOL — the exact
   assembly a normal turn uses, which is what the `NotImplementedError` demands —
   and keep the reply BUFFERED until TurnSense ends the turn and the final text
   matches. Thinking early, speaking on time: it can waste tokens, never
   interrupt.
2. **TurnSense v2**: add the audio-native encoder's probability as a feature. The
   misses left are "complete-sounding but he carried on" ("10 లక్షలు." then more),
   which only the voice can hear. Retrain weekly as calls accumulate — in-domain
   data moved the fold AUC from ~0.60 to 0.72-0.83 tonight.
3. **The LLM**, decided on server-side numbers and the conversation eval, not on
   a laptop. OpenAI needs credits to be a candidate at all.

## 8. The seven techniques

| technique | used? | where / why not |
|---|---|---|
| 1 retrieval over prompt-stuffing | **yes** | answer-bank rows; 3 trigger precision fixes + 4 rows (section 5) |
| 2 semantic embeddings | skipped | regex triggers measure 0-0.4% false fires on 971 real lines; embeddings add a dependency for no measured gain yet — revisit for recall on paraphrases |
| 3 model routing / cascading | skipped | needs server-side LLM numbers; no faster model is proven from India yet |
| 4 classical ML | **yes** | TurnSense: logistic regression on 2,600+ real labelled pauses, grouped CV |
| 5 bandits | skipped | no outcome signal flows while the server is down, and turn-taking has no variants to split traffic over yet |
| 6 eval harness / regression | **yes** | Silero + clock-corrected replay, 5-fold CV, oracle ceiling, 13 unit tests, gate list updated |
| 7 observability & versioning | **yes** | artifact carries version + metrics; one `[turnsense] end:` log line per turn; v5 prompt versioned; this doc |

## Files

Built today, in `dograh-vapi` (branch `turnsense`): `api/services/vaani/turnsense.py`,
`turnsense_turn.py`, `models/turnsense_te.json`, `api/tests/test_turnsense_turn.py`,
and small config-gated edits to `turn_taking.py`, `workflow_configurations.py`,
`brain_processor.py`, `run_pipeline.py`.

In `bswealthfinance/tools/turnsense/`: `replay_eval.py`, `soniox_rt_segments.py`,
`build_pause_dataset.py`, `train.py`, `cv_replay.py`, `teacher_label.py`,
`llm_ttft_bench.py`, `verify_wf2_reference_v5.py`.

## Appendix — the ship commands, exactly

Both publishing tools default to the OLD box (`--server voice`); every command
below names Vaani explicitly. One change per call; gate green before each.

```
# 1. what is live right now (read-only)
python tools/pre_call_gate.py --skip-tests
python tools/vaani_config.py --workflow 2

# 2. ship the code (model is off by default: no call changes)
cd ../dograh-vapi && git checkout main && git merge --ff-only turnsense && git push vaani main
cd ../bswealthfinance && python tools/coolify.py deploy <vaani-app-uuid>      # kept out of this public repo
#    then confirm /health reports the new build before anything else

# 3. TurnSense on MB Solar only, then publish and verify the PUBLISHED config
python tools/vaani_config.py --workflow 2 --set turn_stop_strategy=turn_analyzer --set turn_model=turnsense --set telugu_turn_assist=false
python tools/publish_workflow.py --workflow 2 --server vaani
#    revert: --set turn_stop_strategy=transcription --set telugu_turn_assist=true, publish

# 4. one test call, then grade it and read the decisions
python tools/call_grade.py <run_id> --workflow 2   # and the server's "[turnsense] end:" lines

# 5. MB Solar prompt v5, on its own call
python tools/patch_node_prompt.py --workflow 2 --node agent --file clients/reference/wf2_mbsolar_prompt_v5.md --server vaani --dry-run
python tools/patch_node_prompt.py --workflow 2 --node agent --file clients/reference/wf2_mbsolar_prompt_v5.md --server vaani
python tools/publish_workflow.py --workflow 2 --server vaani
#    the gate pins PROMPT_FILE to v4 -- point it at v5 in the same change
```
