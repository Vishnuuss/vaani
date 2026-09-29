"""Replay adapter: build a TurnSenseAnalyzer from a policy string.

    turnsense                                  defaults
    turnsense:fast=0.8,slow=0.35,max=1.2       override any TurnSenseParams
    turnsense:agent=0                          ignore the agent's line (words only)
    turnsense:model=<path>                     score a candidate artifact
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO.parent / "dograh-vapi"))

from api.services.vaani.turnsense import TurnSenseModel  # noqa: E402
from api.services.vaani.turnsense_turn import (  # noqa: E402
    TurnSenseAnalyzer,
    TurnSenseParams,
)

_KEYS = {"fast": "fast_p", "slow": "slow_p", "min": "min_wait_secs",
         "mid": "mid_wait_secs", "max": "max_wait_secs",
         "notext": "no_text_wait_secs", "ceil": "ceiling_secs"}
_MODELS: dict[str, TurnSenseModel] = {}


class _NoAgent(TurnSenseAnalyzer):
    def note_agent(self, text: str) -> None:
        return None


def from_spec(spec: str, sr: int):
    opts = {}
    if ":" in spec:
        for kv in spec.split(":", 1)[1].split(","):
            if kv.strip():
                k, v = kv.split("=", 1)
                opts[k.strip()] = v.strip()
    params = TurnSenseParams(**{_KEYS[k]: float(v) for k, v in opts.items() if k in _KEYS})
    path = opts.get("model")
    key = path or "<default>"
    if key not in _MODELS:
        _MODELS[key] = TurnSenseModel.load(path) if path else TurnSenseModel.load()
    cls = _NoAgent if opts.get("agent") == "0" else TurnSenseAnalyzer
    a = cls(sample_rate=sr, params=params, model=_MODELS[key])
    a.set_sample_rate(sr)
    return a
