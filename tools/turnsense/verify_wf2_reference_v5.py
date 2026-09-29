#!/usr/bin/env python
"""Build MB Solar's v5 prompt and prove it safe before anyone publishes it.

v4 (.tmp/wf2_agent_prompt_v4.md, the file pre_call_gate pins as published) carries 2,696 characters of the
client's own answers OUTSIDE any bold row -- the PM Surya Ghar details, the
four-step process, and ten prepared Telugu answers -- so `client_reference`
never injects them and a caller asking about net metering hears "I will find
out" while the answer sits in the file (doc 41, "still open").

v5 = v4's head, byte-identical + those facts as triggered rows + v4's rows,
unchanged. Checked here, offline:

  1. the head is byte-identical (the part compiled into every turn)
  2. every TRIGGER compiles; every row fits MAX_CHARS
  3. no role labels, no MODE: lines, no answer loses its tail at a '?'
     (the same checks pre_call_gate runs on the published snapshot)
  4. nothing is left unreachable except headings and instructions
  5. every new row fires on the questions it exists for, and on NO plain
     answer -- measured over every real MB Solar caller line in the RT corpus

    python tools/turnsense/verify_wf2_reference_v5.py
"""
from __future__ import annotations

import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO.parent / "dograh-vapi"))
from api.services.vaani import client_reference as CR  # noqa: E402
from api.services.vaani.reply_sanitizer import ROLE_LABEL_RE, ReplySanitizer  # noqa: E402

TS = REPO / ".tmp" / "turnsense"
NL = chr(10)
OUT = REPO / ".tmp" / "wf2_agent_prompt_v5.md"

# Trigger fixes, measured over 844 real MB Solar caller lines (29 Sep):
#   roof row     fired on 10 lines, ALL plain answers ("సొంత రూఫ్ ఉంది.") -- the
#                agent asks about the roof on every call, so the FAQ answer was
#                injected into nearly every call's roof turn
#   what-else    fired on a bare "కెమెరా" and on "ఇంకేమైనా రోగం వస్తుందా"
#   family       fired on any "ఆయన" ("he"), e.g. "మీరు ఏ ఆయన?"
TRIGGER_FIXES = {
    r"TRIGGER: రూఫ|roof|డాబా|టెర్రస|terrace|నీడ|shade|shadow|చిన్న\s*రూఫ":
    r"TRIGGER: (రూఫ|roof|డాబా|టెర్రస|terrace)[^\n]{0,30}(చిన్న|small|సరిపోతుంద|సరిపోద|చాలుతుంద|చాలదా|ఎంత\s*(కావాల|స్థలం|ప్లేస్|ఏరియా))|నీడ|shade|shadow|చిన్న\s*రూఫ",
    r"|సీసీ\s*కెమ|CC\s*కెమ|cctv|కెమెరా|camera|":
    r"|సీసీ\s*కెమ|CC\s*కెమ|cctv|",
    r"|ఇంకా\s*(ఏమ|ఏం|ఏవ)|ఇంకేమ|వేరే\s*(ఏమ|ఏం)":
    r"|ఇంకా\s*(ఏమ|ఏం|ఏవ)[ఀ-౿]*\s*(చేస్తార|ఉన్నాయ|ఇస్తార|సర్వీస)|ఇంకేమ[ఀ-౿]*\s*(చేస్తార|ఉన్నాయ|ఇస్తార|సర్వీస)|వేరే\s*(ఏమ|ఏం)[ఀ-౿]*\s*(చేస్తార|ఉన్నాయ|ఇస్తార)",
    r"TRIGGER: ఆయన|ఆవిడ|భర్త|భార్య|husband|wife|ఫ్యామిల|family|ఇంట్లో\s*(అడిగ|మాట్లాడ)":
    r"TRIGGER: (ఆయన|ఆవిడ|భర్త|భార్య|husband|wife|ఫ్యామిల|family|ఇంట్లో|మా\s*వాళ్ళ)[ఀ-౿]*\s*(తో|ని|ను|కి)?\s*(అడిగ|అడగ|మాట్లాడ|చెప్ప|కనుక్క|డిసైడ్|decide|discuss|ask)",
}

