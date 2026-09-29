"""TurnSense-te: is the caller finished, judged from the words Soniox just gave us?

The decision this serves
------------------------
Production ends a turn on a 0.7 s timer after Silero's 0.2 s stop: 0.90 s after
the last word on every turn, fast or slow, finished or not (23 Sep, wf2: 0.9011,
0.9029, 0.902 ...). Replayed over 145 real calls on production's own VAD, that
timer cuts callers off on 7.5% of pauses, and the Telugu assist racing it takes
that to 9.5% without moving the median at all (tools/turnsense/replay_eval.py).

A timer cannot do better, because it cannot tell these apart:

    agent  "మీ కరెంట్ బిల్లు నెలకి ఎంత వస్తుంది?"
    caller "మాది."                      <- 0.2 s breath, the amount is coming
    caller "2 లక్షలు వస్తున్నాయి."      <- finished

The words can. Soniox finalises on every Silero stop and hands over the text of
the pause about 0.25 s later -- the moment the old analyzer mostly had NO text
for (31% fresh, Sep 7), and the reason every earlier text rule under-fired.

This module is the model half: features and a probability. `turnsense_turn.py`
is the analyzer that turns the probability into a wait. They are split so the
trainer (`bswealthfinance/tools/turnsense/train.py`) imports exactly this code
and a feature can never differ between training and a live call.

Inference is a sparse dot product in pure Python + numpy: no sklearn, no torch,
well under a millisecond, so it adds nothing a caller could hear.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path

from api.services.vaani.completeness import (
    CONNECTIVES,
    DANGLING_POSTPOSITIONS,
    HESITATIONS,
    sounds_unfinished,
)

try:  # amounts is part of the package; kept soft so the trainer can run alone
    from api.services.vaani.amounts import NUMERALS, RANGE_TOKENS, SCALES
except Exception:  # pragma: no cover
    NUMERALS, RANGE_TOKENS, SCALES = set(), set(), set()

MODEL_PATH = Path(__file__).parent / "models" / "turnsense_te.json"

_PUNCT = "?.!,;:।…—-\"'()[] \t\n"
_DIGIT = re.compile(r"\d")

# What an agent line is asking for. A one-word answer to "your name?" is
# finished; the same one word after "which city and which area?" may not be.
_ASK = {
    "ask_name": ("పేరు", "name"),
    "ask_amount": ("ఎంత", "amount", "బిల్లు", "budget", "బడ్జెట్", "income", "ఆదాయం"),
    "ask_place": ("ఎక్కడ", "ఏ ఊరు", "సిటీ", "ఏరియా", "city", "area", "location", "లొకేషన్"),
    "ask_time": ("ఎప్పుడు", "టైం", "సమయం", "time", "రేపు", "ఎల్లుండి"),
    "ask_choice": ("లేదా", " or ", "ఏది", "ఏ రకం", "ఏమిటి", "ఏంటి"),
    "ask_yesno": ("ఉందా", "కదా", "చేయవచ్చా", "మాట్లాడవచ్చా", "ఆ?", "కావాలా", "సరేనా"),
}

# Re-prompts a caller makes when he is WAITING on us. They are finished turns
# with a question in them, whatever their length.
REPROMPTS = {"హలో", "hello", "హలో అండి", "వినపడుతుందా", "ఉన్నారా", "ఏమన్నారు",
             "ఏంటి", "అర్థం కాలేదు", "మళ్ళీ చెప్పండి", "హా", "ఆ"}


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", (text or "").strip())


def _tokens(text: str) -> list[str]:
    return [t for t in (w.strip(_PUNCT) for w in _norm(text).split()) if t]


def _end_punct(text: str) -> str:
    t = _norm(text).rstrip()
    if not t:
        return "none"
    if t.endswith(("...", "…", "—", "-")):
        return "trail"
    ch = t[-1]
    return {"?": "q", ".": "stop", "।": "stop", "!": "stop", ",": "comma"}.get(ch, "none")


# Prior for a caller we have not heard pause yet: ~15% of pauses on real calls
# are followed by him carrying on (pauses.jsonl, 29 Sep).
HOLD_PRIOR = 0.15


def hold_rate(holds: int, pauses: int) -> float:
    """This caller's own habit so far, shrunk toward the prior."""
    return (holds + 3 * HOLD_PRIOR) / (pauses + 3)


