"""The lead record is the only artefact the client reads. It was unguarded.

Run 853 stored `monthly_bill: 15`. The caller's bill reached Sarvam as
"పది ఐదు గుంధల అండి" -- a garbled "పదిహేను వేలు", fifteen thousand.

Everything that should have caught it worked correctly:

  - `amounts.parse_amount` returned None rather than guess. Right.
  - `extractor._is_plausible_money` would have dropped 15. Right.

And the record still says 15, because there are TWO extraction paths and only
one was guarded. `pipecat_engine._do_extraction` ended in
`self._gathered_context.update(extracted_data)` -- raw model output -- and that
is the path that writes the lead record and the client's webhook payload.

So every guard this project has built protected the CONVERSATION, while the
artefact the business is paid for took whatever the model said.

Fifteen rupees is not a monthly electricity bill. The reason it matters more
than being merely wrong: a null is visibly missing and a human follows it up,
while `15` looks like an answer and nobody ever looks again.
"""

from __future__ import annotations

from api.services.vaani import amounts
from api.services.vaani.lead_record import sanitise


def test_the_run_853_figure_is_dropped():
    safe, dropped = sanitise({"monthly_bill": 15})
    assert "monthly_bill" not in safe, "15 rupees was stored as a monthly bill"
    assert dropped and "credible" in dropped[0]


def test_a_real_bill_is_kept_untouched():
    safe, _ = sanitise({"monthly_bill": 15000})
    assert safe["monthly_bill"] == 15000


def test_the_bounds_are_amounts_own_and_not_a_second_copy():
    """A restated bound is a bound that drifts. These must be the same numbers."""
    safe, _ = sanitise({"monthly_bill": amounts.MIN_PLAUSIBLE})
    assert safe["monthly_bill"] == amounts.MIN_PLAUSIBLE
    safe, _ = sanitise({"monthly_bill": amounts.MIN_PLAUSIBLE - 1})
    assert "monthly_bill" not in safe
    safe, _ = sanitise({"monthly_bill": amounts.MAX_PLAUSIBLE + 1})
    assert "monthly_bill" not in safe


def test_a_phrase_amount_is_read_not_rejected():
    """"one lakh" is a real answer; only a bare implausible figure is dropped."""
    safe, _ = sanitise({"monthly_bill": "ఒక లక్ష"})
    assert "monthly_bill" in safe


def test_a_hesitation_is_stripped_from_a_name():
    """Run 314: a caller was addressed as "Mr. Um Bhaskar" for a whole call."""
    safe, dropped = sanitise({"customer_name": "ఉమ్ భాస్కర్"})
    assert safe["customer_name"] == "భాస్కర్"
    assert dropped


def test_a_clean_name_is_left_alone():
    safe, dropped = sanitise({"customer_name": "కృష్ణ"})
    assert safe["customer_name"] == "కృష్ణ"
    assert not dropped


def test_control_signals_pass_through_untouched():
    """These are the brain's, not customer facts. Filtering them would break
    disqualification and call ending."""
    payload = {"disqualified": True, "must_end": True, "end_reason": "rented",
               "buying_signal": False, "summary": "x", "lead_score": "100"}
    safe, dropped = sanitise(dict(payload))
    assert safe == payload
    assert not dropped


def test_nulls_survive_as_nulls():
    """A missing answer must stay visibly missing, not vanish from the record."""
    safe, _ = sanitise({"location": None, "customer_name": "",
                        "roof_available": "unknown"})
    assert safe["location"] is None
    assert safe["customer_name"] == ""
    assert safe["roof_available"] == "unknown"


def test_a_non_dict_cannot_poison_the_record():
    safe, dropped = sanitise(["not", "a", "dict"])
    assert safe == {}
    assert dropped


def test_unknown_fields_are_never_invented_or_altered():
    """The guard only ever subtracts. It must not add or rewrite."""
    payload = {"property_type": "apartment", "location": "Anandpur",
               "roof_available": False, "assessment_agreed": True}
    safe, dropped = sanitise(dict(payload))
    assert safe == payload
    assert not dropped
