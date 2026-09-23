"""The client's own figures must not be blocked as invented.

`ReplyFilter.known_numbers` exists so that "a new knowledge base defines its own
legal numbers just by containing them" (brain_processor.py:104-106). That
invariant broke when Layer 3 was split: `client_reference.split(prompt)[0]` is
compiled into the system prompt and the half BELOW `## Reference` is consulted
per turn instead — so the knowledge base is no longer in the text
`numbers_in()` reads.

Measured on the live MB Solar prompt, 23 Sep:

    known_numbers TODAY   ['1','2','3','4','5','6','one','six','three','two',
                           'ఒక','రెండు','వెయ్యి','వేలు']
    known_numbers if full [... 'thirty','thousand','eighteen','seventy',
                           'eight','four','hundred','twenty','five' ...]

    subsidy  answer -> no_price_quote, no_invented_quantity   BLOCKED
    units    answer -> no_price_quote, no_invented_quantity   BLOCKED
    warranty answer -> no_invented_quantity                   BLOCKED

`no_invented_quantity` is in BLOCKING_RULES, so the reply is replaced with
SAFE_FALLBACK -- "సార్, కరెక్ట్ ఫిగర్ ఇప్పుడే చెప్పలేను" -- in answer to the
three most-asked questions on a solar sales call. It is intermittent rather than
constant only because `_gate` substitutes only while nothing has been spoken
yet, and HOLDBACK=24 usually releases the first chunk before the number lands.
A large first frame from the LLM is enough to make it fire.

The PM Surya Ghar figures are not inventions. They are in the file the client
wrote. They have to be on the whitelist.
"""

from api.services.vaani import guardrails

REFERENCE_HALF = """## Reference

**Is the subsidy real?**
TRIGGER: సబ్సిడ|subsid
"PM Surya Ghar స్కీమ్ లో మొదటి two kW కి thirty thousand rupees, ఆ తర్వాత kW కి
eighteen thousand rupees, మొత్తం seventy eight thousand rupees వరకు వస్తుంది అండి."

**How many units?**
TRIGGER: యూనిట్
"మంచి సైజ్ సిస్టమ్ కి నెలకి సుమారు four hundred units వస్తాయి అండి."

**Warranty?**
TRIGGER: వారంట
"ప్యానెల్స్ twenty five years కంటే ఎక్కువ వస్తాయి అండి."
"""

COMPILED_HALF = """## OUTPUT CONTRACT
- Never invent a number. Not one.
Ask about ఒక or రెండు things.
"""

SUBSIDY = ("PM Surya Ghar స్కీమ్ లో మొదటి two kW కి thirty thousand rupees, "
           "ఆ తర్వాత kW కి eighteen thousand rupees, మొత్తం seventy eight "
           "thousand rupees వరకు వస్తుంది అండి.")
UNITS = "మంచి సైజ్ సిస్టమ్ కి నెలకి సుమారు four hundred units వస్తాయి అండి."
WARRANTY = "ప్యానెల్స్ twenty five years కంటే ఎక్కువ వస్తాయి అండి."


def _blocking(line, known):
    return {v.rule for v in guardrails.blocking(
        guardrails.check(line, known_numbers=known))}


def test_the_compiled_half_alone_blocks_the_clients_own_figures():
    """The defect, stated as a test so it cannot come back silently."""
    known = guardrails.numbers_in(COMPILED_HALF)
    assert "no_invented_quantity" in _blocking(SUBSIDY, known)
    assert "no_invented_quantity" in _blocking(WARRANTY, known)


def test_the_reference_half_makes_them_legal():
    known = guardrails.numbers_in(COMPILED_HALF + REFERENCE_HALF)
    assert "no_invented_quantity" not in _blocking(SUBSIDY, known), (
        "the subsidy figures are written in the client's own file")
    assert "no_invented_quantity" not in _blocking(UNITS, known)
    assert _blocking(WARRANTY, known) == set(), (
        "twenty five years carries no price and is simply a fact from the file")


def test_the_brain_whitelists_both_halves():
    """The function the brain actually calls, not a re-implementation."""
    from api.services.vaani.brain_processor import whitelist_numbers

    known = whitelist_numbers(COMPILED_HALF, REFERENCE_HALF)
    assert "thirty" in known and "thousand" in known
    assert "no_invented_quantity" not in _blocking(SUBSIDY, known)


def test_an_actually_invented_number_is_still_caught():
    """The rule must keep working -- this is a safety guard, not noise."""
    known = guardrails.numbers_in(COMPILED_HALF + REFERENCE_HALF)
    invented = "మీకు నెలకి ninety nine thousand rupees ఆదా అవుతుంది అండి."
    assert _blocking(invented, known), (
        "ninety nine thousand appears nowhere in the client's file")
