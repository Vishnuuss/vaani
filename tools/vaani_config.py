"""Read or set one workflow_configurations key on a VAANI workflow.

    python tools/vaani_config.py --workflow 2
    python tools/vaani_config.py --workflow 2 --set speculation_enabled=false
    python tools/vaani_config.py --workflow 2 --set llm_hedge=2 --set max_call_duration=300

Why a separate tool from `sync_latency_config.py`
--------------------------------------------------
That one has exactly one job: push the values the CODE intends into the stored
config that overrides them. It reads its targets from
`api/schemas/workflow_configurations.py` and takes no arguments, on purpose --
it is a reconciler, not an editor.

This is the editor, for settings whose right value is a judgement rather than a
code default: how many completions to hedge, whether speculation is worth its
tokens, how long a call may run.

Every write is read back. `workflow_configurations` has returned HTTP 200 for a
write that changed nothing before (the STT config on 29 Aug, wrong nesting), and
a silently-ignored setting is indistinguishable from a working one until several
live calls later.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

env: dict[str, str] = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

BASE = (env.get("VAANI_SERVER_API_URL")
        or "https://vaani.bswealthfinance.com").rstrip("/")
KEY = env["VAANI_SERVER_API_KEY"]


def api(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-API-Key": KEY}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{path}", data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # The server says exactly what it objected to and a bare traceback
        # throws that away. A 422 from this endpoint is almost always a
        # sentence naming the field, and reading it is the difference between
        # a fix and an afternoon of guessing.
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {e.code} from {method} {path}\n{detail[:600]}")
    return json.loads(raw) if raw.strip() else {}


def coerce(text: str):
    """"false" is a boolean, "2" is an int, "0.2" is a float, else a string.

    Types matter here: the pipeline reads these with
    `bool(run_configs.get(...))`, and the STRING "false" is true.
    """
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        # `model_overrides` is a nested object, not a scalar. Added 18 Sep to
        # put Soniox on ONE workflow via model_overrides.stt while the other
        # five stay on the organisation's Sarvam config -- resolve.py
        # deep-merges this section and handles a provider change explicitly.
        return json.loads(stripped)
    low = stripped.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", type=int, default=2)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    a = ap.parse_args()

    cfg = dict(api("GET", f"/api/v1/workflow/fetch/{a.workflow}")
               .get("workflow_configurations") or {})

    if not a.set:
        print(json.dumps(cfg, indent=1, ensure_ascii=False))
        return 0

    changes = {}
    for pair in a.set:
        if "=" not in pair:
            raise SystemExit(f"--set expects KEY=VALUE, got {pair!r}")
        key, raw = pair.split("=", 1)
        key = key.strip()
        value = coerce(raw)
        print(f"{key:<32}{str(cfg.get(key, '(unset)')):>16}  ->  {value!r}")
        changes[key] = value

    cfg.update(changes)
    api("PUT", f"/api/v1/workflow/{a.workflow}",
        {"workflow_configurations": cfg})

    after = (api("GET", f"/api/v1/workflow/fetch/{a.workflow}")
             .get("workflow_configurations") or {})
    bad = {k: (after.get(k), v) for k, v in changes.items() if after.get(k) != v}
    if bad:
        print(f"\nWROTE BUT DID NOT STICK: {bad}")
        return 1
    print(f"\napplied and verified: {json.dumps(changes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
