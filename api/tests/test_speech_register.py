"""Run 300, asked how many hours solar panels work:

    సౌర ప్యానెల్స్ రోజుకు సుమారు eight నుండి ten గంటల వరకు శక్తి ఉత్పత్తి
    చేస్తాయి, మేఘావృత రోజుల్లో కూడా కొంచెం తక్కువగా పనిచేస్తాయి.

Every content word is Sanskrit-derived literary Telugu. Nobody down a phone
line says సౌర, ఉత్పత్తి or మేఘావృత. The sentence is grammatically perfect and
socially wrong, which is worse than a small mistake: it is the register of a
government notice, and it tells the customer in one word that they are not
talking to a salesperson.

Layer 1 has carried the rule, with a worked table, since the persona was
written -- and the same reply said ధన్యవాదాలు, which that table names by name.
So it is enforced after generation instead of requested before it.
"""

import pytest

from api.services.vaani.reply_sanitizer import ReplySanitizer
from api.services.vaani.speech_register import spoken


def test_the_run_300_sentence_comes_out_in_the_spoken_register():
    said = spoken("సౌర ప్యానెల్స్ రోజుకు సుమారు eight నుండి ten గంటల వరకు "
                  "శక్తి ఉత్పత్తి చేస్తాయి, మేఘావృత రోజుల్లో కూడా తక్కువగా పనిచేస్తాయి.")
    assert "సోలార్ ప్యానెల్స్" in said
    assert "eight to ten గంటలు" in said
    assert "మబ్బులు ఉన్న" in said
    for literary in ("సౌర", "ఉత్పత్తి", "మేఘావృత", "నుండి", "వరకు"):
        assert literary not in said


@pytest.mark.parametrize("literary,spoken_form", [
    ("సౌర శక్తి త్వరగా లాభం ఇస్తుంది", "సోలార్"),
    ("మీ విద్యుత్ బిల్లు ఎంత?", "కరెంట్ బిల్లు"),
    ("ధన్యవాదాలు సార్", "థాంక్యూ"),
    ("మీరు అందుబాటులో ఉంటారా?", "కుదురుతుందా"),
])
def test_each_register_slip_is_corrected(literary, spoken_form):
    assert spoken_form in spoken(literary)


# --- what must NOT be touched ---------------------------------------------

def test_an_ordinary_from_is_not_a_range():
    """నుండి is a perfectly good word. Only the range FRAME is rewritten."""
    assert spoken("మీరు Hyderabad నుండి ఉంటున్నారా?") == "మీరు Hyderabad నుండి ఉంటున్నారా?"


def test_text_with_nothing_to_fix_is_returned_unchanged():
    line = "మంచిది సార్, మీ పేరు చెప్పగలరా?"
    assert spoken(line) == line


def test_empty_text_is_safe():
    assert spoken("") == ""


def test_the_longer_phrase_wins_over_its_own_prefix():
    """సౌర ప్యానెల్స్ must not become సోలార్ ప్యానెల్స్ by two separate passes."""
    assert spoken("సౌర ప్యానెల్స్") == "సోలార్ ప్యానెల్స్"


# --- it reaches actual speech ---------------------------------------------

def test_the_sanitizer_speaks_the_corrected_register():
    s = ReplySanitizer()
    out = s.feed("సౌర శక్తి మంచిది.") + s.finish()
    assert "సోలార్" in out
    assert "సౌర" not in out


def test_a_phrase_split_across_stream_fragments_is_still_corrected():
    """The reason the rewrite runs on the buffer rather than on each chunk."""
    s = ReplySanitizer()
    out = "".join(s.feed(part) for part in ("సౌర ", "శక్తి ", "చాలా మంచిది.")) + s.finish()
    assert "సోలార్" in out
    assert "సౌర శక్తి" not in out
