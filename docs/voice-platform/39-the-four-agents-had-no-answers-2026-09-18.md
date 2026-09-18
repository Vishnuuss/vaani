# 39 — The four agents had no answers, and the call could not hang up

*2026-09-18. Runs 981, 982, 983, 984, 985 — one call on each BS Wealth agent,
build `40006cb00546`.*

The client's report, in his words: the four agents were "very inconsistent",
"didn't answer some of the questions", "some didn't cut the call", and they
"sound more like scripted — question answer question answer". He gave the
decisive example himself: asked *will you take a loan?*, he replied with a
question of his own — *what types of loan do you have?* — and the agent, having
been given an answer it did not expect, lost the thread.

Three separate defects. Two are now fixed; one waits on facts only he has.

## What the calls measured

| run | agent | turns | TOTAL p50 | endpoint p50 | LLM | TTS |
|---|---|---|---|---|---|---|
| 983 | solar | 7 | 1.197s | 0.698s (0/7 under 0.6) | 0.397 | 0.108 |
| 982 | investment | 10 | 1.228s | 0.700s (3/10) | 0.383 | 0.104 |
| 981 | property | 12 | 1.206s | 0.678s (4/12) | 0.497 | 0.108 |
| 985 | loan | 3 | 1.596s | 0.691s | 0.429 | 0.095 |

All four `workflow_configurations` are byte-identical, so the inconsistency the
client heard is not configuration. It is content.

## Defect 1 — they had nothing to answer with

`client_reference.py` gives every agent an on-demand FAQ: rows under a
`## Reference` heading, each with a `TRIGGER:` regex, injected only on the turn
a caller reaches for one. MB Solar has eighteen such rows.

The four BS Wealth agents had **none**. Not a thin set — zero.

| wf | agent | prompt | `## Reference` | TRIGGER rows |
|---|---|---|---|---|
| 3 | loan | 4,302 chars | no | 0 |
| 4 | solar | 1,890 | no | 0 |
| 5 | investment | 1,959 | no | 0 |
| 6 | property | 2,546 | no | 0 |

Layer 2 has said *"Answer, then ask — never defer"* all along, and it is
compiled into every one of these calls. The instruction was never missing. The
**material** was. wf4's prompt goes further and instructs the deflection in so
many words: *"Price, subsidy, savings, payback — you have NO numbers."*

So run 983 could only ever go the way it went:

    USER  ...అంటే అంతా సబ్సిడీ వస్తదిగా, మొత్తం.      is it ALL subsidy?
    BOT   మా executive ఈ వారంలో రెండు సమయాల్లో...      [a booking]
    USER  ఆగు, ఆగు, నేను ఏం అడిగా?                    stop, stop — what did I ASK?

and run 981 the same way, cut off mid-definition:

    USER  రెసిడెన్షియల్ ప్లాట్ అంటే ఏంటి?              what IS a residential plot?
    BOT   Residential plot అంటే                        [never finished]

The counter-example proves it is content and not protocol. Run 985 — the
client's own loan example — was answered **correctly**:

    USER  మీ దగ్గర ఏమేం లోన్స్ ఉన్నాయి?
    BOT   Personal loan, business loan, education loan అండి — మీకు ఏది కావాలి?

because the loan prompt happens to list the three types in its prose. Where the
question is in the text, the agent answers it. Where it is not, it deflects.
There was never a "gets confused by a counter-question" bug to fix.

## Defect 2 and 3 — one root cause, two symptoms

Run 982 would not hang up. The business finished at about 90 seconds — the
advisor call agreed, "సరే", "బాయ్" — and the call ran to **171**:

    USER  అలాగేనండి. బాయ్.
    USER  హలో? హలో, కాల్‌లో ఉన్నారా ఇంకా?
    USER  హలో?
    USER  హలో, అక్కా, కాల్‌లో ఉన్నావా?
    BOT   వీడ్కోలు సార్.                               [30+ seconds later]

`_end_is_earned` refuses `MODE: END` while a question is on the line. That is
run 882's fix and it is right — hanging up on an engaged caller is the most
expensive mistake this agent makes. But *"కాల్‌లో ఉన్నారా ఇంకా?"* — **are you
still on the call?** — carries the interrogative clitic, so it read as a
question, and it is not one. It is a **presence check**: the noise a caller
makes because nobody has spoken. It asks for nothing.

