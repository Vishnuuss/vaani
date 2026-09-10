#!/usr/bin/env python
"""Set a workflow's greeting, and refuse one the TTS would spell out letter by letter.

The call this comes from
------------------------
Run 853, 7 Sep. The first words every caller hears were stored like this:

    Namaskaram andi, MB Solar Hub nunchi Priya matladutunnanu.

That is Telugu written in Latin letters. The TTS is configured `language: te`,
so it does not read Latin as English -- it reads it as characters, and the
caller hears the company name spelled out. Every other line in that call was in
Telugu script and sounded correct; only the greeting was wrong, and the greeting
is the one line that decides whether the caller stays on the phone.

Why a guard and not just a corrected string
-------------------------------------------
Fixing the string fixes one call. The CLASS of defect is "Latin-script Telugu
reaches a Telugu TTS", and it can come back through the dashboard, a migration,
or anyone typing a greeting the way it is easiest to type. Nothing in the system
would notice: no error is raised, no test fails, the transcript looks fine, and
the only symptom is a caller hanging up on a robot spelling at them.

So the check lives at the write, where it can refuse.

What the guard does NOT do
--------------------------
It does not ban Latin. Telugu callers code-switch constantly and this project
has a standing rule against forcing language purity -- "solar", "enquiry" and
"site survey" are what people actually say. A sentence with English words in it
is correct. A sentence with NO Telugu script in it is transliteration, which is
a different thing, and that is what is refused.

    python tools/set_greeting.py --workflow 2 --file new_greeting.txt
    python tools/set_greeting.py --workflow 2 --file g.txt --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vaani_runs as V  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# Telugu block. Anything in here is script the TTS pronounces as speech.
TELUGU = range(0x0C00, 0x0C80)


def telugu_fraction(text: str) -> float:
    """Share of LETTERS that are Telugu script. Digits and punctuation ignored."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) in TELUGU) / len(letters)


def bare_acronyms(text: str) -> list[str]:
    """Runs of 2-4 capitals with no vowel -- what a TTS reads one letter at a time.

    "MB" is the live example. Written "ఎంబి" it is a word; written "MB" it is
    two letters, and the caller hears them as two letters.
    """
    import re
    return [w for w in re.findall(r"\b[A-Z]{2,4}\b", text)
            if not set(w) & set("AEIOU")]


def check(text: str) -> list[str]:
    """Every reason this greeting should not be spoken. Empty list means fine."""
    problems = []
    frac = telugu_fraction(text)
    if frac == 0.0:
        problems.append(
            "FATAL: not one Telugu character. This is transliteration, and a "
            "te-language TTS will spell it out. Write it in Telugu script.")
    elif frac < 0.30:
        problems.append(
            f"FATAL: only {frac:.0%} of the letters are Telugu script. A Telugu "
            "greeting with a few English words is normal; this is the reverse.")
    for a in bare_acronyms(text):
        problems.append(
            f"WARNING: {a!r} is bare capitals and will be read letter by letter. "
            f"Write it the way it is said, e.g. 'ఎంబి'.")
    return problems


def req(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-API-Key": V.KEY}
    if data:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(f"{V.BASE}{path}", data=data,
                               headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", type=int, required=True)
    ap.add_argument("--file", help="UTF-8 file holding the new greeting")
    ap.add_argument("--node", default=None, help="node id (default: the first)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="write despite a FATAL finding. There is no good reason.")
    a = ap.parse_args()

    print(f"server: {V.BASE}")
    doc = req("GET", f"/api/v1/workflow/fetch/{a.workflow}")
    wd = doc["workflow_definition"]
    if isinstance(wd, str):
        wd = json.loads(wd)

    nodes = wd["nodes"]
    node = (next((n for n in nodes if str(n.get("id")) == str(a.node)), None)
            if a.node else nodes[0])
    if node is None:
        sys.exit(f"node {a.node} not found; have {[n.get('id') for n in nodes]}")

    before = node["data"].get("greeting") or ""
    print(f"\nnode {node.get('id')} ({(node.get('data') or {}).get('name')})")
    print(f"current greeting ({telugu_fraction(before):.0%} Telugu script):")
    print(f"  {before}\n")
    for p in check(before):
        print(f"  [current] {p}")

    if not a.file:
        return 0

    new = Path(a.file).read_text(encoding="utf-8").strip()
    print(f"\nnew greeting ({telugu_fraction(new):.0%} Telugu script):")
    print(f"  {new}\n")
    problems = check(new)
    for p in problems:
        print(f"  {p}")
    if any(p.startswith("FATAL") for p in problems) and not a.force:
        sys.exit("\nREFUSED. Fix the greeting, or pass --force if you truly mean it.")

    if a.dry_run:
        print("\ndry run: nothing written.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = Path("backups") / f"wf{a.workflow}_def_{stamp}.json"
    bak.parent.mkdir(exist_ok=True)
    bak.write_text(json.dumps(wd, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"backup -> {bak}")

    node["data"]["greeting"] = new
    req("PUT", f"/api/v1/workflow/{a.workflow}", {"workflow_definition": wd})

    chk = req("GET", f"/api/v1/workflow/fetch/{a.workflow}")["workflow_definition"]
    if isinstance(chk, str):
        chk = json.loads(chk)
    got = next(n for n in chk["nodes"] if str(n.get("id")) == str(node.get("id")))
    ok = (got["data"].get("greeting") or "") == new
    print("readback:", "MATCHES" if ok else "!! DOES NOT MATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
