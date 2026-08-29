"""The caller taking back something he already said.

The case
--------
    AGENT   మీ నెలవారీ బిల్లు ఎంత?
    CALLER  పది లక్షలు
    ...
    CALLER  సారీ, పది కాదు -- ఇరవై లక్షలు అనుకుంటా

A person corrects himself constantly, and the correction is usually a beat or
two after the mistake. Vaani used to drop the second figure on the floor: once
a field is in `known`, `note_amount` refuses to overwrite it, and it refuses
for a good reason -- run 266 read "మూడు లక్షలు" (an answer about money) as three
o'clock and booked an appointment nobody had agreed to. A field that anything
can overwrite is a field that gets overwritten by accident.

So the answer is not to open the gate; it is to require the caller to say he is
correcting himself. He always does -- that is what makes it a correction rather
than a new fact. Marking the revision explicitly keeps the accident closed and
lets the deliberate case through.

Why a class of words and not a phrase list
------------------------------------------
Three grammatical moves cover essentially every self-repair in these calls:

    negation of the prior value   "పది కాదు", "not ten", "అది కాదు"
    an apology opening a repair   "సారీ", "sorry", "క్షమించండి"
    an adversative correction     "actually", "నిజానికి", "కాదు కాదు", "wrong"

None of them names a value, so none of them is tied to solar, to bills, or to
the numbers one caller happened to say.

Cost of being wrong
-------------------
A false positive re-opens a field the caller is talking about anyway, and the
new value still has to parse and still has to be plausible before it is stored.
A false negative is the thing Vishnu described: telling the agent the real
figure and watching it keep the wrong one. The second is far worse, so the
markers below are read generously.
"""

from __future__ import annotations

# Any of these, anywhere in the utterance, marks it as a repair of something
# already said. Substring matching, not tokens: Telugu agglutinates, so "కాదు"
# arrives attached to whatever it negates as often as it arrives alone.
MARKERS = (
    # negation
    "కాదు", "కాదండి", "కాద", "లేదు కాదు", "అది కాదు", "వద్దు",
    # apology-led repair
    "సారీ", "క్షమించండి", "sorry",
    # explicit correction
    "నిజానికి", "అసలు కాదు", "తప్పు", "మార్చండి", "మార్చు", "సరిచేయ",
    "కరెక్షన్", "కరెక్ట్ చేయ", "రాంగ్",
    # English, which these callers mix in constantly
    "actually", "not ten", "no no", "i mean", "i meant", "instead",
    "correction", "correct it", "change it", "my mistake", "wrong",
)

# "కాదు" also does ordinary work -- "నాకు అది కాదు కావాలి" is not a repair of a
# stored value, and neither is a flat refusal. A marker only counts as a
# correction when the caller ALSO supplies something to correct it to, which is
# checked by the caller of this function (the new value must parse). This
# module answers one question only: is he taking something back.


def is_correction(text: str) -> bool:
    """True when the caller is revising something he said earlier."""
    low = (text or "").lower()
    return any(m in low for m in MARKERS)
