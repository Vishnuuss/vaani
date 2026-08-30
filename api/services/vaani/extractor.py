"""Fact extraction -- fills CallState.known, entirely OFF the critical path.

Found by testing the brain end to end: without this, `KNOWN` stays empty
forever. `STILL_NEED` then lists fields the caller already answered, and the
agent eventually re-asks them. Re-asking is one of the fastest ways to lose
trust on an outbound call -- it says nobody was listening.

The design constraint: extraction must NEVER delay a reply. So it runs as a
detached task after the turn is already committed, and its result lands in the
state block for the NEXT turn. One turn of lag is invisible in conversation;
300ms of added latency is not.

A small model is right for this. It is a structured-output task, not a
reasoning one -- see FINDINGS §2 on not paying 120B prices for small jobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from api.services.vaani import amounts
from api.services.vaani.completeness import strip_fillers
from api.services.vaani.negation import is_bare_denial, is_negative
from api.services.vaani.state import _is_money_field

log = logging.getLogger("vaani.extractor")

SYSTEM = """You extract facts from a sales phone call.

Return ONLY a JSON object. No prose, no markdown, no code fences.

Rules:
- Include a key ONLY if the customer actually stated it. Never infer or guess.
- If the customer did not answer something, omit that key entirely.
- NEVER answer false, "no" or "none" for a field unless the customer actually
  said no. An answer you cannot make sense of is NOT a no -- omit the key. An
  answer to a different question is NOT a no -- omit the key. Saying where
  something is ("on the factory", "on our terrace") is a YES, not a no.
- Values must be short and in English, normalised.
- Also include "objection" if they raised one, as one of:
  price, timing, spouse, trust, not_interested, busy, competitor
- Include "disqualified": true only if a stated disqualifier clearly applies.
- Include "buying_signal": true if the customer asked to book, asked someone to
  come and look, or asked a closing question (price, timing, how to proceed).
- Include "next_step_agreed": true ONLY if the customer has actually ACCEPTED a
  specific next step -- a visit, a callback, or a stated time. Not if they are
  still considering it.
- Include "must_end": true if the customer asked to be removed from the list,
  accused the company of fraud, said this is the wrong number, is a child, or is
  angry and wants the call to stop. Set "end_reason" to a short phrase.
