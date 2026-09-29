"""Place a single outbound test call on Dograh and pull back the diagnostics.

This self-hosted build has no /telephony/initiate-call route -- outbound goes through the
campaign API. A one-row CSV campaign is exactly what the dashboard's "Manual call" button
creates, so this reproduces that flow over the API.

    python tools/place_test_call.py --workflow 1 --phone +91XXXXXXXXXX --name tester
    python tools/place_test_call.py --report <campaign_id>      # fetch results later

Reads DOGRAH_BASE_URL / DOGRAH_API_KEY from .env (NEXT_PUBLIC_DOGRAH_API_URL also accepted).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

# Which telephony configuration the campaign dials through. This was hardcoded
# to 1 ("BSWEALTH"), which is the id on the OLD production Dograh at
# voice.bswealthfinance.com. On vaani there is no id 1 at all, and
# campaign/create answers `telephony_configuration_not_found` -- after the CSV
# has already been uploaded, so it looks like the upload failed.
#
# Resolved from the server instead: GET /api/v1/organizations/telephony-configs
# (plural -- the router prefix is "/organizations") and take the one flagged
# `is_default_outbound`. On vaani, 13 Sep 2026, that is id 5, "vaani"/vobiz.
# --telephony-config overrides it.
TELEPHONY_CONFIG_ID = None


def resolve_telephony_config() -> int:
    """The org's default outbound configuration, read off the server."""
    data = req("GET", "/api/v1/organizations/telephony-configs")
    configs = data.get("configurations", []) if isinstance(data, dict) else []
    if not configs:
        sys.exit("no telephony configurations on this server")
    for c in configs:
        if c.get("is_default_outbound") and c.get("is_ready_for_outbound"):
            print(f"telephony config: {c['id']} ({c.get('name')}/{c.get('provider')})")
            return c["id"]
    sys.exit(f"no default outbound config ready; have: "
             f"{[(c.get('id'), c.get('name')) for c in configs]}")


def _env() -> tuple[str, str]:
    env = {}
    p = Path(".env")
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    import os
    base = os.environ.get("DOGRAH_BASE_URL") or env.get("DOGRAH_BASE_URL") \
        or env.get("NEXT_PUBLIC_DOGRAH_API_URL")
    key = os.environ.get("DOGRAH_API_KEY") or env.get("DOGRAH_API_KEY")
    if not base or not key:
        sys.exit("DOGRAH_BASE_URL and DOGRAH_API_KEY must be set")
    return base.rstrip("/"), key


BASE, KEY = "", ""


def req(method: str, path: str, body=None, raw_url: str | None = None, data: bytes | None = None,
        content_type: str | None = None):
    url = raw_url or f"{BASE}{path}"
    headers = {}
    if not raw_url:
        headers["X-API-Key"] = KEY
    payload = data
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    r = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=60) as resp:
        text = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def place(workflow: int, phone: str, name: str, vertical: str,
          telephony_config: int | None = None) -> int:
    telephony_config = telephony_config or resolve_telephony_config()
    lead_id = str(uuid.uuid4())
    csv = ("phone_number,customer_name,city,property_type,budget,lead_id,email,vertical\n"
           f"{phone},{name},,,,{lead_id},,{vertical}\n").encode()
    fname = f"manual_call_{lead_id}_{int(time.time() * 1000)}.csv"

    up = req("POST", "/api/v1/s3/presigned-upload-url",
             {"file_name": fname, "file_size": len(csv), "content_type": "text/csv"})
    print("presign:", json.dumps(up)[:300])

    put_url = up.get("upload_url") or up.get("url")
    key = up.get("file_key") or up.get("key") or up.get("s3_key") or up.get("object_key")
    if not put_url or not key:
        sys.exit(f"unexpected presign response: {up}")

    req("PUT", "", raw_url=put_url, data=csv, content_type="text/csv")
    print("uploaded:", key)

    camp = req("POST", "/api/v1/campaign/create", {
        "name": f"Manual call - {name} (gpt-oss test)",
        "workflow_id": workflow,
        "source_type": "csv",
        "source_id": key,
        "telephony_configuration_id": telephony_config,
        "max_concurrency": 1,
        "retry_config": {"enabled": False, "max_retries": 0, "retry_delay_seconds": 120,
                         "retry_on_busy": False, "retry_on_no_answer": False,
                         "retry_on_voicemail": False},
    })
    cid = camp.get("id") or camp.get("campaign_id")
    print("campaign:", cid, json.dumps(camp)[:200])

    req("POST", f"/api/v1/campaign/{cid}/start")
    print(f"STARTED campaign {cid} -> dialing {phone} on workflow {workflow}")
    return cid


