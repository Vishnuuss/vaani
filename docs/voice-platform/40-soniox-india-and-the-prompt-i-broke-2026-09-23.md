# 40 — Soniox on the India region, and the prompt I broke chasing 800 ms

*2026-09-23. Ten calls, runs 1009–1021, builds `32fe4f7d` → `f62e4f3c`.*

## The one clean result

A Soniox key scoped to the **India region** arrived. Measured on live calls, STT
finalisation is now:

| | value |
|---|---|
| Sarvam saarika:v2.5 (run 972) | 0.392 s |
| Soniox ears-only, GLOBAL host (run 989, 19 Sep) | 0.686 s |
| **Soniox ears-only, INDIA host (runs 1013–1021)** | **0.23–0.28 s** |

Across 40-odd turns on five calls it never left that band. It is the fastest and
by far the steadiest STT this project has recorded, and it is the only change
today that is unambiguously good.

The cause was never the model. **This server is in Mumbai** (200.141.7.188,
Hostinger IN) and `stt-rt.soniox.com` is Soniox's global host, so every
finalisation crossed an ocean. `stt-rt.in.soniox.com` does not.

Region keys are **scoped**: the India key returns 401 on the global host and the
old global key returns 401 on the India host. The URL and the key must move
together, or STT dies — and a dead STT on this stack looks exactly like the
turn-detection bugs this project spends its life chasing. That is why the URL is
now `SONIOX_STT_URL` with a loud default rather than a silent constant
(`service_factory.py`, commit `f62e4f3c`).

When the day started the org config already held the India key while the code
still dialled the global host. Run 1009 is what that costs: 26 seconds, one bot
turn, no user transcript, empty `llm ttfb`.

## What was NOT measured, despite a claim to the contrary

An earlier note in this session said the India region measured 2.7x faster than
global from an Indian laptop. **That number should not be quoted.** Two runs of
`tools/probe_soniox_region.py` disagreed in opposite directions — 1.410 s vs
3.782 s for India, then 2.011 s vs 1.192 s against it — with 5-second websocket
handshakes in both. The home link was the dominant variable. The region's value
is established by the live calls above, not by that probe.

## The turn-config defect: set, believed, ignored

MB Solar had `semantic_turn_completion: True` stored on it and
`turn_stop_strategy: "transcription"`. The key is read **only** inside the
`turn_analyzer` branch, so the LLM had never gated a turn end. Nor had
`turn_model`, nor any `endpoint_*` value. What actually ran was the 0.7 s timer
beside `telugu_turn_assist` — two eager detectors, the faster one wins, and
nothing in that arrangement can hold the floor for a caller.

Run 1012 is the consequence. The caller answered "మాది.", drew breath, lost the
turn after two words, and the reply he triggered was cancelled by his own next
words three times over. He heard nothing and hung up saying "హలో. హలో అండి".

The file already carried the scar: *Someone set MB Solar's `endpoint_min_secs`
to 0.3 and it changed nothing.* This is the third time. Commit `ab31f9e0` makes
it loud — any analyzer-only key set on a transcription agent is now named in a
warning, along with the setting that would bring it to life. Four tests cover
it, one of which exists because the first draft asserted against pytest's
`caplog` while this codebase logs through loguru, and so passed against an empty
string.

## The mistake that cost four calls

After run 1013 I changed **two things at once**: the turn strategy and the agent
prompt (v36, a Telugu-script fix for the company name). Runs 1014, 1015 and 1016
then produced a greeting and nothing else — LLM firing every turn, TTS firing
only for the greeting.

I diagnosed that as a turn-taking deadlock three times running and changed three
more settings. It was the prompt. v35 and v36 bracket it exactly:

| run | prompt | outcome |
|---|---|---|
| 1012, 1013 | v35 | speaks |
| 1014, 1015, 1016 | **v36** | greeting only, replies discarded |
| 1018, 1020, 1021 | v37 (= v35 restored) | speaks |

The signature was visible from the first failure and I read past it: **the LLM
ran and only the speaking stopped.** That is a reply being discarded downstream,
which is a generation or sanitiser problem, never a turn that will not end. A
turn that will not end produces no LLM call at all.

Two lessons, both already written elsewhere in this repo and both ignored today:
change one variable per call, and when something breaks, suspect what you just
changed before you suspect the subsystem you were already thinking about.

Because of this, run 1015 — `turn_analyzer` with the LLM gate OFF — was
condemned on contaminated evidence and had to be re-run as 1020.

## The LLM turn gate does not work on gpt-oss-120b

