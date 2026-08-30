"""The shared layers must serve every client, not just the one in front of us.

Layers 1, 2 and 4 are inherited unchanged by every agent Vaani ever runs. Layer 3
is the only per-client text. That separation is the whole reason a new client is
a knowledge base rather than a rewrite -- and it decays silently, because the
easiest place to fix a solar call is the solar-flavoured example already sitting
in the persona.

It had already decayed. Layer 1 carried "సౌర శక్తి → సోలార్",
"విద్యుత్ బిల్లు → కరెంట్ బిల్లు", and a sentence naming "subsidy, panel, meter,
battery" as the words to say in English. All correct for MB Solar Hub. All dead
weight in a jewellery agent's cached prefix, and all of it inviting the model to
talk about panels to a caller buying gold.

The register RULES are universal and stay. The industry's words come from Layer
3's "Words real customers use for this", which the client's own knowledge base
fills in.
"""

import re
from pathlib import Path

import pytest

FIGURE = re.compile(r"₹\s*[\d,]{3,}"
                    r"|\d{4,}\s*(?:rupees|रूपयों)")

LAYERS = Path(__file__).resolve().parents[1] / "services" / "vaani" / "layers"

SHARED = [
    "01_persona/te-IN.md",
    "02_psychology/core.md",
    "04_mission/outbound.md",
]

# Words that belong to ONE industry. Not a blacklist of English -- "ఫోన్",
# "బ్యాంక్", "టైమ్" are universal register examples and must stay.
INDUSTRY_WORDS = [
    # solar, the client that has been in front of us all month
    "సోలార్", "సౌర", "solar", "ప్యానెల్", "panel", "సబ్సిడీ", "subsidy",
    "విద్యుత్", "కరెంట్ బిల్లు", "kilowatt", "net metering", "నెట్ మీటరింగ్",
    # the other verticals this platform already runs
    "బంగారం", "jewellery", "gold", "insurance", "బీమా", "మ్యూచువల్ ఫండ్",
    "mutual fund", "loan emi",
]

CLIENT_NAMES = ["MB Solar", "mbsolarhub", "BS Wealth", "బీఎస్"]


@pytest.mark.parametrize("name", SHARED)
def test_no_industry_vocabulary(name):
    text = (LAYERS / name).read_text(encoding="utf-8")
    low = text.lower()
    found = [w for w in INDUSTRY_WORDS
             if (w.lower() in low if w.isascii() else w in text)]
    assert not found, (
        f"{name} carries industry vocabulary {found}. The register RULE belongs "
        f"here; the industry's WORDS belong in Layer 3's 'Words real customers "
        f"use for this', which the client knowledge base fills in.")


@pytest.mark.parametrize("name", SHARED)
def test_no_client_name(name):
    text = (LAYERS / name).read_text(encoding="utf-8")
    found = [c for c in CLIENT_NAMES if c.lower() in text.lower()]
    assert not found, f"{name} names a specific client: {found}"


@pytest.mark.parametrize("name", SHARED)
def test_no_rupee_figures(name):
    """A concrete price or subsidy amount is a Layer 3 fact by definition.

    One in a shared layer would be quoted to every client's customers, and the
    invented-quantity guard would then treat it as legal for all of them.
    """
    text = (LAYERS / name).read_text(encoding="utf-8")
    # A figure inside a code span is a FORMATTING example, not a claim -- Layer
    # 1 writes "never as digits ... not `3000` or `Rs 3,000`", and that sentence
    # wraps, so the disclaimer sits on the previous line. The backticks are the
    # reliable signal; a price the agent might actually say is never in them.
    text = re.sub(r"`[^`]*`", "", text)
    figures = []
    for line in text.splitlines():
        # A figure shown as an example of the WRONG format is not a price
        # claim. Layer 1 says amounts are written as English words, "not
        # `3000` or `Rs 3,000`" -- that figure is the thing being forbidden.
        if re.search(r"not|never|Wrong", line, re.IGNORECASE):
            continue
        figures += re.findall(FIGURE, line)
    assert not figures, f"{name} states a concrete amount: {figures}"


# --- the one convention the layers used to disagree about --------------------

def test_the_clock_convention_is_stated_once():
    """A clock time and a duration are different words, and the persona has to
    say which is which -- three conventions were live at once and the agent
    flipped between them (runs 300, 314, 317, 320)."""
    persona = (LAYERS / "01_persona/te-IN.md").read_text(encoding="utf-8")
    flat = " ".join(persona.split())
    assert "o'clock" in flat, "the clock form must be shown"
    assert "గంటలు" in flat, "the duration form must be shown"
    assert "must not be half of each" in flat


def test_layer_three_is_where_vocabulary_lives():
    """The persona must point at Layer 3 rather than carrying the words itself,
    or the next client's build starts by deleting solar examples again."""
    persona = (LAYERS / "01_persona/te-IN.md").read_text(encoding="utf-8")
    # Whitespace-normalised: the pointer is a prose sentence and wraps.
    flat = " ".join(persona.split())
    assert "Layer 3" in flat
    assert "Words real customers use for this" in flat
