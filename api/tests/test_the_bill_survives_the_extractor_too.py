"""Run 897 (12 Sep): every field captured, and it asked for two of them five times.

    50:22  USER  టూ టూ సిక్స్టీ లాక్స్ వస్తుంది.
    ...
    51:15  BOT   నెలకి కరెంట్ బిల్లు ఎంత అవుతుంది అండి?     the bill, again
    51:22  BOT   మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?       location, again
    51:30  USER  ఇవన్నీ చెప్పాను కదా అప్పుడు. హలో.          I told you all this
    51:32  BOT   మీ కరెంట్ బిల్లు నెలకి                      the bill, a third time
    51:34  BOT   మీరు ఏ ఏరియా లేదా సిటీలో ఉంటున్నారు?       location, a third time

    gathered: monthly_bill 260000, location అనంతపురి, and all four others

The lead record had every field. The LIVE state did not, which is why they
stayed in STILL_NEED and kept being asked -- and why the guard that refuses to
re-ask a KNOWN field could not fire either: it reads `state.known`, and the
field was never there.

The plausibility ceiling was removed from `CallState.note_amount` on 11 Sep,
when the client's instruction was to believe what the caller says and never
delete it. There was a SECOND copy of the same gate on the asynchronous path,
in `apply_to_state`, and it was missed. "టూ టూ సిక్స్టీ లాక్స్" reads as 60
lakhs, over the old Rs 50 lakh ceiling, so the extractor dropped it on the
floor every single turn.

A half-applied policy is worse than either policy applied whole: the figure
reached the vendor's record, so it looked stored, while the agent behaved all
call as though it had never been said.
"""

from api.services.vaani import extractor
from api.services.vaani.state import CallState

FIELDS = ["monthly_bill", "location"]


def _state() -> CallState:
    return CallState(required_fields=FIELDS,
                     questions={"monthly_bill": "మీ కరెంట్ బిల్లు నెలకి ఎంత?",
                                "location": "మీరు ఏ ఊరు?"})


def test_run_897_a_sixty_lakh_bill_reaches_the_live_state():
    s = _state()
    extractor.apply_to_state(s, {"monthly_bill": 6_000_000}, FIELDS,
                             user_text="టూ టూ సిక్స్టీ లాక్స్ వస్తుంది")
    assert s.known.get("monthly_bill"), "the bill was dropped on the async path"
    assert "monthly_bill" not in s.still_need, (
        "it stayed on the checklist and will be asked again -- run 897")


def test_it_is_not_doubted_back_at_him():
    s = _state()
    extractor.apply_to_state(s, {"monthly_bill": 6_000_000}, FIELDS,
                             user_text="అరవై లక్షలు")
    assert s.doubted is None


def test_an_ordinary_bill_is_unaffected():
    s = _state()
    extractor.apply_to_state(s, {"monthly_bill": 70_000}, FIELDS,
                             user_text="డెబ్బై వేలు")
    assert s.known.get("monthly_bill") == "70000"


def test_the_two_paths_now_agree():
    """The synchronous and asynchronous paths must not disagree about a figure.

    They did, and that disagreement is the whole of run 897: `note_amount`
    believed him, `apply_to_state` did not, and which one ran decided whether
    the agent remembered what he had said.
    """
    sync, async_ = _state(), _state()
    sync.pending_ask = "monthly_bill"
    sync.note_amount("60 లక్షలు")
    extractor.apply_to_state(async_, {"monthly_bill": 6_000_000}, FIELDS,
                             user_text="60 లక్షలు")
    assert bool(sync.known.get("monthly_bill")) is bool(
        async_.known.get("monthly_bill"))