`semantic_turn_completion` requires the model to emit a `✓` marker as its first
token, while this agent's own MODE protocol requires the reply to begin with
MODE. Two competing "must begin with" rules on an open-weights model. On
non-compliance there is no marker, the mixin re-prompts internally, and *no
completion frame is ever emitted* — the turn stays open indefinitely.

Run 1013 shows both halves: three turns finalised in **0.305 s**, the fastest
endpoints ever recorded here, and two turns hung for **14.8 s and 31.5 s** with
three cancelled LLM attempts each. The `user_turn_stop_timeout` watchdog cannot
rescue it, because it fires only `if self._user_turn and not self._user_speaking`,
and lowering it to 2.5 s changed nothing (run 1014).

**Verdict: off.** It is the only configuration that has produced sub-0.4 s
endpoints on this stack, and it is unusable until the marker protocol is
reliable.

## Latency: the target was not met, and the floor is not yet explained

| run | config | p50 total | endpoint p50 | under 0.6 s | fields |
|---|---|---|---|---|---|
| 1018 | timer 0.7 s, no assist | 1.538 s | 0.950 s | 0/4 | 2/6 (short call) |
| **1020** | **turn_analyzer, no gate** | **1.589 s** | **0.968 s** | 0/10 | **6/6** |
| 1021 | as 1020, unsure floor 0.45 | 1.482 s | 0.962 s | 0/15 | 6/6 |

The endpoint sits at **0.93–1.04 s on every turn of every configuration**, and
that is the finding. It did not move when the turn strategy changed, and it did
not move when `endpoint_unsure_floor_secs` was halved from 0.9 to 0.45 — a
setting that had just been brought to life and should have been binding. Silero
is at `stop_secs=0.2` and STT is 0.24 s, which accounts for less than half of it.

**The remaining ~0.5 s is unexplained.** It is the entire gap between today's
1.55 s and the 800 ms in `latency_budget.yaml`, whose endpoint allocation is
250 ms. Nothing else is close: LLM 0.46 s, TTS 0.10 s, STT 0.24 s.

That is the next piece of work, and it needs server logs rather than more live
calls — specifically whether `TeluguTurnAnalyzer` reports `enabled` on the
server as it does locally, since a fallback to Smart Turn v3 (no Telugu, mostly
times out) would produce exactly this dead-constant endpoint. The weights are
git-tracked and ship in the image, so if it is disabled the reason is elsewhere.

Lowering the floor also made quality worse: run 1021 fragmented answers
("సంవత్సరాలు, మరి. / సొంత ఇల్లు.") and re-asked the property type three times.
Reverted to 0.9.

## Final live configuration

Settled on run 1020's, which is the best measured: 10 turns, 109 s, ended on
`end_call` rather than a hangup, and **all six fields captured** — commercial,
Rs 50,000, Vijayawada, roof, విష్ణు, survey booked.

    turn_stop_strategy            turn_analyzer
    semantic_turn_completion      False        # deadlocks on gpt-oss-120b
    telugu_turn_assist            False        # eager racer, cannot reduce cutoffs
    endpoint_min_secs             0.3
    endpoint_max_secs             2.2          # hard ceiling; no 30 s hang possible
    endpoint_unsure_floor_secs    0.9          # 0.45 measured worse
    user_turn_stop_timeout        2.5
    STT  soniox / stt-rt-v5 / te-IN, INDIA region key

## Known defects still open

1. **Re-asks.** Run 1021 asked the property type three times and re-asked
   location after "మాది హైదరాబాద్". Documented previously as model drift.
2. **English leaking into Telugu speech** — "after-two-pm",
   "morning-ten-o'clock", "today after two pm" (run 1020). Needs a prompt fix,
   which is exactly what broke the agent today, so it wants a careful one and a
   call of its own.
3. **The 0.95 s endpoint floor**, above.

## Operational notes earned today

- `tools/place_test_call.py` silently falls back to `NEXT_PUBLIC_DOGRAH_API_URL`
  when `DOGRAH_BASE_URL` is unset — which is `voice.bswealthfinance.com`, the
  OLD box, whose Vobiz credentials have been dead since August. It found
  telephony config 1 there and queued a campaign that could never dial. Vaani
  has only config 5. Always pass `DOGRAH_BASE_URL=https://vaani-api...`.
- A definition PUT via `patch_node_prompt.py` does **not** wipe
  `workflow_configurations` — verified, 31 keys before and after.
- Vobiz returns `busy` and `no-answer` as ordinary outcomes (runs 1010, 1017,
  1019). A call that never connects looks like a failure in the run list and is
  not one.
