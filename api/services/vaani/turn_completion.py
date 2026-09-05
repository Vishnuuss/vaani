"""Semantic turn completion, composed with Vaani's own output format.

What this is for
----------------
The client, 5 Sep, quoting a talk on turn detection:

    "the LLM can start thinking or reasoning even before the user has finished
     speaking ... if the user is clearly not done, model just emits these small
     signals ... but if the user is done, then the model can immediately
     continue into the actual response."

pipecat ships that protocol —
`turns/user_stop/llm_turn_completion_user_turn_stop_strategy.py`, driven by
`UserTurnCompletionLLMServiceMixin`. The LLM is polled while the caller is still
talking and answers with a marker instead of a reply:

    ✓   complete    -> speak the response that follows
    ○   incomplete  -> they were cut off; wait, re-prompt after a few seconds
    ◐   incomplete  -> they are thinking; wait longer

The audio half stays with `TeluguTurnAnalyzer`, which is the only turn detector
that reads Telugu at all. This adds the semantic half, which is exactly the
audio+text combination the talk recommends.

The collision this module exists to resolve
-------------------------------------------
pipecat's default instructions say, in capitals:

    "Every single response MUST begin with a turn completion indicator."

Vaani's `MODE_PROTOCOL` says:

    "the first line of your reply is always exactly one of: MODE: ASK ..."

Both claim the first token, so shipping pipecat's defaults unchanged would break
the MODE line — and MODE is load-bearing. `MODE: END` is the only thing that
hangs up the call (`state.must_end`), and `MODE: CLOSE` is what stops the agent
asking further questions once a time is agreed. Losing it does not degrade the
agent politely; it makes it unable to end a call.

`UserTurnCompletionConfig` takes custom `instructions`, so the two compose
rather than compete: the marker comes first, then the MODE line, then the
speech. The mixin strips the marker before anything downstream sees the text,
so `ReplySanitizer._note_mode` still finds MODE where it expects it.

NOT WIRED IN YET, deliberately
------------------------------
The saving only exists if inference is triggered MID-UTTERANCE, which needs
interim transcripts. `saarika:v2.5` emits none, and the switch to
`saaras:v3-realtime` was reverted after run 780 (see
`test_realtime_stt_one_turn_per_utterance`). So this is written, tested and
left dark until realtime STT is verified on a live call. Wiring it now would be
untestable code that changes the reply format of every agent.
"""

from __future__ import annotations

# Marker first, MODE second, speech third. The order is the whole point.
#
# Written in the same register as pipecat's original -- blunt, repetitive, and
# explicit about the format -- because that is what the model actually follows.
VAANI_TURN_COMPLETION_INSTRUCTIONS = """
CRITICAL — MANDATORY RESPONSE FORMAT. Every reply has THREE parts, in this
order, and the first two are never spoken aloud:

  1. a turn completion marker
  2. the MODE line
  3. what you say out loud

Decide the marker first. Ask: has the caller given you enough to reply to?

Use ✓ (COMPLETE) when:
- they answered your question with actual content
- they asked you something, however short
- they made a complete statement or request

Use ○ (INCOMPLETE, they will continue in a moment) when:
- they were cut off mid-word or mid-sentence
- they are part-way through a number and have stopped: "నా నంబర్ తొంభై ఒకటి..."
- they said only a filler: "అ...", "ఉమ్...", "అంటే..."

Use ◐ (INCOMPLETE, they need longer) when:
- they asked for time: "ఒక్క నిమిషం", "ఆలోచించనివ్వండి", "hold on"
- they are deliberating out loud: "హ్మ్...", "ఏమో..."
- they acknowledged without answering: "సరే...", "అలాగా..."

THE FORMATS, and there are only three:

1. COMPLETE — the marker, a space, the MODE line, a blank line, your reply:

   ✓ MODE: ASK

   మంచిది సార్, మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?

2. INCOMPLETE SHORT — the single character ○ and NOTHING else. No MODE line,
   no words, no punctuation.

3. INCOMPLETE LONG — the single character ◐ and NOTHING else.

A grammatically finished sentence is not always a finished TURN. "అది మంచి
ప్రశ్న" is a complete sentence and an unfinished thought — that is ◐, not ✓.

When in doubt use ✓. Waiting when the caller has finished is the failure they
notice; they are sitting in silence wondering if you heard them.
"""


def compose_instructions(mode_protocol: str) -> str:
    """The completion instructions, with Vaani's output format appended.

    Kept as a function rather than a constant so the MODE text has exactly one
    source of truth -- `compiler.MODE_PROTOCOL`. A second copy of it here would
    drift, and the failure would be silent: the agent would still talk, and
    would simply stop being able to hang up.
    """
    return VAANI_TURN_COMPLETION_INSTRUCTIONS.rstrip() + "\n" + mode_protocol
