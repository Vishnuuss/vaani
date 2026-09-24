"""Run 1036 (24 Sep) said, out loud, down the phone:

    "assessment_agreed: ఉచితంగా ఒక సైట్ సర్వే చేయించుకుంటారా?"

The model copied a field id out of its own state block. The sanitizer already
strips MODE lines and truncates at role labels; a snake_case id followed by a
colon at the start of a sentence is the same kind of leak and is now removed.
It needs an underscore, so no ordinary word -- Telugu or English -- can match.
"""

from api.services.vaani.reply_sanitizer import ReplySanitizer


def _heard(text: str, step: int = 5) -> str:
    s = ReplySanitizer()
    out = ""
    for i in range(0, len(text), step):
        out += s.feed(text[i:i + step]) or ""
    return out + (s.finish() or "")


def test_run_1036_the_field_id_is_not_spoken():
    heard = _heard("assessment_agreed: ఉచితంగా ఒక సైట్ సర్వే చేయించుకుంటారా?")
    assert "assessment_agreed" not in heard
    assert "ఉచితంగా ఒక సైట్ సర్వే" in heard, "the question itself must survive"


def test_a_field_id_after_a_sentence_is_removed_too():
    heard = _heard("సరే అండి. customer_name: మీ పేరు చెప్పగలరా?")
    assert "customer_name" not in heard
    assert "మీ పేరు చెప్పగలరా" in heard


def test_ordinary_speech_with_a_colon_is_untouched():
    for line in ("సమయం: రేపు ఉదయం ten o'clock అండి.", "Note: ఇది ఉచితం అండి."):
        assert _heard(line).strip().startswith(line.split(":")[0]), line