def dense_features(agent: str, turn: str, last: str,
                   seg_secs: float, n_segs: int,
                   caller_hold_rate: float = HOLD_PRIOR,
                   caller_pauses: int = 0) -> dict[str, float]:
    """Hand-built, interpretable features. Every one is a grammatical CLASS,
    except the last two, which are THIS caller's own pausing habit so far --
    known online, from pauses whose outcome has already happened."""
    toks = _tokens(turn)
    last_toks = _tokens(last)
    low = [t.lower() for t in toks]
    lw = low[-1] if low else ""
    f: dict[str, float] = {}
    f["bias"] = 1.0
    f["n_words"] = math.log1p(len(toks))
    f["n_words_last"] = math.log1p(len(last_toks))
    f["n_chars_last"] = math.log1p(len(_norm(last)))
    f["seg_secs"] = min(6.0, max(0.0, seg_secs))
    f["n_segs"] = math.log1p(max(0, n_segs))
    p = _end_punct(last)
    for k in ("q", "stop", "comma", "trail", "none"):
        f[f"punct_{k}"] = 1.0 if p == k else 0.0
    f["unfinished_rule"] = 1.0 if sounds_unfinished(last) else 0.0
    f["unfinished_rule_turn"] = 1.0 if sounds_unfinished(turn) else 0.0
    f["last_hes"] = 1.0 if lw in HESITATIONS else 0.0
    f["all_hes"] = 1.0 if low and all(t in HESITATIONS for t in low) else 0.0
    f["last_conn"] = 1.0 if lw in CONNECTIVES else 0.0
    f["last_postp"] = 1.0 if lw in DANGLING_POSTPOSITIONS else 0.0
    f["last_range"] = 1.0 if lw in RANGE_TOKENS else 0.0
    f["last_num"] = 1.0 if (_DIGIT.search(lw) or lw in NUMERALS) and lw not in SCALES else 0.0
    f["last_scale"] = 1.0 if lw in SCALES else 0.0
    f["reprompt"] = 1.0 if " ".join(low) in REPROMPTS else 0.0
    f["latin_last"] = 1.0 if lw and all(ord(c) < 128 for c in lw) else 0.0
    a = _norm(agent).lower()
    f["agent_q"] = 1.0 if a.rstrip().endswith("?") else 0.0
    f["agent_none"] = 1.0 if not a else 0.0
    for k, keys in _ASK.items():
        f[k] = 1.0 if any(x in a for x in keys) else 0.0
    f["caller_hold_rate"] = float(caller_hold_rate)
    f["caller_pauses"] = math.log1p(max(0, caller_pauses))
    return f


def ngram_keys(agent: str, turn: str, last: str) -> list[str]:
    """Sparse keys: character n-grams of the TAIL, where completion lives.

    Telugu is verb-final, and whether a clause is finished is written in the
    last few characters: a finite ending (-ను, -ాను, -ుంది, -ారు) against a
    non-finite one (-ి conjunctive, -తే conditional, -కి dative, -లో locative).
    The tail of the caller's text, with an end marker, captures that without a
    word list. The agent's last few words are added as context keys.
    """
    keys: list[str] = []
    t = "⟨" + _norm(turn)[-28:] + "⟩"
    for n in (2, 3, 4, 5):
        for i in range(max(0, len(t) - 14), len(t) - n + 1):
            keys.append(f"c{n}:{t[i:i + n]}")
    toks = _tokens(turn)
    if toks:
        keys.append("w1:" + toks[-1].lower())
        if len(toks) > 1:
            keys.append("w2:" + " ".join(toks[-2:]).lower())
        if len(toks) == 1:
            keys.append("only:" + toks[0].lower())
    at = _tokens(agent)
    for w in at[-3:]:
        keys.append("a:" + w.lower())
    return keys


class TurnSenseModel:
    """Logistic regression over dense + hashed-free sparse keys, from JSON."""

    def __init__(self, blob: dict):
        self.version = blob.get("version", "?")
        self.dense_names: list[str] = blob["dense_names"]
        self.mean = blob["dense_mean"]
        self.scale = blob["dense_scale"]
        self.w_dense = blob["w_dense"]
        self.w_sparse: dict[str, float] = blob["w_sparse"]
        self.intercept = float(blob["intercept"])
        self.meta = blob.get("meta", {})
        # Optional stacked text scorer, trained on grammar labels (teacher.jsonl).
        # Its logit enters the behavioural model as the dense feature
        # "text_score", which learned from real pauses how far to trust it.
        tm = blob.get("text_model") or {}
        self.text_w: dict[str, float] = tm.get("w_sparse", {})
        self.text_b = float(tm.get("intercept", 0.0))

    def text_score(self, agent: str, turn: str, last: str) -> float:
        if not self.text_w:
            return 0.0
        z = self.text_b
        # set(): training saw each key once (binary features); a repeated n-gram
        # must not be counted twice here. Caught by train.py's parity check.
        for k in set(ngram_keys(agent, turn, last)):
            z += self.text_w.get(k, 0.0)
        return max(-8.0, min(8.0, z))

    @classmethod
    def load(cls, path: Path | str = MODEL_PATH) -> "TurnSenseModel | None":
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None

    def logit(self, agent: str, turn: str, last: str,
              seg_secs: float = 1.0, n_segs: int = 1,
              caller_hold_rate: float = HOLD_PRIOR, caller_pauses: int = 0) -> float:
        d = dense_features(agent, turn, last, seg_secs, n_segs,
                           caller_hold_rate, caller_pauses)
        if self.text_w:
            d["text_score"] = self.text_score(agent, turn, last)
        z = self.intercept
        for name, m, s, w in zip(self.dense_names, self.mean, self.scale, self.w_dense):
            z += w * ((d.get(name, 0.0) - m) / s)
        ws = self.w_sparse
        for k in set(ngram_keys(agent, turn, last)):      # binary, as trained
            z += ws.get(k, 0.0)
        return z

    def probability(self, agent: str, turn: str, last: str,
                    seg_secs: float = 1.0, n_segs: int = 1,
                    caller_hold_rate: float = HOLD_PRIOR,
                    caller_pauses: int = 0) -> float:
        """P(the caller has finished his turn)."""
        z = self.logit(agent, turn, last, seg_secs, n_segs,
                       caller_hold_rate, caller_pauses)
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