def report(cid: int) -> None:
    prog = req("GET", f"/api/v1/campaign/{cid}/progress")
    print("progress:", json.dumps(prog, ensure_ascii=False)[:400])
    runs = req("GET", f"/api/v1/campaign/{cid}/runs")
    rows = runs.get("runs", runs) if isinstance(runs, dict) else runs
    if not rows:
        print("no runs yet")
        return
    for r in rows:
        rid = r.get("id") or r.get("workflow_run_id")
        print(f"\n=== run {rid} | {r.get('state') or r.get('status')} "
              f"| disposition={r.get('call_disposition')}")
        detail = req("GET", f"/api/v1/workflow/{r.get('workflow_id')}/runs/{rid}")
        show_run(detail)


def show_run(detail: dict) -> None:
    ctx = detail.get("gathered_context") or {}
    print("nodes_visited:", ctx.get("nodes_visited"))
    print("gathered:", json.dumps({k: v for k, v in ctx.items() if k != "nodes_visited"},
                                  ensure_ascii=False)[:500])
    print("cost:", json.dumps(detail.get("cost_info"), ensure_ascii=False)[:200])
    events = (detail.get("logs") or {}).get("realtime_feedback_events") or []
    ttfb, lat, transcript = [], [], []
    for e in events:
        t = e.get("type") or ""
        p = e.get("payload") or {}
        if t == "rtf-ttfb-metric":
            ttfb.append(float(p["ttfb_seconds"]))
        elif t == "rtf-latency-measured":
            lat.append(float(p["latency_seconds"]))
        elif t == "rtf-bot-text":
            transcript.append(("bot", p.get("text")))
        elif t == "rtf-user-transcription":
            transcript.append(("user", p.get("text")))
        elif t == "rtf-node-transition":
            transcript.append(("NODE", f"-> {p.get('node_name')}"))
        elif "error" in t:
            print("  !! ERROR EVENT:", json.dumps(e, ensure_ascii=False)[:300])

    usage = (detail.get("usage_info") or {}).get("llm") or {}
    for svc, u in usage.items():
        turns = max(1, sum(1 for w, _ in transcript if w == "bot"))
        print(f"  tokens {svc.split('|||')[-1]}: prompt={u.get('prompt_tokens')} "
              f"cached={u.get('cache_read_input_tokens')} "
              f"completion={u.get('completion_tokens')} "
              f"({u.get('completion_tokens', 0) // turns}/reply over {turns} replies)")

    def stat(name, xs):
        if not xs:
            print(f"  {name}: none recorded")
            return
        xs = sorted(xs)
        p50 = xs[len(xs) // 2]
        print(f"  {name}: n={len(xs)} min={min(xs):.2f} p50={p50:.2f} max={max(xs):.2f}")

    print("LATENCY")
    stat("llm ttfb (s)", ttfb)
    stat("turn latency (s)", lat)
    print("TRANSCRIPT")
    for who, text in transcript:
        print(f"  {who:5s}: {text}")


def main() -> int:
    global BASE, KEY
    BASE, KEY = _env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", type=int)
    ap.add_argument("--phone")
    ap.add_argument("--name", default="test")
    ap.add_argument("--vertical", default="loan")
    ap.add_argument("--telephony-config", type=int,
                    help="override the resolved default outbound config id")
    ap.add_argument("--report", type=int, help="campaign id to fetch results for")
    a = ap.parse_args()

    if a.report:
        report(a.report)
        return 0
    if not (a.workflow and a.phone):
        sys.exit("--workflow and --phone required (or use --report <campaign_id>)")
    place(a.workflow, a.phone, a.name, a.vertical, a.telephony_config)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
