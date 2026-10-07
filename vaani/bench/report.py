"""Stats and table rendering for benchmark runs."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable


def pct(values: list[float], p: float) -> float:
    """Percentile via nearest-rank. Small sample sizes make interpolation
    misleading, and we run 20-30 samples, not thousands."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(p / 100.0 * len(ordered) + 0.5)) - 1))
    return ordered[idx]


@dataclass
class Metric:
    """One timed quantity, sampled N times. All values in milliseconds."""

    name: str
    unit: str = "ms"
    budget: float | None = None          # target from the plan's budget table
    samples: list[float] = field(default_factory=list)
    note: str = ""

    def add(self, ms: float) -> None:
        self.samples.append(ms)

    @property
    def ok(self) -> bool | None:
        """None when there is no budget to judge against."""
        if self.budget is None or not self.samples:
            return None
        return pct(self.samples, 50) <= self.budget

    def row(self) -> list[str]:
        if not self.samples:
            return [self.name, "-", "-", "-", "-", "SKIPPED", self.note]
        p50, p90 = pct(self.samples, 50), pct(self.samples, 90)
        budget = f"{self.budget:.0f}" if self.budget is not None else "-"
        verdict = {True: "PASS", False: "OVER", None: "-"}[self.ok]
        return [
            self.name,
            f"{min(self.samples):.0f}",
            f"{p50:.0f}",
            f"{p90:.0f}",
            budget,
            verdict,
            self.note,
        ]


@dataclass
class Section:
    title: str
    metrics: list[Metric] = field(default_factory=list)
    error: str = ""

    def metric(self, name: str, budget: float | None = None, note: str = "") -> Metric:
        m = Metric(name=name, budget=budget, note=note)
        self.metrics.append(m)
        return m


HEADERS = ["metric", "min", "p50", "p90", "budget", "verdict", "note"]


def render(sections: Iterable[Section], samples: int) -> str:
    out: list[str] = []
    out.append("")
    out.append("=" * 96)
    out.append(f"  VAANI PHASE 0 -- STACK BENCHMARK   (n={samples} per metric, all times in ms)")
    out.append("=" * 96)

    for sec in sections:
        out.append("")
        out.append(f"## {sec.title}")
        if sec.error:
            out.append(f"   !! {sec.error}")
            continue
        rows = [m.row() for m in sec.metrics]
        if not rows:
            out.append("   (no metrics)")
            continue
        widths = [
            max(len(HEADERS[i]), max(len(r[i]) for r in rows))
            for i in range(len(HEADERS))
        ]
        header = "  ".join(h.ljust(widths[i]) for i, h in enumerate(HEADERS))
        out.append("   " + header)
        out.append("   " + "-" * len(header))
        for r in rows:
            out.append("   " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    out.append("")
    return "\n".join(out)


def save_json(sections: Iterable[Section], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(s) for s in sections]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
