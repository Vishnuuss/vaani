"""Run 318: the first call after the fixes, and the first lost on content alone.

WR-TEL-OUT-55669215, 200 seconds, `call_duration_exceeded`, nothing booked.
p50 was 0.892s, "ten గంటలకు" was right, the subsidy answer was right. It still
failed, and none of it was speed.

Two defects, both about a number.

1. It sized a solar system on the phone:

       USER : మా ఫ్యాక్టరీ ... 300 స్క్వేర్ మీటర్స్ ... ఎన్ని ప్యానెల్స్?
       BOT  : 300 square meters రూఫ్ మీద సుమారు 30 kW ... 80-100 panels

   Nobody said 30 kW or 80-100. A factory owner will repeat that to a vendor.

2. It offered the same appointment eight times in ninety seconds, to a caller
   asking real questions about cost, because once its two-ask budget was spent
   the state block said "STILL_NEED: []" and nothing else -- and the model fell
   back on a history full of offers.
"""

import pytest

from api.services.vaani import guardrails
from api.services.vaani.guardrails import (
    check,
    invented_quantities,
    numbers_in,
)
from api.services.vaani.state import CallState

# Numbers the MB Solar Hub knowledge base actually contains.
KB = numbers_in(
    "30,000 rupees per kW for the first 2 kW and 18,000 per kW up to 3 kW, "
    "capped at 78,000. 400 units a month. 25 years. up to 500 kW.")


# --- 1. the invented system size --------------------------------------------

def test_the_sentence_that_shipped():
    """300 was the caller's own figure. 30, 80 and 100 came from nowhere."""
    said = "300 square meters రూఫ్ మీద సుమారు 30 kW సిస్టమ్, అంటే 80-100 panels"
    assert sorted(set(invented_quantities(said, KB | {"300"}))) == ["100", "30", "80"]


def test_the_subsidy_answer_is_not_flagged():
    """The whole point of a whitelist over a blacklist.

    Banning kW would have blocked the best answer this agent has.
    """
    said = "మొదటి 2 kW కి 30,000 rupees per kW, తర్వాత 18,000, మొత్తం 78,000 వరకు"
    assert invented_quantities(said, KB) == []


@pytest.mark.parametrize("said", [
    "నెలకి 400 units generate అవుతాయి",
    "panels 25 సంవత్సరాలు పైగా ఉంటాయి",
    "GHS కి 500 kW వరకు",
])
def test_other_knowledge_base_figures_pass(said):
    assert invented_quantities(said, KB) == []


def test_a_number_the_caller_supplied_is_allowed():
    """Echoing their own figure is acknowledgement, not a claim."""
    report = check("మీ 300 square meters రూఫ్ కి సైట్ అసెస్‌మెంట్ చేస్తాం",
                   caller_said="మా ఫ్యాక్టరీ 300 స్క్వేర్ మీటర్స్ ఉంటుంది",
                   known_numbers=KB)
    assert not [v for v in report.violations if v.rule == "no_invented_quantity"]


def test_plain_speech_with_numbers_is_untouched():
    """A bare number in ordinary talk is not a technical claim. Only sentences
    carrying a unit are examined, or this fires on nearly every turn."""
    assert invented_quantities("ఒక నిమిషం మాట్లాడొచ్చా?", set()) == []
    assert invented_quantities("రెండు ఆప్షన్స్ ఉన్నాయి", set()) == []


def test_comma_grouped_digits_are_one_number():
    """"30,000" tokenises as "30" and "000". An earlier version normalised that
    trailing "000" to "0" and reported the subsidy answer as invented."""
    assert "30000" in numbers_in("30,000 rupees")
    assert "0" not in numbers_in("30,000 rupees")


def test_it_is_a_blocking_rule():
    """An invented spec is a compliance failure, like an invented price: the
    reply is replaced, not merely logged."""
    assert "no_invented_quantity" in guardrails.BLOCKING_RULES


def test_no_knowledge_base_means_no_enforcement():
    """An empty whitelist means nothing was compiled, not that every number is
    banned. Enforcing against nothing would gag the agent on its first line."""
    report = check("సుమారు 30 kW సిస్టమ్ పెట్టవచ్చు", known_numbers=set())
    assert not [v for v in report.violations if v.rule == "no_invented_quantity"]


def test_the_violation_names_the_numbers():
    report = check("సుమారు 30 kW, 80 panels", known_numbers=KB)
    hit = [v for v in report.violations if v.rule == "no_invented_quantity"]
    assert hit and "30" in hit[0].evidence and "80" in hit[0].evidence


# --- 2. an empty checklist is not an instruction -----------------------------

def state() -> CallState:
    return CallState(
        required_fields=["assessment_agreed"],
        questions={"assessment_agreed": "సైట్ అసెస్‌మెంట్ షెడ్యూల్ చేయాలా?"},
    )


def test_an_exhausted_checklist_stops_the_offer():
    """The defect. Eight offers in ninety seconds after the budget ran out."""
    s = state()
    s.ask_counts["assessment_agreed"] = s.MAX_ASKS_PER_FIELD
    block = s.render()

    assert s.still_need == []
    assert "NOTHING LEFT TO ASK" in block
    assert "OFFER EXACTLY" not in block, "it offered a time again"


def test_it_says_what_to_do_instead_of_only_what_not_to():
    """"STILL_NEED: []" alone is what caused this. The replacement has to carry
    a positive instruction or the model falls back on the history again."""
    s = state()
    s.ask_counts["assessment_agreed"] = s.MAX_ASKS_PER_FIELD
    block = s.render()
    assert "Answer whatever they ask" in block
    assert "END THE CALL" in block


def test_a_live_checklist_still_offers_the_time():
    """The fix must not stop a first, legitimate offer."""
    block = state().render()
    assert "OFFER EXACTLY" in block
    assert "NOTHING LEFT TO ASK" not in block


def test_a_booking_still_outranks_the_empty_checklist():
    """Branch order: an actual appointment must win over 'nothing left to ask'."""
    s = state()
    s.ask_counts["assessment_agreed"] = s.MAX_ASKS_PER_FIELD
    s.appointment_iso = "2026-09-01T10:00:00+05:30"
    block = s.render()
    assert "BOOKED" in block
    assert "NOTHING LEFT TO ASK" not in block


def test_must_end_still_outranks_it():
    s = state()
    s.ask_counts["assessment_agreed"] = s.MAX_ASKS_PER_FIELD
    s.must_end = True
    s.end_reason = "The caller asked to be removed."
    block = s.render()
    assert "STOP." in block
    assert "NOTHING LEFT TO ASK" not in block