# (utterance, title of the row that must fire)
MUST_FIRE = [
    ("మీరు ఏం చేస్తారు అండి?", "What is MB Solar Hub / what do you do?"),
    ("ఎంబి సోలార్ అంటే ఏంటి?", "What is MB Solar Hub / what do you do?"),
    ("ఆహ్, సోలార్ హబ్ అంటే ఏంటమ్మా, అది నాకు అర్థం కాలేదు", "What is MB Solar Hub / what do you do?"),
    ("ఇది ఎలా పని చేస్తుంది?", "How does it work / what is the process?"),
    ("ప్రాసెస్ ఏంటి అండి?", "How does it work / what is the process?"),
    ("ఎప్పుడు కాల్ చేస్తారు వెండర్?", "How soon will a vendor contact me?"),
    ("స్కీమ్ ఎప్పుడు మొదలైంది?", "When did the scheme start?"),
    # the three rows whose triggers were tightened must still fire on questions
    ("మా రూఫ్ చిన్నది, సరిపోతుందా?", "What if my roof is small / shaded?"),
    ("నీడ పడుతుంది, పర్లేదా?", "What if my roof is small / shaded?"),
    ("హాట్ వాటర్ కూడా ఉందా?", "What else do you do besides panels"),
    ("సీసీ కెమెరాలు పెడతారా?", "What else do you do besides panels"),
    ("ఇంకా ఏమి చేస్తారు మీరు?", "What else do you do besides panels"),
    ("మా ఆయనని అడిగి చెప్తాను", '"I need to ask my husband / wife / family"'),
    ("ఇంట్లో మాట్లాడి చెప్తా", '"I need to ask my husband / wife / family"'),
    # rows that were already there, spot-checked unchanged
    ("సబ్సిడీ ఎంత వస్తుంది?", "Is there a subsidy? / How much subsidy?"),
    ("నెట్ మీటరింగ్ అంటే ఏంటి?", "What is net metering"),
    ("మీరు ఎక్కడి నుంచి?", "Where are you / how do I reach you?"),
]
# Plain answers to the agent's own questions, from real calls. No row may fire.
MUST_NOT_FIRE = [
    "సొంత ఇల్లు.", "అపార్ట్‌మెంట్.", "కమర్షియల్ ప్లేస్.", "మాది సొంత ఇల్లే.",
    "మాది హైదరాబాద్.", "విజయవాడ అండి", "2 లక్షలు వస్తున్నాయి.", "3000 వస్తుంది.",
    "నా పేరు సురేష్.", "రేపు సాయంత్రం.", "ఆ... మాట్లాడొచ్చు.", "అవును.", "సరే.",
    "ఉంది.", "లేదు.", "సొంత రూఫ్ ఉంది.", "రూఫ్ ఉంది, టెర్రస్ ఏం లేదు.",
    "ఆ, టెర్రస్ ఉంది.", "యార్, కెమెరా—", "మీరు ఏ ఆయన?", "10:00. 10:00. అవును.",
]


def build() -> tuple[str, str]:
    v4 = (REPO / ".tmp" / "wf2_agent_prompt_v4.md").read_text(encoding="utf-8")
    head, ref = CR.split(v4)
    for old, new in TRIGGER_FIXES.items():
        assert ref.count(old) == 1, f"trigger fix did not match exactly once: {old[:50]}"
        ref = ref.replace(old, new)
    i = ref.index("## Questions customers actually ask")
    rows = (TS / "wf2_reference_new_rows.md").read_text(encoding="utf-8").strip()
    ref = ref[:i] + rows + NL + NL + ref[i:]
    return v4, head.rstrip() + NL + NL + ref.strip() + NL


def rt_caller_lines() -> list[str]:
    out = []
    for f in glob.glob(str(TS / "rt" / "wf2_*.json")):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        out += [s["text"] for s in d.get("segments", []) if s.get("text")]
    return out


