"""Run 887 (11 Sep): an industry caller said 70 lakhs and the figure was deleted.

    USER  ఇండస్ట్రీ.
    USER  70 లాక్స్
    ...
    lead record:  monthly_bill absent entirely

`MAX_PLAUSIBLE` is Rs 50 lakh a month. 70 lakhs is over it, so `note_amount`
recorded nothing and the most valuable kind of lead this business gets -- a
factory -- reached the vendor with no bill on it.

The ceiling was argued from rooftop capacity: above ~Rs 50 lakh/month the
caller is beyond what a rooftop array can serve, so confirming costs nothing.
That reasoning is sound and the client has overruled it anyway, on 11 Sep:
believe what he said, never delete it, and let sales sanity-check the number.
Being wrong here costs one odd row in a spreadsheet. Being wrong the other way
costs the lead, and runs 864 and 868 hung up over exactly this.

The mishearing the ceiling was built to catch -- "వేలు" (thousands) heard as
"లక్షలు" -- is real and is NOT solved by this. It is accepted, deliberately: a
figure that is recorded can be corrected later, and one that was never recorded
cannot.
"""

from api.services.vaani.state import CallState


def _state() -> CallState:
    return CallState(required_fields=["monthly_bill"],
                     questions={"monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత?"})


def test_run_887_an_industry_bill_of_seventy_lakhs_is_recorded():
    s = _state()
    s.pending_ask = "monthly_bill"
    assert s.note_amount("70 లాక్స్")
    assert s.known.get("monthly_bill") == str(7_000_000)


def test_he_is_never_told_his_own_bill_cannot_be_real():
    s = _state()
    s.pending_ask = "monthly_bill"
    s.note_amount("70 లాక్స్")
    assert s.doubted is None
    assert "cannot be a monthly" not in s.render()


def test_a_range_is_still_recorded_with_both_ends():
    """Runs 864 and 868: "50 to 60 lakhs", and the lead lost its number."""
    s = _state()
    s.pending_ask = "monthly_bill"
    assert s.note_amount("50 to 60 లక్షలు")
    assert s.known.get("monthly_bill")
    assert s.doubted is None


def test_an_ordinary_bill_is_unaffected():
    s = _state()
    s.pending_ask = "monthly_bill"
    assert s.note_amount("70,000")
    assert s.known.get("monthly_bill") == str(70_000)
