"""Replace one node's prompt on a Dograh workflow DRAFT, from a UTF-8 text file.

Editing only touches the draft. Live telephony keeps serving the last published version
until `python tools/publish_workflow.py --workflow 5` runs.

    python tools/patch_node_prompt.py --workflow 5 --node 1 --file .tmp/start.txt
    python tools/patch_node_prompt.py --workflow 5 --node 1 --file .tmp/start.txt --dry-run

Writes a timestamped backup of the whole definition before every write.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

env: dict[str, str] = {}
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
# Which install to edit. This tool was written when there was one, and it was
# voice.bswealthfinance.com. The agents that matter now live on Vaani, and
# writing a Vaani prompt to the old Dograh would edit a different client's live
# agent -- so the server is a choice, mirroring publish_workflow.py, and it
# still DEFAULTS to voice so no existing invocation changes meaning.
#
#     python tools/patch_node_prompt.py --workflow 2 --node agent #         --file .tmp/p.md --server vaani --dry-run
SERVERS = {
    "voice": ("https://voice.bswealthfinance.com", "DOGRAH_API_KEY"),
    "vaani": (env.get("VAANI_SERVER_API_URL", "https://vaani-api.bswealthfinance.com"),
              "VAANI_SERVER_API_KEY"),
}
BASE = SERVERS["voice"][0]
KEY = env["DOGRAH_API_KEY"]


def req(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-API-Key": KEY}
    if data:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", type=int, required=True)
    ap.add_argument("--node", required=True, help="node id, e.g. 1, 2, 0, 4")
    ap.add_argument("--file", required=True, help="UTF-8 file holding the new prompt")
    ap.add_argument("--server", choices=sorted(SERVERS), default="voice",
                    help="which install to edit (default: voice)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    # Repoint BEFORE the first request. Reading the definition from one install
    # and writing it to another would overwrite a different client's agent with
    # this one's prompt, and the read would look perfectly normal.
    global BASE, KEY
    BASE, key_name = SERVERS[a.server]
    KEY = env[key_name]
    print(f"server: {a.server}  {BASE}")

    new_prompt = Path(a.file).read_text(encoding="utf-8")

    doc = req("GET", f"/api/v1/workflow/fetch/{a.workflow}")
    wd = doc["workflow_definition"]
    if isinstance(wd, str):
        wd = json.loads(wd)

    node = next((n for n in wd["nodes"] if str(n.get("id")) == str(a.node)), None)
    if node is None:
        sys.exit(f"node {a.node} not found; have {[n.get('id') for n in wd['nodes']]}")

    before = node["data"].get("prompt") or ""
    name = (node.get("data") or {}).get("name")
    print(f"workflow {a.workflow}  node {a.node} ({name})  "
          f"{len(before)} -> {len(new_prompt)} chars")

    if a.dry_run:
        print("\n--- dry run, nothing written. New prompt would be: ---")
        print(new_prompt)
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = Path("backups") / f"wf{a.workflow}_def_{stamp}.json"
    bak.parent.mkdir(exist_ok=True)
    bak.write_text(json.dumps(wd, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"backup -> {bak}")

    node["data"]["prompt"] = new_prompt
    req("PUT", f"/api/v1/workflow/{a.workflow}", {"workflow_definition": wd})

    check = req("GET", f"/api/v1/workflow/fetch/{a.workflow}")["workflow_definition"]
    if isinstance(check, str):
        check = json.loads(check)
    got = next(n for n in check["nodes"] if str(n.get("id")) == str(a.node))
    ok = (got["data"].get("prompt") or "") == new_prompt
    print("readback:", "MATCHES" if ok else "!! DOES NOT MATCH - check the dashboard")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
