"""Bound what a conversational turn is allowed to be.

The failure
-----------
Run 12 (`WR-TEL-OUT-07584411`, 2026-08-27) spoke this to the caller as ONE turn:

    మీ ఇల్లు, అపార్ట్‌మెంట్ లేదా కమర్షియల్ ఏది?      <- question 3
    మీకు మీ స్వంత రూఫ్ స్పేస్ ఉంది కదా?             <- question 4
    My name is Rani.                                 <- the CALLER's line, invented
    సరే రాణి అండి, మా వేరిఫైడ్ వెండర్ ...            <- question 5
    We have all info, need agreement.                <- its own internal note
    MODE: CLOSE                                      <- the control token, ALOUD
    అవును, రాణి అండి, మీకు ఈ వారంలో ఏ రోజు ...      <- replying to itself

272 characters. The model wrote a four-turn script and the TTS read all of it,
control token included.

Why it could happen at all
--------------------------
Nothing bounded the generation. `max_tokens` and `max_completion_tokens` are
`NOT_GIVEN` in pipecat's request builder, and there is **no `stop` key in the
body at any value** (`pipecat/src/pipecat/services/openai/base_llm.py:336-368`),
so Groq applied its own model-max. Meanwhile the prompt teaches exactly this
shape: `MODE_PROTOCOL` asks for a control token on line one, and the layer files
carry `CUSTOMER:`/`WRONG:`/`RIGHT:` few-shot transcripts. A model shown labelled
transcripts and told to prefix its reply with a token will eventually continue
the transcript instead of taking one turn in it.

The bounds here stop a runaway generation; `ReplySanitizer` is what actually
keeps a turn to one reply and one question.

STOP: the model may not begin a second speaker's turn
-----------------------------------------------------
Every entry is a label that only appears once the model has stopped replying and
started writing dialogue. All of them are newline-prefixed, and that is
load-bearing rather than cosmetic.

A bare `MODE:` was tried first and is a trap. `MODE_PROTOCOL` instructs the model
to make `MODE: ASK` the FIRST line of its reply, so a bare stop matches at
position 0 and generation halts before a single word is produced. Measured
against Groq on 2026-08-27, same prompt, temperature 0:

    no stop         -> "MODE: ASK\n\nMeeru emi telusukovalanukuntunnaru?"
    stop "MODE:"    -> ""                    <- every turn becomes silence
    stop "\nMODE:"  -> full reply, intact

The empty completion still reports `finish_reason: "stop"`, so nothing upstream
would have flagged it: the caller would simply have heard nothing, on every turn
where the model obeyed its own protocol.

The limit of this defence is real. A stop sequence cannot catch a marker the
model runs together with preceding text, and run 12 emitted
`...need agreement.MODE: CLOSE` with no newline at all. No safe stop sequence
matches that. It belongs to `ReplyFilter`, which strips `MODE:` wherever it
appears. Stop sequences are the cheap backstop, not the whole answer.
MAX: a backstop, not a length policy
------------------------------------
The cap counts REASONING tokens, not just spoken ones, and that makes a
tight budget actively dangerous on this model. Measured against Groq on
2026-08-27 with `reasoning_effort=low`, reasoning tokens per turn:

    normal answer      24        "what do you do"   78
    bill amount        51        frustrated caller  76
    "not interested"   55        "too expensive"    78

At a cap of 80, six of seven turns ended with `finish_reason: "length"`, and
the three hardest returned an EMPTY reply -- 78 reasoning tokens spent, 2 left
for speech. Those three are "what do you do", an angry caller, and a price
objection: precisely the turns the objection playbooks exist for. A cap chosen
to keep replies short would have silenced the agent exactly where it matters
most, and silence reads as a dropped call.

So the cap is deliberately loose. Spoken content measured 19-44 tokens and
reasoning peaked at 152, so 400 clears both with room to spare and every test
turn finishes on `stop`. It exists only to stop a runaway generation, and it is
NOT what keeps replies short.

What keeps replies short is `ReplySanitizer`, which ends the turn at its first
question mark. That rule is content-aware, costs no tokens, and cannot starve
the model of room to think.

Scope
-----
Conversational replies only. Extraction, voicemail detection and summarisation
are not spoken to anyone, have no MODE contract, and legitimately return longer
structured output -- bounding those would corrupt them for no benefit. Hence the
explicit opt-in at the single conversational call site rather than a default.
"""

from __future__ import annotations

# All newline-prefixed. A bare "MODE:" matches the protocol's own leading
# header at position 0 and returns an EMPTY completion -- see the docstring.
#
# EXACTLY FOUR. Groq rejects a longer list outright:
#   "'stop' : maximum number of items is 4"  (HTTP 400, measured 2026-08-27)
# which would have failed every call, not just a malformed one. These four are
# the highest-probability markers; ReplySanitizer still covers Agent:,
# Assistant:, BOT:, WRONG: and RIGHT:, so nothing is undefended -- they simply
# do not get the cheap early stop.
REPLY_STOP_SEQUENCES = [
    "\nMODE:",  # the control token run 12 spoke aloud
    "\nCUSTOMER:",  # the few-shot label used in the prompt layers
    "\nCaller:",
    "\nUser:",
]

# Groq: "maximum number of items is 4". Exceeding it is a 400 on every request,
# so this is asserted at import rather than discovered on a live call.
MAX_STOP_SEQUENCES = 4
assert len(REPLY_STOP_SEQUENCES) <= MAX_STOP_SEQUENCES, (
    f"{len(REPLY_STOP_SEQUENCES)} stop sequences; the provider accepts "
    f"at most {MAX_STOP_SEQUENCES}"
)

# A runaway backstop, NOT a length policy -- see the docstring. Must clear the
# worst measured reasoning burn (152 tokens) plus a full reply (44), because the
# budget is shared between thinking and speaking.
MAX_REPLY_TOKENS = 400


def conversational_extra(extra: dict | None = None) -> dict:
    """Merge the reply bounds into a provider `extra` dict.

    Returns a new dict; the caller's is never mutated, because the same settings
    object is reused across every service built in one run.
    """
    merged = dict(extra or {})
    existing = list(merged.get("stop") or [])
    for marker in REPLY_STOP_SEQUENCES:
        if marker not in existing:
            existing.append(marker)
    # Truncating beats a 400. A dropped marker costs one early stop and the
    # sanitizer still removes it from the reply; an over-long list costs every
    # call on the workflow.
    if len(existing) > MAX_STOP_SEQUENCES:
        existing = existing[:MAX_STOP_SEQUENCES]
    merged["stop"] = existing
    return merged
