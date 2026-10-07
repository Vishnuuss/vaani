"""Phase 0 benchmark runner.

    python -m vaani.bench.run                 # everything
    python -m vaani.bench.run --only groq     # one vendor
    python -m vaani.bench.run -n 30           # more samples

Nothing in the pipeline gets built around a vendor number until it appears here.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from . import cartesia_bench, llm_bench, net, sarvam_bench
from .report import Metric, Section, pct, render, save_json

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / ".tmp" / "bench"

SUITES = {
    "net": net.run,
    "llm": llm_bench.run,
    "cartesia": cartesia_bench.run,
    "sarvam": sarvam_bench.run,
}

# Fixed segments we cannot measure without a live Vobiz leg. Kept explicit so
# the roll-up is honest about what is measured and what is still assumed.
ASSUMED_MS = {
    "PSTN in (Vobiz -> gateway)": 40.0,
    "endpoint decision (our endpointer)": 120.0,
    "LLM -> first clause boundary": 50.0,
    "PSTN out + jitter buffer": 90.0,
}


def _load_dotenv() -> None:
    """Minimal .env loader -- avoids a hard dependency on python-dotenv."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _find(sections: list[Section], section_sub: str, metric_sub: str) -> float | None:
    for sec in sections:
        if section_sub.lower() not in sec.title.lower():
            continue
        for m in sec.metrics:
            if metric_sub.lower() in m.name.lower() and m.samples:
                return pct(m.samples, 50)
    return None


def _rollup(sections: list[Section]) -> Section:
    """Sum measured p50s into the voice-to-voice number the plan is judged on."""
    sec = Section(title="ROLL-UP -- estimated voice-to-voice latency at the caller's ear")

    stt_final = _find(sections, "sarvam", "flush")
    tts_ttfb = _find(sections, "cartesia", "warm socket")
    llm_final = _find(sections, "llm", "sarvam 105b-conversations  | t_first_final")
    llm_fallback = _find(sections, "llm", "gpt-oss-120b effort=low  | t_first_final")

    missing = [
        n for n, v in (
            ("Sarvam STT", stt_final), ("Cartesia", tts_ttfb), ("LLM", llm_final)
        ) if v is None
    ]
    if missing:
        sec.error = (
            f"Cannot roll up -- no measurements for: {', '.join(missing)}. "
            "Add the missing API keys and re-run."
        )
        return sec

    fixed = sum(ASSUMED_MS.values())
    for label, val in ASSUMED_MS.items():
        sec.metrics.append(Metric(name=f"  [assumed] {label}", samples=[val]))
    sec.metrics.append(Metric(
        name="  [measured] STT finalise after flush", samples=[stt_final],
        note="NOT on the critical path -- we answer off partials (FINDINGS 5)"))
    sec.metrics.append(Metric(name="  [measured] TTS first byte (warm)", samples=[tts_ttfb]))

    # STT contributes ZERO here on purpose. Partials arrive DURING speech, so by
    # the time our endpointer commits the turn we already hold the text.
    # transcript.final lands ~300ms later and is used only to correct the record
    # for extraction and logging. Adding it here would be measuring a design we
    # deliberately rejected.
    hit = fixed + tts_ttfb                                  # LLM already in flight
    miss = hit + llm_final                                  # pay the full LLM wait

    sec.metrics.append(Metric(
        name="TOTAL -- speculation HIT", samples=[hit], budget=600.0,
        note="the p50 path; LLM cost is absorbed under the caller's own speech",
    ))
    sec.metrics.append(Metric(
        name="TOTAL -- speculation MISS", samples=[miss], budget=600.0,
        note=f"+{llm_final:.0f}ms LLM; covered by the filler bank (plan 1.5)",
    ))

    if llm_fallback is not None:
        sec.metrics.append(Metric(
            name="TOTAL -- MISS w/ groq gpt-oss-120b",
            samples=[fixed + tts_ttfb + llm_fallback], budget=600.0,
            note=f"groq first-final {llm_fallback:.0f}ms",
        ))

    # The decision this whole suite exists to make.
    verdict = []
    if hit <= 600:
        verdict.append(f"p50 target REACHABLE ({hit:.0f}ms with speculation).")
    else:
        verdict.append(f"p50 target MISSED even on a speculation hit ({hit:.0f}ms).")
    if llm_fallback is not None and llm_final > llm_fallback + 80:
        verdict.append(
            f"sarvam costs {llm_final - llm_fallback:.0f}ms more than groq -- "
            "judge that against the Telugu samples, not on speed alone."
        )
    sec.metrics.append(Metric(name="=> DECISION", note=" ".join(verdict)))
    return sec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vaani Phase 0 stack benchmark")
    parser.add_argument("--only", choices=sorted(SUITES), action="append",
                        help="run only these suites (repeatable)")
    parser.add_argument("-n", "--samples", type=int, default=15)
    args = parser.parse_args(argv)

    _load_dotenv()
    chosen = args.only or list(SUITES)

    sections: list[Section] = []
    for name in chosen:
        print(f"  running {name} ...", file=sys.stderr, flush=True)
        try:
            sections.append(SUITES[name](args.samples))
        except Exception as exc:  # noqa: BLE001 - one bad vendor must not kill the run
            sections.append(Section(title=name.upper(),
                                    error=f"{type(exc).__name__}: {exc}"))

    if len(chosen) == len(SUITES):
        sections.append(_rollup(sections))

    print(render(sections, args.samples))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"phase0-{stamp}.json"
    save_json(sections, out)
    print(f"  saved: {out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