"""


def _coerce_json(text: str) -> dict:
    """Models wrap JSON in fences or prose. Recover the object anyway."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def extract(llm, fields: list[str], disqualifiers: list[str],
                  user_text: str, agent_text: str = "") -> dict:
    """Pull structured facts from one exchange. Returns {} on any failure."""
    if not user_text.strip():
        return {}

    schema_hint = ", ".join(f'"{f}"' for f in fields) or '"none"'
    dq = "\n".join(f"- {d}" for d in disqualifiers) or "- none"
    prompt = (
        f"Fields to look for: {schema_hint}\n"
        f"Disqualifiers:\n{dq}\n\n"
        f"AGENT: {agent_text}\n"
        f"CUSTOMER: {user_text}\n\n"
        "JSON:"
    )

    try:
        raw = await llm.complete(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": prompt}],
            max_tokens=180, temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - extraction must never break a call
        log.warning("extraction failed: %s", exc)
        return {}

    return _coerce_json(raw)


def _is_plausible_money(value) -> bool:
    """Would a real monthly electricity bill ever be this number?"""
    try:
        rupees = float(str(value).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        # Not a bare figure -- "one lakh", "10-15k". Leave it alone; the
        # synchronous parser in `state.note_amount` is the one that reads
        # phrases, and it applies the same bounds itself.
        return True
    return amounts.MIN_PLAUSIBLE <= rupees <= amounts.MAX_PLAUSIBLE


_FALSEY = {False, "false", "no", "none", "not available", "nil", "0"}


def _is_denial(value) -> bool:
    """Is this extracted value asserting that something is NOT the case?"""
    if isinstance(value, bool):
        return value is False
    return isinstance(value, str) and value.strip().lower() in _FALSEY


def apply_to_state(state, data: dict, fields: list[str],
                   user_text: str = "") -> None:
    """Merge extracted facts into CallState. Tolerant of extra/missing keys.

    `user_text` is what the caller actually said. It is the evidence for any
    NEGATIVE fact -- see the gate below and `negation.py`.
    """
    # Read and cleared at the top, BEFORE the empty-data return below.
    #
    # Clearing it at the bottom instead latched it forever: a turn that
    # extracted nothing returned early, the flag stayed raised, and every later
    # disqualifier in the call was silently suppressed. The first version of
    # this did exactly that, and `test_the_grace_lasts_exactly_one_turn` is why
    # it is not still doing it. One turn of grace means one turn.
    misheard = bool(getattr(state, "misheard_last_turn", False))
    state.misheard_last_turn = False

    if not data:
        return
    # A negative fact needs an actual negation in the caller's own words.
    #
    # Run 312: "ఎక్కడండి ట్రాక్టర్ పైన మా ట్రాక్టర్ పైన" (on our tractor --
    # itself a garbling of "on our factory") produced `roof_available: false`,
    # and the agent told a factory owner solar was not possible for him and hung
    # up. There is no "no" anywhere in that sentence. The model inferred one,
    # which is exactly what the prompt above forbids and cannot enforce.
    #
    # Dropping the key instead leaves the field unknown, which puts it back in
    # STILL_NEED and the agent asks again. One wasted question against a lost
    # lead is not a close call.
    #
    # Only applied when we actually have the caller's words: a caller-text-less
    # call (the final end-of-call extraction re-reads the whole transcript) is
    # left alone rather than silently stripped of every negative.
    if user_text and not is_negative(user_text):
        denied = [k for k, v in data.items()
                  if k in fields and _is_denial(v)]
        for key in denied:
            log.info("dropping %s=%r -- no negation in %r",
                     key, data[key], user_text[:80])
            data = {k: v for k, v in data.items() if k != key}

    # Whether this turn told us anything factual at all. See the disqualifier
    # gate below -- an utterance that filled no field cannot have disqualified
    # anybody either.
    learned_this_turn = False

    for key, value in data.items():
        if key in ("objection", "disqualified", "disqualify_reason",
                   "buying_signal", "next_step_agreed", "must_end", "end_reason"):
            continue
        if value in (None, "", "unknown", "not stated"):
            continue
        if key in fields:
            if _is_money_field(key) and not _is_plausible_money(value):
                # Run 295 stored `monthly_bill: 62`. The caller had said
                # "60 ... aaa ... 70" and was cut off after "60"; what reached
                # the extractor was the fragment "62", and `learn` takes
                # whatever it is handed. `amounts.py` has known since run 286
                # what a monthly electricity bill can credibly be -- that check
                # simply was not on this path, only on the synchronous one.
                #
                # Dropped rather than stored: an implausible figure in the lead
                # record is worse than a null, because null is visibly missing
                # and 62 looks like an answer. The state block asks the caller
                # to confirm instead.
                state.doubted = amounts.parse_amount(str(value))
                continue
            # "um" is not part of anybody's name.
            #
            # Run 314 stored `customer_name: "ఉమ్ భాస్కర్"` because the caller
            # hesitated before saying it, and the agent then addressed him as
            # "ఉమ్ భాస్కర్ గారు" -- Mr. Um Bhaskar -- for the remainder of the
            # call. The filler vocabulary already existed; it had just never
            # been applied to a value on its way into the record.
            state.learn(key, strip_fillers(str(value)))
            learned_this_turn = True

    objection = data.get("objection")
    if isinstance(objection, str) and objection:
        state.note_objection(objection)
    elif isinstance(objection, list):
        for item in objection:
            state.note_objection(str(item))

    # Latching, never un-setting: a caller who agreed does not un-agree because
    # a later turn failed to mention it.
    if data.get("buying_signal") is True:
        state.buying_signal = True
    if data.get("next_step_agreed") is True:
        state.next_step_agreed = True

    if data.get("must_end") is True:
        state.must_end = True
        state.end_reason = str(data.get("end_reason", "the caller wants the call to end."))

    if data.get("disqualified") is True:
        if misheard:
            # The agent said "I could not hear you" one turn ago. Disqualifying
            # on the reply to that is how run 312 ended: two garbled words about
            # a tractor, and a factory owner was told solar was not possible for
            # him. A disqualifier is the one decision in this call that cannot
            # be walked back, so it needs an utterance we actually understood.
            log.info("not disqualifying (%s) -- previous turn was a misheard "
                     "repair", data.get("disqualify_reason"))
        elif is_bare_denial(user_text) and not learned_this_turn:
            # Run 324, and it is the worst kind of failure this agent has: it
            # ended a live call on a fact it had never asked for.
            #
            #   49.48  BOT   మీది సొంత ఇల్లా, అపార్ట్‌మెంటా, లేదా కమర్షియల్ ప్లేసా?
            #   53.35  USER  కాదు.
            #   57.52  BOT   సార్, మీరు రూఫ్ లేదా టెర్రస్ కలిగి లేకపోతే
            #                మేము కొనసాగించలేము. థాంక్యూ.
            #
            # He said "no" to a three-way question about his property type. The
            # agent read that as "no roof", disqualified him and hung up at 64
            # seconds. `roof_available` is null in the saved record -- the roof
            # question was never asked, and there is no turn in that transcript
            # where he could have answered it.
            #
            # So a disqualifier now needs the fact behind it. The extractor may
            # still decide the caller is out; it may not decide it from a field
            # it has neither been told nor asked about. Not a rule about roofs:
            # the same shape would end a call on a budget or a city that was
            # never discussed, and hanging up is the one move that cannot be
            # walked back.
            log.info("not disqualifying (%s) -- %r is a bare denial and this "
                     "turn filled no field, so there is no fact behind it",
                     data.get("disqualify_reason"), user_text[:60])
        else:
            state.disqualified = True
            state.disqualify_reason = str(
                data.get("disqualify_reason", "stated disqualifier"))



def spawn(llm, state, fields, disqualifiers, user_text, agent_text="") -> asyncio.Task:
    """Fire-and-forget. The reply has already gone out; this catches up state."""

    async def _run() -> None:
        data = await extract(llm, fields, disqualifiers, user_text, agent_text)
        apply_to_state(state, data, fields, user_text)
        if data:
            log.info("extracted %s", data)

    task = asyncio.create_task(_run())
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return task
