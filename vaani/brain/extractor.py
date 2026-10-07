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

log = logging.getLogger("vaani.extractor")

SYSTEM = """You extract facts from a sales phone call.

Return ONLY a JSON object. No prose, no markdown, no code fences.

Rules:
- Include a key ONLY if the customer actually stated it. Never infer or guess.
- If the customer did not answer something, omit that key entirely.
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


def apply_to_state(state, data: dict, fields: list[str]) -> None:
    """Merge extracted facts into CallState. Tolerant of extra/missing keys."""
    if not data:
        return
    for key, value in data.items():
        if key in ("objection", "disqualified", "disqualify_reason",
                   "buying_signal", "next_step_agreed", "must_end", "end_reason"):
            continue
        if value in (None, "", "unknown", "not stated"):
            continue
        if key in fields:
            state.learn(key, str(value))

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
        state.disqualified = True
        state.disqualify_reason = str(data.get("disqualify_reason", "stated disqualifier"))


def spawn(llm, state, fields, disqualifiers, user_text, agent_text="") -> asyncio.Task:
    """Fire-and-forget. The reply has already gone out; this catches up state."""

    async def _run() -> None:
        data = await extract(llm, fields, disqualifiers, user_text, agent_text)
        apply_to_state(state, data, fields)
        if data:
            log.info("extracted %s", data)

    task = asyncio.create_task(_run())
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return task
