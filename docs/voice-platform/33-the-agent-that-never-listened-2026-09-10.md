# 33 — The agent that never listened

**10 September 2026**

---

## The question that found it

Vishnu asked a simple thing: *is the qualification flow a strict script that
just asks the questions, or does the model generate them?* He added that he
wanted to be certain the agent would not loop the same question and ignore what
the caller said.

Answering the first half honestly required reading the live path rather than
the design docs. That read found the second half was already happening.

---

## The architecture, as it actually is

It is **not** a script. There is no node graph and no fixed sequence. From
`vaani/gateway/pipeline.py`:

> *"Four layers → one flat system prompt. This is the whole 'flow'. There is no
> second prompt, no per-turn template, no node."*

Four layers are compiled **once at boot** into a single system prompt:

| Layer | Content | Varies by |
|---|---|---|
| 1 | Persona & voice | language — constant per language |
| 2 | Sales psychology | nothing — the moat |
| 3 | Business, questions, objections | client (`clients/*.yaml`) |
| 4 | Mission | agent type |

Layers 1 and 2 are byte-identical across every call, so they sit first and land
in the provider's cached prefix.

Then on **every turn**:

```
caller speaks
  -> STT
  -> StateInjector: synchronous regex triage (hard stops), then rewrite the
     context so an ~80-token STATE BLOCK is the LAST system message
  -> LLM generates the reply
  -> ReplyFilter: strip the MODE line, check guardrails
  -> TTS
```

The state block is the steering wheel, and it is placed last **on purpose** —
`state.py` says so plainly: *"This block is the LAST thing the model sees, which
makes it the most authoritative thing in the context — more so than 3,000 tokens
of prose further up."*

That design choice is what made the defect so damaging.

---

## The defect

`state.known` — the record of what the caller has already answered — is written
by exactly one function: `extractor.apply_to_state` → `state.learn`.

`vaani/brain/extractor.py` was imported by **`vaani/sim/simulate.py` only**.
`StateInjector`, the processor that runs on an actual phone call, never called
it.

So on every real call:

1. `KNOWN` stayed `{}` for the whole call.
2. `still_need` therefore returned every field on every turn.
3. `render()` re-pinned `NEXT QUESTION TO ASK: "<question 1>"` as the freshest
   and loudest instruction in the context — however many times it had been
   answered.
4. `advance()` could never leave `QUALIFYING`, because that transition requires
   `still_need` to be empty. PITCHING and CLOSING were unreachable.

Only the hard-stop regexes — removal, fraud, a second refusal, a buying signal,
an agreed time — could break out, because those latch synchronously in
`triage.apply`.

### The warning was already in the file

`extractor.py`'s own docstring:

> *"without this, `KNOWN` stays empty forever. `STILL_NEED` then lists fields
> the caller already answered, and the agent eventually re-asks them. Re-asking
> is one of the fastest ways to lose trust on an outbound call — it says nobody
> was listening."*

It was written, understood, wired into the simulator, and never wired into
production.

### Why every gate passed anyway

The simulator calls `extractor.extract` itself, at `simulate.py:126`. The sim
therefore measured a system that listened, while the phone ran a system that did
not. **A gate that builds its own wiring cannot detect missing wiring.**

---

## The fix

Two changes, plus a guard against the bug class.

**1. `brain_processor.StateInjector`** takes an `llm=`, and after triage spawns
the extractor fire-and-forget, handing it the agent's own previous line (because
"own" only means home ownership next to the question that prompted it). The
result lands in the state block for the next turn — the one-turn lag the
extractor was always designed around. It costs the current turn nothing.

**2. `pipeline.py`** opens one `LLMClient` in a FastAPI lifespan at boot and
passes it in. At boot, not per call: `LLMClient.__aenter__` pre-warms with a
models GET, and paying that handshake while the phone is ringing would land on
the greeting. If no provider is configured the server logs a loud error and
boots anyway — degrading to the old behaviour beats not starting.

**3. A guard test** parses `pipeline.py` and fails if a `StateInjector` is ever
constructed without `llm=`. The defect was never in the brain; it was in the
wiring, and a unit test of the processor cannot see that.

---

## Evidence

Test-first. All 8 tests were written and watched fail before any production
line changed. Then, to check the tests were honest rather than merely red, the
wiring alone was disabled (`if False:`) while keeping the new signature: the 4
behavioural tests failed and the 3 safety tests stayed green, which is exactly
right.

- Full suite: **33 passed** (25 pre-existing + 8 new), no regressions.
- Boots with no API keys: logs the error, `EXTRACT_LLM=None`, `/health` 200.
- Boots with keys: `extraction model ready`, warm providers `[sarvam, groq]`.

Against the **real** model, one real Telugu sentence —
*"మా సొంత ఇల్లు అండి, బిల్లు నెలకి మూడు వేలు వస్తుంది"*
("it's our own house, the bill comes to three thousand a month"):

| | State block after that one turn |
|---|---|
| **Before** | `KNOWN: {}` · `STILL_NEED: [home_ownership, monthly_bill, timeline]` · `NEXT QUESTION TO ASK: "Do you own or rent your home?"` |
| **After** | `KNOWN: {home_ownership: owned, monthly_bill: 3000}` · `STILL_NEED: [timeline]` · `NEXT QUESTION TO ASK: "When are you looking to install?"` |

The caller answered two questions in one breath. The old agent was about to ask
the first one again.

---

## What this does not cover

- **Nothing is deployed.** This is verified on the development machine only. It
  is not yet confirmed that the server runs this code.
- **No phone call has been placed** against the fix. The turn-level proof above
  is real but it is not a call.
- **The extraction model is the default (Sarvam 105B).** The extractor argues
  for a small model — *"not paying 120B prices for small jobs"* — so per-turn
  extraction cost is an open follow-up, not a settled choice.
