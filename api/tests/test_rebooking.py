"""Run 300's last forty seconds, and the hour the vendor would have missed.

    AGENT   ఎల్లుండి సాయంత్రం four oclockకి షెడ్యూల్ చేసాం. ధన్యవాదాలు...
    CALLER  రేపు ఏమైనా ఉన్నాయా స్లాట్స్?        any slots tomorrow?
    AGENT   ఎల్లుండి సాయంత్రం four oclockకి... (identical)
    CALLER  రేపు స్టార్ట్స్ ఉన్నాయా ఏమన్నావ్     what did you say?
    AGENT   ...(identical)
    CALLER  కాదు కాదు రేపు ఉన్నాయా. రేపు. రేపు ఉన్నాయా?
    AGENT   ...(identical)

Four times, until he stopped. And the slot he had accepted -- repeating the
agent's own "సాయంత్రం four oclock" back at it -- was stored as 17:00.
"""

from datetime import datetime, timedelta

from api.services.vaani import booking
from api.services.vaani.booking import IST, parse_slot
from api.services.vaani.state import CallState

NOW = datetime(2026, 8, 29, 14, 20, tzinfo=IST)


def booked_state() -> CallState:
    s = CallState()
    s.required_fields = ["assessment_agreed"]
    s.questions = {"assessment_agreed": "ఎప్పుడు కుదురుతుంది?"}
    s.appointment_iso = datetime(2026, 8, 31, 16, 0, tzinfo=IST).isoformat()
    return s


# --- the hour that was silently wrong --------------------------------------

def test_the_agents_own_words_read_back_book_the_time_it_said():
    """`Slot.say()` renders 16:00 as "సాయంత్రం four oclock". The caller repeated
    that phrase; Sarvam wrote the English "four" in Telugu script; nothing
    matched it, so the hour fell back to the generic సాయంత్రం = 17:00."""
    assert parse_slot("ఎల్లుండి సాయంత్రం ఫోర్ ఓ క్లాక్.", NOW).hour == 16


def test_morning_was_hiding_it():
    """ఉదయం defaults to 10 and callers pick "ten", so the fallback happened to
    be right and the gap only ever bit in the evening."""
    assert parse_slot("రేపు ఉదయం టెన్ ఓ క్లాక్", NOW).hour == 10
    assert parse_slot("రేపు మధ్యాహ్నం టు", NOW).hour == 14


def test_every_spoken_hour_round_trips_through_what_the_agent_says():
    """Whatever the agent can say, the parser must be able to read back."""
    for hour in (10, 14, 16):
        when = datetime(2026, 8, 31, hour, 0, tzinfo=IST)
        spoken = booking.Slot(when).say(now=NOW)
        assert parse_slot(spoken, NOW) == when, spoken


# --- booked is not deaf ----------------------------------------------------

def test_asking_about_another_day_reopens_instead_of_repeating():
    s = booked_state()
    assert s.note_reschedule("రేపు ఏమైనా ఉన్నాయా స్లాట్స్?")
    assert not s.appointment_iso, "the slot is reopened so it can be re-offered"


def test_asking_does_not_silently_pick_a_time_for_them():
    """He asked WHETHER tomorrow was possible. That is not choosing 10 a.m."""
    s = booked_state()
    s.note_reschedule("రేపు ఏమైనా ఉన్నాయా స్లాట్స్?")
    assert s.appointment_iso == "", "asking must never become a booking"
    assert s.offered == ()


def test_naming_a_different_time_moves_the_appointment():
    s = booked_state()
    assert s.note_reschedule("రేపు ఉదయం టెన్ ఓ క్లాక్ చేయండి")
    assert datetime.fromisoformat(s.appointment_iso).hour == 10


def test_the_reply_is_told_to_confirm_the_new_time():
    s = booked_state()
    s.note_reschedule("రేపు ఉదయం టెన్ ఓ క్లాక్ చేయండి")
    assert "THEY MOVED IT" in s.render()


def test_a_question_after_booking_is_answered_not_repeated():
    s = booked_state()
    s.last_user_text = "సబ్సిడీ ఎంత వస్తుంది?"
    assert "answer THAT first" in s.render()


def test_a_question_with_no_time_in_it_leaves_the_booking_alone():
    """Reopening on any question at all would lose confirmed appointments."""
    s = booked_state()
    assert not s.note_reschedule("సబ్సిడీ ఎంత వస్తుంది?")
    assert s.appointment_iso


def test_a_bare_later_time_still_cannot_move_it_silently():
    """Run 266's guard. "మూడు లక్షలు" must never become three o'clock."""
    s = booked_state()
    assert not s.note_reschedule("మూడు లక్షలు")
    assert datetime.fromisoformat(s.appointment_iso).hour == 16


def test_repeating_the_same_time_back_changes_nothing():
    s = booked_state()
    assert not s.note_reschedule("ఎల్లుండి సాయంత్రం ఫోర్ ఓ క్లాక్")
    assert datetime.fromisoformat(s.appointment_iso).hour == 16


def test_nothing_happens_when_no_appointment_exists_yet():
    s = CallState()
    assert not s.note_reschedule("రేపు ఉన్నాయా?")


def test_a_day_mentioned_in_passing_does_not_move_the_visit():
    """"ఎల్లుండి మా వాళ్ళు ఊరికి వెళ్తున్నారు" names a day and asks for nothing.

    Run 266's bug wearing a different hat: a value that happened to appear in
    the sentence, taken as an instruction nobody gave.
    """
    s = booked_state()
    assert not s.note_reschedule("ఎల్లుండి మా వాళ్ళు ఊరికి వెళ్తున్నారు")
    assert datetime.fromisoformat(s.appointment_iso).hour == 16


def test_consent_is_still_not_a_time():
    """"ఆ బాగుంటుంది, ఓకే" parsed as two o'clock once the transliterated
    numerals went in, because a substring search for "టు" matches inside
    "ఉంటుంది". Tokens, not substrings."""
    assert parse_slot("ఆ బాగుంటుంది, ఓకే", NOW) is None


def test_an_amount_in_transliteration_is_not_a_clock():
    """"ఫైవ్ లాక్స్" has no లక్ష in it, so the substring MONEY guard missed it."""
    assert parse_slot("ఫైవ్ లాక్స్", NOW) is None
    assert parse_slot("టెన్ టు ట్వంటీ లాక్స్", NOW) is None