The direction of the failure is what made it expensive. Every "are you there?"
was itself proof the agent had stopped talking, and every one renewed the
refusal to hang up. The longer the dead air ran, the more certainly it could not
end. He had to put the phone down himself.

The same misreading cost a turn earlier in the same call, through
`render()`'s `he_asked`:

    BOT   ...మీకు పిల్లలు ఉన్నారా అండి
    USER  నాకు పిల్లలు లేరు. హలో? నేను ఇంకా కాల్‌లో ఉన్నారా?
    BOT   అవును సార్, ఇంకా కాల్‌లోనే ఉన్నాం అండి. [then re-asks]
    USER  నేను చెప్పింది మీకు వినిపించిందా? మాకు పిల్లలు లేరు.
          "did you HEAR what I said?"

`note_user_said` accumulates a turn, so `last_user_text` was the answer *and*
the "hello" together. The "hello" made `he_asked` true, the `elif he_asked`
branch withdrew the checklist, and the agent spent its turn confirming it was
still connected while the answer it had just been given went unheard.

That is the "it is not listening / it sounds scripted" complaint, and it is one
predicate, in two places.

### The fix

`_is_presence_check()` in `state.py`: true when the text carries a presence
phrase and nothing surviving its removal is a question. Wired into
`_end_is_earned` and into `he_asked`. It only ever removes a *block* — it is
never itself a reason to end a call.

Two details cost a round each and are worth recording. The STT writes
`కాల్‌లో` with a **ZWNJ** holding the halves together, which is not whitespace,
so a `\s*` matched neither spelling. And the leftovers `ఇంకా` (*still*) and
`అక్కా` (*sister*) both end on the AA sign, which is `_QUESTION_PARTICLE`'s
entire rule — so the residue read as a question on the strength of one vowel
sign, and the check failed on exactly the real sentences it was written for.

65 tests, all passing. 765 pass across the conversation suite; the 2 failures in
`test_endpoint_hold.py` pre-date this work and were confirmed by stash.

## What was written, and what is still owed

Answer banks now exist for all four, built from the utterances that actually
failed and verified against them offline by `tools/verify_reference.py`:

| wf | live rows | fires on | per-turn prompt |
|---|---|---|---|
| 3 loan | 5 | 1 of 5 turns | 4,302 → 4,301 chars |
| 4 solar | 9 | 4 of 8 | 1,890 → 1,889 |
| 5 investment | 5 | 3 of 10 | 1,959 → 1,958 |
| 6 property | 6 | 2 of 12 | 2,546 → 2,545 |

The per-turn column is the point. Everything added sits **after** `## Reference`
and is therefore never compiled into the system prompt; only the matched row
reaches the model, at most two, capped at 700 characters. The client asked for
answers without a bigger bill, and this is the mechanism that gives both.

Nothing fires on a plain answer — not on `హైదరాబాద్`, not on `20 లక్షలు`, not
on `సరే` or `బై`.

**What is still owed is every number.** Subsidy amounts, interest rates,
eligibility, returns, minimum premium, brokerage, approval status. Those rows
are written with no `TRIGGER:` line, which `parse()` never selects, so the agent
cannot say them — and until they are filled the Layer 2 catch-all answers
honestly: *"I don't know, our team will confirm."* Guessing them was never an
option: these are a regulated financial-services firm's real customers.

## On the 800ms target

The client asked for sub-second. The measured budget is 1.20s median:
endpoint 0.70 + LLM 0.42 + TTS 0.11. **No amount of prompt work touches the
0.70.** It is `smart_turn_stop_secs` 0.2 plus `dograh_speech_timeout_secs` 0.45,
and lowering the timer is what brought the double replies back on 13 September.

The only measured way down is Soniox, at 0.44s — and doc 38, written this
morning, rejected it on live evidence for cutting callers off mid-sentence.
`87242fec` is the written-but-undeployed answer to that, and it has never been
measured on a call. Until it is, 800ms is not a number anyone here can promise,
and the honest statement is 1.2s today.

Quality first, then latency, one change at a time — which is the client's own
standing rule, and the reason these two were not shipped together.
