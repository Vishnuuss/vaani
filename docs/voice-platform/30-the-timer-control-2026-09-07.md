# The control nobody had run

**7 September 2026 · Project Vaani · turn detection**

Every turn-detector comparison on this project has been model against model.
None was against a stopwatch. This is that measurement, and it reverses a
decision that was about to ship.

---

## The question

The retrained linear model has been waiting on a deploy decision since the
morning of 7 September. It was chosen because, scored as a classifier in
isolation, it beat everything: 1.17% false cutoffs against the deployed
forest's 6.45%. A 5x improvement on the cardinal failure.

The question never asked was: **compared to not having a model at all?**

## The method

`tools/sweep_timer_control.py`. Same 60 recorded calls, same 235 speech bursts,
same harness (`tools/replay_turns.py`) as every other number in this programme.
No live calls, nothing billed.

The control is one line: set `threshold = 1.1`. The model's probability can
never exceed 1.0, so no verdict ever fires, and the endpoint floors decide the
turn alone. That is a stopwatch. Sweep the wait from 0.4 s to 2.0 s and you have
the curve every model should have been scored against from the start.

## The result

| case | cut off | wait p50 | wait p90 |
|---|---|---|---|
| DUMB TIMER 0.4s | 27.7% | 0.60s | 0.62s |
| DUMB TIMER 0.6s | 19.1% | 0.80s | 0.82s |
| DUMB TIMER 0.8s | 16.6% | 1.00s | 1.02s |
| DUMB TIMER 1.0s | 15.7% | 1.20s | 1.22s |
| DUMB TIMER 1.3s | 13.6% | 1.52s | 1.54s |
| DUMB TIMER 1.6s | 9.4% | 1.84s | 1.84s |
| DUMB TIMER 2.0s | 5.1% | 2.24s | 2.26s |
| **MODEL shipped (live today)** | 23.0% | 0.48s | 0.98s |
| **MODEL retrained linear** | 19.1% | 0.98s | 1.04s |
| **MODEL retrained gbm** | 21.3% | 0.63s | 1.16s |
| **MODEL smart-turn-v3.2** | 6.4% | 2.18s | 2.18s |

Scoring each model against the timer curve interpolated at that model's own
median wait — *what would a stopwatch have cost me for the same patience?*

| model | cut off | at wait | stopwatch | verdict |
|---|---|---|---|---|
| shipped (live) | 23.0% | 0.48s | 27.7% | better by 4.7 pts |
| retrained gbm | 21.3% | 0.63s | 26.4% | **better by 5.1 pts** |
| retrained linear | 19.1% | 0.98s | 16.9% | **WORSE by 2.2 pts** |
| smart-turn-v3.2 | 6.4% | 2.18s | 5.7% | WORSE by 0.7 pts |

---

## 1. The linear model must not ship

It is not an improvement. It is worse than owning no model: a flat 0.6 s timer
cuts off the same 19.1% of turns and answers **0.18 s sooner**.

The isolated-classifier measurement was not wrong, it was answering a different
question. Document 29 §8 said why in advance and the warning was not heeded
closely enough:

> `_wait_secs` interpolates on `frac = p / bar`, so changing the model changes
> the wait curve as well as the verdict.

A model that is more *certain* is also more *patient*, because certainty feeds
the wait. So the linear model bought its lower cut-off rate with 0.5 s of extra
waiting — and a stopwatch will sell you that same patience cheaper.

**When the isolated metric and the end-to-end metric disagree, the end-to-end
one is the one callers experience.**

## 2. Smart Turn v3.2 does not transfer to Telugu

Pipecat bundles `smart-turn-v3.2-cpu.onnx`: a Whisper-mel encoder to a single
logit, 8M parameters, 12 ms on CPU, 23 languages — **not** including Telugu.

Its 6.4% reads as the best number in the table until you notice
**`p50 == p90 == 2.18s`**. A model that fires early would show a spread. This
one has none: it almost never says COMPLETE, so it *is* a 2.2 s stopwatch. And
a real 2.2 s stopwatch scores 5.1%, which is better.

`turn_taking.py` already carried the comment "will mostly time out". This is the
measurement behind it. Adopting it as-is is off the table; **fine-tuning it on
the 2,950 real Telugu labels is now the open question**, and it is a different
and much larger piece of work.

## 3. Prosody is worth about five points, total

The best model in the table beats a stopwatch by 5.1 percentage points.

Sixteen hand-built acoustic features, a 250-tree forest, a one-class training
set found and corrected, two retrainings, a bake-off against a vendor model, and
a twelve-point endpoint sweep. Five points of cut-off rate.

That is the ceiling document 29 predicted, now measured against the right
baseline for the first time. **The semantic channel is not an optimisation of
this work. It is the replacement for it.**

---

## What this does not say

**It does not say ship a stopwatch.** The 2.0 s timer scores 5.1% because it
waits 2.24 s on *every* turn, including the ones where the caller obviously
finished. That is a different and worse call to be on — the caller who is done
talking sits in silence for two seconds. The models earn their keep exactly
where a timer cannot, at a 0.48 s median. The finding is that they earn far less
of it than anyone had measured.

**It does not invalidate the retraining.** Finding the one-class training set
was correct and the corrected labels are real. The retrained *forest* is the
best model on this table. What failed was the promotion decision between two
retrained candidates, made on a metric that does not survive the wait curve.

## What changed in the code

`turn_model` is now a workflow configuration: `forest` (default, unchanged),
`linear`, or `timer`. Until today `TeluguTurnAnalyzer.__init__` did
`_load_forest(...) or _load(...)` — a forest on disk always won, so shipping a
different model meant deleting a file, which is not revertible from config on a
live client agent.

`timer` loads nothing on purpose. "No verdict" had to be expressible before it
could be tested.

**Nothing is deployed. The default is still what production runs today.**

## Next

1. Do not ship the linear model. Decision reversed on the evidence above.
2. Retrained forest is a modest, real gain — 21.3% vs 23.0%, +0.15 s wait.
   Worth shipping only if that trade is wanted; it is 1.7 points.
3. The real work is the semantic channel, and the four-state turn ontology
   (complete / incomplete / backchannel / wait) that Parts 6 and 7 of the brief
   are about.
