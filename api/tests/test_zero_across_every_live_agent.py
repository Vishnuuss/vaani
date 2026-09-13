"""The zero fix, driven through the field list of EVERY live agent.

The client's instruction on 13 Sep: test it on all the agents, not one. The
fix lives in `amounts.py`, which every agent shares, so a change there that
helps MB Solar can just as easily break the property agent's budget question.
This drives each one.

FIELDS below is the real `extraction_variables` order read off
vaani.bswealthfinance.com on 13 Sep 2026. It is a fixture, not a fetch: a test
that calls the live server fails when the network does, and tells you nothing
about the code.

Two groups, and both matter:

  - Agents WITH a money field (`bill`/`amount`/`spend`/`budget`/`consumption`,
    per `state.MONEY_FIELDS`). Zero must be recorded and must retire the field.
    MB Solar's `monthly_bill` is run 939's field. The property agent's `budget`
    is the one nobody looked at, and "జీరో" to a budget question was failing
    exactly the same way.

  - Agents WITHOUT one. Nothing about them should change. They are here so
    that if someone later widens MONEY_FIELDS, or makes the zero parse fire
    somewhere it should not, these go red instead of a caller finding out.
"""

from __future__ import annotations

import pytest

from api.services.vaani import amounts
from api.services.vaani.state import CallState, _is_money_field


# Read off the live server, 13 Sep 2026. Workflow 1 is an empty stub.
FIELDS = {
    "wf2 MB Solar Hub": [
        "property_type", "monthly_bill", "location",
        "roof_available", "customer_name", "assessment_agreed"],
    "wf3 HDFC Bank Loan": [
        "loan_required", "loan_type", "do_not_call", "summary"],
    "wf4 Tata Solar": [
        "house_ownership", "solar_planning", "do_not_call",
        "summary", "lead_score"],
    "wf5 BS Wealth Investment": [
        "currently_investing", "investment_type", "interested",
        "do_not_call", "summary", "lead_score"],
    "wf6 BS Wealth Property": [
        "property_interest", "property_type", "budget", "location",
        "timeline", "lead_score", "do_not_call", "summary"],
}

WITH_MONEY = {a: f for a, f in FIELDS.items() if any(_is_money_field(x) for x in f)}
WITHOUT_MONEY = {a: f for a, f in FIELDS.items() if not any(_is_money_field(x) for x in f)}

ZEROS = ["జీరో", "సున్నా", "zero", "0"]


def _state(fields: list[str]) -> CallState:
    s = CallState()
    s.required_fields = list(fields)
    return s


def test_the_fixture_covers_both_groups():
    """A guard on the test itself, not on the code.

    If the agents are reconfigured and every money field disappears, the
    parametrised tests below would all silently vanish and this file would
    pass while testing nothing.
    """
    assert WITH_MONEY, "no agent has a money field -- fixture is stale"
    assert WITHOUT_MONEY, "no agent lacks a money field -- fixture is stale"


@pytest.mark.parametrize("said", ZEROS)
@pytest.mark.parametrize("agent", sorted(WITH_MONEY))
def test_zero_retires_the_money_field_on_each_agent(agent, said):
    """Run 939's failure, re-run against every agent that can hit it."""
    fields = FIELDS[agent]
    money = next(f for f in fields if _is_money_field(f))

    state = _state(fields)
    # Walk the checklist to the money field, as the call does.
    for f in fields:
        if f == money:
            break
        state.known[f] = "x"

    assert state.still_need[0] == money
    assert state.note_amount(said) is True, (
        f"{agent}: {said!r} was not recorded -- {money} will be asked again")
    assert state.known[money] == "0"
    assert money not in state.still_need


@pytest.mark.parametrize("agent", sorted(WITHOUT_MONEY))
def test_an_agent_with_no_money_field_is_untouched(agent):
    """Zero must not invent a value where no money was ever asked for."""
    state = _state(FIELDS[agent])
    for said in ZEROS:
        assert state.note_amount(said) is False
    assert state.known == {}
    assert state.still_need == FIELDS[agent]


@pytest.mark.parametrize("agent", sorted(WITH_MONEY))
def test_a_real_figure_still_parses_on_each_agent(agent):
    """The zero path must not have shadowed the ordinary one."""
    fields = FIELDS[agent]
    money = next(f for f in fields if _is_money_field(f))
    state = _state(fields)
    for f in fields:
        if f == money:
            break
        state.known[f] = "x"
    assert state.note_amount("రెండు వేలు") is True
    assert state.known[money] == "2000"


@pytest.mark.parametrize("agent", sorted(WITH_MONEY))
def test_a_time_is_still_not_money_on_each_agent(agent):
    """The NOT_MONEY guard is unchanged by the zero path."""
    fields = FIELDS[agent]
    money = next(f for f in fields if _is_money_field(f))
    state = _state(fields)
    for f in fields:
        if f == money:
            break
        state.known[f] = "x"
    assert state.note_amount("రేపు పది గంటలకు") is False
    assert money not in state.known


def test_zero_is_not_read_out_of_an_unrelated_sentence():
    """`note_amount` only fires while a money field is the live question.

    Without this, a caller saying "zero" about anything at all -- his roof, his
    interest -- would land in the bill.
    """
    state = _state(FIELDS["wf2 MB Solar Hub"])
    assert state.still_need[0] == "property_type"   # not a money field
    assert state.note_amount("జీరో") is False
    assert state.known == {}
