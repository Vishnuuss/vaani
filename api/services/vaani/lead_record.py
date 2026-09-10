"""Guard the lead record, which is the only artefact the client ever reads.

The gap this closes
-------------------
There are TWO extraction paths in this system, and until now only one was
guarded.

  1. `api/services/vaani/extractor.py` -- Vaani's own. It drops an implausible
     bill (run 295), strips "um" out of a name (run 314), and refuses to record
     a NEGATIVE fact from the mere absence of a yes (run 312). Weeks of real
     defects are encoded in it.

  2. `pipecat_engine._do_extraction` -- Dograh's native variable extraction.
     It ends in `self._gathered_context.update(extracted_data)`: raw LLM output,
     no validation of any kind. **This is the one that writes the lead record**
     and the webhook payload the client receives.

So every guard Vaani has built protects the CONVERSATION, while the artefact the
business is actually paid for was taking whatever the model said.

Run 853 is the worked example. The caller's bill came through Sarvam as
"పది ఐదు గుంధల అండి" -- a garbled "పదిహేను వేలు", fifteen thousand.
`amounts.parse_amount` did exactly the right thing and returned None rather than
guess. Vaani's extractor would then have dropped the figure. The lead record
says `monthly_bill: 15`, because path 2 never asked either of them.

Fifteen rupees is not a monthly electricity bill. But it is worse than a null,
and that is the whole argument: a null is visibly missing and a human follows it
up, while `15` looks like an answer and nobody ever looks again.

Why this file, rather than a check inside pipecat_engine
--------------------------------------------------------
The rules already exist and are already tested. Re-implementing "is this a
plausible bill" beside the engine would create a second definition to drift
against the first, which is precisely the failure being fixed. So this reuses
`amounts`, `completeness` and `negation` and adds no new judgement of its own.

It is deliberately CONSERVATIVE. It drops values it can prove are wrong and
leaves everything else alone; it never invents, never rewrites a plausible
figure, and never turns a null into a value.
"""

from __future__ import annotations

from loguru import logger

from api.services.vaani import amounts, completeness
from api.services.vaani.state import _is_money_field

# Keys that are Vaani control signals, not customer facts. They are handled by
# the brain and must pass through untouched.
_CONTROL = {"objection", "disqualified", "disqualify_reason", "buying_signal",
            "next_step_agreed", "must_end", "end_reason", "summary",
            "lead_score", "call_disposition"}

_EMPTY = {None, "", "unknown", "not stated", "not available", "n/a"}


def _plausible_money(value) -> bool:
    """Reuses `amounts`' bounds rather than restating them."""
    try:
        rupees = float(str(value).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        # A phrase, not a bare figure -- "one lakh", "10-15k". `parse_amount`
        # reads those and applies the same bounds itself.
        parsed = amounts.parse_amount(str(value))
        return parsed is None or parsed.plausible
    return amounts.MIN_PLAUSIBLE <= rupees <= amounts.MAX_PLAUSIBLE


def _clean_name(value):
    """"um" is not part of anybody's name (run 314)."""
    if not isinstance(value, str):
        return value
    return completeness.strip_fillers(value).strip() or value


def sanitise(extracted: dict) -> tuple[dict, list[str]]:
    """Return (safe_values, reasons_dropped).

    Only ever removes. A caller who gave a real answer keeps it; a value the
    existing rules can prove is wrong is dropped so the field stays visibly
    empty instead of quietly wrong.
    """
    if not isinstance(extracted, dict):
        return {}, [f"extraction returned {type(extracted).__name__}, not a dict"]

    safe, dropped = {}, []
    for key, value in extracted.items():
        if key in _CONTROL or key == "extracted_variables":
            safe[key] = value
            continue
        if isinstance(value, str) and value.strip().lower() in _EMPTY:
            safe[key] = value
            continue
        if value in _EMPTY:
            safe[key] = value
            continue

        if _is_money_field(key) and not _plausible_money(value):
            dropped.append(
                f"{key}={value!r} is not a credible amount "
                f"({amounts.MIN_PLAUSIBLE}-{amounts.MAX_PLAUSIBLE}); "
                "storing null instead, because a wrong figure nobody rechecks "
                "is worse than a missing one")
            continue

        if "name" in key.lower():
            cleaned = _clean_name(value)
            if cleaned != value:
                dropped.append(f"{key}: stripped hesitation -> {cleaned!r}")
            safe[key] = cleaned
            continue

        safe[key] = value

    return safe, dropped


def sanitise_and_log(extracted: dict, *, node: str = "") -> dict:
    """`sanitise`, with every drop written to the log so it is never silent."""
    safe, dropped = sanitise(extracted)
    for reason in dropped:
        logger.warning(f"[lead-record{' ' + node if node else ''}] {reason}")
    return safe