def main() -> int:
    ok = True

    def check(cond, label, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'OK ' if cond else 'BAD'}] {label:48} {detail}")

    v4, prompt = build()
    h4, _ = CR.split(v4)
    h5, r5 = CR.split(prompt)
    rows = CR.parse(r5)
    new_titles = [m.group("title").strip() for m in CR._ROW.finditer(
        (TS / "wf2_reference_new_rows.md").read_text(encoding="utf-8"))]
    print(f"v4 {len(v4)} chars -> v5 {len(prompt)} chars; rows {len(CR.parse(CR.split(v4)[1]))} -> {len(rows)}")

    check(h5 == h4, "head byte-identical to v4 (every-turn prompt)")
    dead = [r.title for r in rows if r.pattern is None and r.title != "Anything else at all"]
    check(not dead, "every TRIGGER compiles", ", ".join(dead))
    big = [r.title for r in rows if len(f"REFERENCE ({r.title}): {r.body}") > CR.MAX_CHARS]
    check(not big, "every row fits MAX_CHARS", ", ".join(big))
    check(not ROLE_LABEL_RE.search(prompt), "no role labels")
    check("MODE:" not in prompt, "no MODE: lines")

    def spoken(q: str) -> str:
        s = ReplySanitizer()
        out = ""
        for i in range(0, len(q), 5):
            out += s.feed(q[i:i + 5]) or ""
        return out + (s.finish() or "")
    lost = []
    for r in rows:
        for q in re.findall(r'"([^"]+)"', r.body or ""):
            m = re.search(r"\?\s*(.{4,})$", q.strip(), re.S)
            if m:
                w = re.findall(r"[ఀ-౿]{3,}|[A-Za-z]{3,}", m.group(1))
                if w and w[0] not in spoken(q):
                    lost.append(r.title[:30])
    check(not lost, "no answer loses its tail at a '?'", ", ".join(lost))

    covered = set()
    for m in CR._ROW.finditer(r5):
        covered.update(range(m.start(), m.end()))
    orphan = [ln for ln in "".join(ch for i, ch in enumerate(r5) if i not in covered).splitlines()
              if ln.strip() and not ln.startswith("##")]
    print(f"  unreachable outside rows: {sum(len(x) for x in orphan)} chars "
          f"(v4: 2,696):")
    for ln in orphan:
        print(f"     | {ln[:100]}")

    by_title = {r.title: r for r in rows}
    miss = [(u, t) for u, t in MUST_FIRE
            if t not in CR.lookup(rows, u) and not any(
                x.startswith(f"REFERENCE ({t})") for x in CR.lookup(rows, u))]
    check(not miss, f"{len(MUST_FIRE)} questions fire their row",
          "; ".join(f"{u} -> {t[:25]}" for u, t in miss[:4]))
    wrong = [(a, [x[11:40] for x in CR.lookup(rows, a)]) for a in MUST_NOT_FIRE
             if CR.lookup(rows, a)]
    check(not wrong, f"{len(MUST_NOT_FIRE)} plain answers fire nothing",
          "; ".join(f"{a} -> {w}" for a, w in wrong[:4]))

    lines = rt_caller_lines()
    hits = Counter()
    examples: dict[str, list[str]] = {}
    for ln in lines:
        for t in new_titles:
            r = by_title.get(t)
            if r and r.matches(ln):
                hits[t] += 1
                examples.setdefault(t, []).append(ln)
    print(f"\n  new rows over {len(lines)} real MB Solar caller lines (precision check):")
    for t in new_titles:
        ex = " || ".join(e[:45] for e in examples.get(t, [])[:3])
        print(f"   {hits[t]:>4}  {t[:48]:48}  {ex}")
    rate = max(hits.values(), default=0) / max(1, len(lines))
    check(rate < 0.03, "no new row fires on >3% of real lines", f"max {rate:.1%}")

    OUT.write_text(prompt, encoding="utf-8")
    print(f"\n{'SAFE' if ok else 'NOT SAFE'}: wrote {OUT.relative_to(REPO)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
