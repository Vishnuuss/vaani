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


# --- streaming safety ------------------------------------------------------

def test_a_word_is_not_rewritten_while_it_is_still_arriving():
    """The bug the colon test caught.

    Tokens arrive one character at a time. Mid-stream the buffer ends
    "...పది గంట", which looks like a bare singular and was being corrected to
    గంటలు -- and then "లకు" arrived and the caller heard "గంటలులకు".
    """
    text = "సమయం: రేపు ఉదయం పది గంటలకు."
    s = ReplySanitizer()
    out = "".join(s.feed(ch) for ch in text) + s.finish()
    assert out == text


def test_the_plural_is_still_fixed_when_the_word_really_is_finished():
    s = ReplySanitizer()
    out = "".join(s.feed(ch) for ch in "సాయంత్రం ఐదు గంట.") + s.finish()
    assert "ఐదు గంటలు" in out


def test_one_hour_keeps_the_singular():
    """ఒక గంట is correct Telugu; only counts above one take the plural."""
    assert spoken("మధ్యాహ్నం ఒక గంట") == "మధ్యాహ్నం ఒక గంట"


def test_a_name_takes_garu_not_andi():
    """అండి is a sentence-final particle and belongs after a verb. The particle
    that follows a NAME is గారు; run 305 said "విష్ణు అండి" four times."""
    assert spoken("మంచిది విష్ణు అండి, చెప్పండి", names=("విష్ణు",)) == \
        "మంచిది విష్ణు గారు, చెప్పండి"


def test_andi_after_an_ordinary_verb_is_untouched():
    assert spoken("ఆ చెప్పండి", names=("విష్ణు",)) == "ఆ చెప్పండి"


def test_no_name_known_yet_changes_nothing():
    line = "మంచిది అండి, చెప్పండి"
    assert spoken(line) == line


# --- run 320: "మంచిది విష్ణు అండి" ------------------------------------------
#
# The name-based rewrite was already here and did nothing, for a reason that is
# only visible in the timing: the name reaches `spoken()` from
# `state.known["customer_name"]`, which the ASYNCHRONOUS extractor fills one
# turn later. On the single turn where the agent first uses the name -- the one
# that matters -- there is no name to match.
#
#     USER : మా పేరు విష్ణు అండి
#     BOT  : మంచిది విష్ణు అండి, ఉచిత సైట్ అసెస్‌మెంట్ ...

def test_the_line_that_shipped_in_run_320():
    assert spoken("మంచిది విష్ణు అండి, ఉచిత సైట్ అసెస్‌మెంట్ కోసం") == (
        "మంచిది విష్ణు గారు, ఉచిత సైట్ అసెస్‌మెంట్ కోసం")


def test_it_works_with_no_name_supplied():
    """The whole point: this must not depend on extraction having landed."""
    assert "భాస్కర్ గారు" in spoken("థాంక్యూ భాస్కర్ అండి", names=())


@pytest.mark.parametrize("said,want", [
    ("మంచిది భాస్కర్ అండి", "మంచిది భాస్కర్ గారు"),
    ("సరే నితేష్ అండి, రేపు వస్తాం", "సరే నితేష్ గారు, రేపు వస్తాం"),
    ("అర్థమైంది సతీష్ అండి", "అర్థమైంది సతీష్ గారు"),
])
def test_the_vocative_positions(said, want):
    assert spoken(said) == want


def test_an_imperative_verb_is_not_a_name():
    """"సరే చెప్పండి అండి" is a verb being politely closed. Rewriting it to
    "చెప్పండి గారు" would be a new bug of exactly the same kind."""
    assert spoken("సరే చెప్పండి అండి") == "సరే చెప్పండి అండి"


@pytest.mark.parametrize("said", ["అవును సార్ అండి", "మంచిది మేడమ్ అండి"])
def test_honorifics_do_not_stack(said):
    """"సార్ గారు" is worse than the అండి it replaces -- it reads as servile."""
    assert spoken(said) == said


def test_ordinary_andi_is_untouched():
    assert spoken("ఉందండి") == "ఉందండి"
    assert spoken("మంచిది, మీ పేరు చెప్పగలరా?") == "మంచిది, మీ పేరు చెప్పగలరా?"


# --- The client's corrections, 4 Sep -----------------------------------------
#
# Two register defects, both about how a STRING OF CHARACTERS is read aloud
# rather than which words are chosen:
#
#   "see like . as dot not chukka"
#   "phone numbers should be read in english not telugu ... two at a time"
#
# చుక్క is the Telugu word for a dot, and it is the correct written word. It is
# not what anyone says when reading out an email address or a website: a Telugu
# speaker reading mbsolarhub@gmail.com says "gmail dot com", in English, every
# time. The same speaker reading a mobile number back to confirm it says the
# digits in English, in pairs, because that is how a number is checked.


@pytest.mark.parametrize("said,want", [
    ("gmail చుక్క com", "gmail dot com"),
    ("mbsolarhub చుక్క com అని", "mbsolarhub dot com అని"),
    ("www చుక్క mbsolarhub చుక్క com", "www dot mbsolarhub dot com"),
])
def test_chukka_is_said_as_dot(said, want):
    assert spoken(said) == want


@pytest.mark.parametrize("said,want", [
    ("mbsolarhub@gmail.com", "mbsolarhub@gmail dot com"),
    ("www.mbsolarhub.com", "www dot mbsolarhub dot com"),
])
def test_a_written_dot_inside_an_address_is_said(said, want):
    assert spoken(said) == want


def test_a_full_stop_is_not_turned_into_the_word_dot():
    """The rule is about addresses, not punctuation. A sentence-ending full
    stop must stay a full stop, or every reply ends with the word "dot"."""
    assert spoken("సరే. మంచిది.") == "సరే. మంచిది."


@pytest.mark.parametrize("said,want", [
    ("మీ నంబర్ 9133992799 కదా",
     "మీ నంబర్ nine one, three three, nine nine, two seven, nine nine కదా"),
    ("91339 92799",
     "nine one, three three, nine nine, two seven, nine nine"),
])
def test_a_mobile_number_is_read_in_english_two_at_a_time(said, want):
    assert spoken(said) == want


@pytest.mark.parametrize("amount", [
    "78,000 రూపాయలు",
    "30,000 రూపాయలు",
    "2 kW",
    "400 units",
    "520010",
])
def test_amounts_and_short_numbers_are_left_alone(amount):
    """Only a 10-digit mobile is a phone number. Turning ₹78,000 or a pincode
    into spelled-out English pairs would be a new bug, and a worse one."""
    assert spoken(amount) == amount
