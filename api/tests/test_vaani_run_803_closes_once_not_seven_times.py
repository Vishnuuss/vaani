"""Run 803: the agent said the same goodbye seven times and could not hang up.

The end of the call, 6 Sep, verbatim from the log:

    BOT : ...బుధవారం మధ్యాహ్నం two o'clock సైట్ సర్వే బుక్ చేసేశాం. థాంక్యూ.
    USER: ఆ ఓకే ఓకే               -> BOT: [byte-identical sentence]
    USER: పెద్ద రా                 -> BOT: [byte-identical sentence]
    USER: థ్యాంక్ యూ. ఆ.           -> BOT: [byte-identical sentence]
    USER: మళ్ళీ కలుద్దాం బాయ్       -> BOT: [byte-identical sentence]
    USER: హలో ఓకే సరే బాయ్         -> BOT: [byte-identical sentence]
    USER: సరే థ్యాంక్ యూ బాయ్       -> BOT: [byte-identical sentence]
    USER: బాయ్ బాయ్ కట్ చేస్తారా మీరు?

187 seconds, the last minute of it pure loop, ending with the caller asking the
agent to please hang up -- which it could not do.

Two causes, both here:

1. `render()` builds the BOOKED branch fresh every turn with NO MEMORY that it
   has already been said. The instruction is identical, so the model's reply is
   identical. The client's own diagnosis -- "how can the LLM give the same
   response again and again if my answer is different" -- is exactly right: his
   words are in the context, but a system order stapled on AFTER them outranks
   him, and that order never changes.

2. Nothing hangs up. `EndCallBridge` fires on `state.must_end`, which only
   `MODE: END` sets. The model delivered seven goodbyes without ever emitting
   it, so the line stayed open. There is no code path that ends a call which
   has finished its job.

The fix is a counter, not a prompt: say goodbye once, and if the call is still
open on the next turn, END it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from api.services.vaani.state import CallState

FIELDS = ["property_type", "monthly_bill", "location",
          "roof_available", "customer_name", "assessment_agreed"]


def _booked_state() -> CallState:
    """Run 803's state at the moment the appointment was confirmed."""
    state = CallState(required_fields=list(FIELDS), questions={})
    state.known = {f: "x" for f in FIELDS}
    when = (datetime.now() + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    state.appointment_iso = when.isoformat()
    return state


def _turn(state: CallState, caller_said: str) -> str:
    """One exchange: the caller speaks, the agent's instruction is rebuilt."""
    state.last_user_text = caller_said
    block = state.render()
    state.note_reply_delivered()
    return block


def test_the_agent_closes_once_and_then_ends_the_call():
    state = _booked_state()

    first = _turn(state, "సరే")
    assert not state.must_end, "the goodbye has not been spoken yet"
    assert "END THE CALL" in first

    _turn(state, "ఆ ఓకే ఓకే")
    assert state.must_end, (
        "run 803: the agent had already said goodbye once and was given the "
        "identical instruction again -- five more times"
    )


def test_a_caller_saying_goodbye_ends_the_call_immediately():
    """"బాయ్" is not an invitation to repeat the booking."""
    from api.services.vaani import triage

    state = _booked_state()
    triage.apply(state, "సరే థ్యాంక్ యూ బాయ్")
    assert state.must_end


def test_asking_the_agent_to_hang_up_ends_the_call():
    """Run 803's last line, and the clearest possible signal."""
    from api.services.vaani import triage

    state = _booked_state()
    triage.apply(state, "బాయ్ బాయ్ కట్ చేస్తారా మీరు")
    assert state.must_end


def test_the_closing_instruction_carries_what_the_caller_just_said():
    """"Ask nothing further" was being read as "say nothing new".

    The caller said "బుధవారం చాలు" -- Wednesday is enough, drop the Friday --
    which is new information and not a question, so the `_is_question` gate at
    state.py:636 missed it and the agent repeated the two-slot booking at him.
    """
    state = _booked_state()
    block = _turn(state, "బుధవారం చాలు")
    assert "బుధవారం చాలు" in block, (
        "the caller's words must reach the instruction, not only the transcript"
    )
