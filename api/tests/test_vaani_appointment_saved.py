"""A booked appointment has to survive the call.

Run 262 agreed a site visit out loud and stored `assessment_agreed: true`. No
day, no time. The vendor could not act on it and the caller had been told a slot
that existed only in a transcript, so the booking was, in practice, lost.

The time is parsed deterministically by Vaani rather than by the extraction LLM,
because the distinction that matters -- consenting to a visit versus choosing a
slot -- is exactly the one run 262's agent got wrong.
"""

from __future__ import annotations

import pytest

from api.services.vaani.state import CallState


class Engine:
    """Just enough of PipecatEngine to exercise the merge."""

    def __init__(self, gathered=None):
        self._gathered_context = dict(gathered or {})

    # The two methods under test, copied by import rather than reimplemented.
    from api.services.workflow.pipecat_engine import PipecatEngine

    attach_vaani_state = PipecatEngine.attach_vaani_state
    get_gathered_context = PipecatEngine.get_gathered_context


@pytest.mark.asyncio
async def test_a_booked_time_reaches_the_saved_record():
    st = CallState()
    st.appointment_iso = "2026-08-29T10:00:00+05:30"
    engine = Engine({"assessment_agreed": True})
    engine.attach_vaani_state(st)

    gathered = await engine.get_gathered_context()
    assert gathered["appointment_time"] == "2026-08-29T10:00:00+05:30"
    assert gathered["extracted_variables"]["appointment_time"] == st.appointment_iso


@pytest.mark.asyncio
async def test_nothing_is_added_when_no_slot_was_agreed():
    """The run 262 shape: consent, but no time. Do not invent one."""
    st = CallState()
    engine = Engine({"assessment_agreed": True})
    engine.attach_vaani_state(st)

    gathered = await engine.get_gathered_context()
    assert "appointment_time" not in gathered


@pytest.mark.asyncio
async def test_the_existing_extracted_variables_are_preserved():
    st = CallState()
    st.appointment_iso = "2026-08-29T10:00:00+05:30"
    engine = Engine({"extracted_variables": {"customer_name": "సుబ్బరాజు"}})
    engine.attach_vaani_state(st)

    gathered = await engine.get_gathered_context()
    assert gathered["extracted_variables"]["customer_name"] == "సుబ్బరాజు"
    assert gathered["extracted_variables"]["appointment_time"]


@pytest.mark.asyncio
async def test_an_engine_with_no_vaani_state_is_unaffected():
    """Vaani can fail to build; the rest of the call must not notice."""
    engine = Engine({"assessment_agreed": True})
    gathered = await engine.get_gathered_context()
    assert gathered == {"assessment_agreed": True}
