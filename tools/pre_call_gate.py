#!/usr/bin/env python
"""The one gate. Nothing dials a phone until every line here is green.

Why this exists
---------------
On 23-24 September the live agent was taken down or degraded by things that
were each detectable in seconds, before a single call:

  * a prompt edit that made every reply get discarded -- four silent calls;
  * a Cartesia account out of credit -- HTTP 402, a greeting and then nothing;
  * config edited on the DRAFT while the published snapshot still served the
    old values -- an hour of tuning that reached no call;
  * a deploy that had not landed yet, so a test call measured the old code;
  * a Soniox key that only works on the India host while the code dialled the
    global one -- the agent could not hear.

Each of those cost a billed call and a person's patience to discover. This runs
all of them, plus every conversation-level test, in one command, and exits
non-zero if anything is wrong. A call placed after a red gate is a call placed
knowing it will fail.

    python tools/pre_call_gate.py                 # everything
    python tools/pre_call_gate.py --skip-tests    # live checks only (fast)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
VAANI = REPO.parent / "dograh-vapi"
sys.path.insert(0, str(REPO / "tools"))
import vaani_runs as V  # noqa: E402

WORKFLOW = 2
PROMPT_FILE = REPO / ".tmp" / "wf2_agent_prompt_v4.md"

# What the published snapshot must hold. `telugu_turn_assist` is the 22 Sep
# hybrid, restored 24 Sep: the 0.7s timer plus the Telugu detector racing it,
# measured at 0.37s endpoint p50 on run 997 against 0.95s timer-only.
EXPECTED_CONFIG = {
    "turn_stop_strategy": "transcription",
    "dograh_speech_timeout_secs": 0.7,
    "telugu_turn_assist": True,
    # Deadlocks on gpt-oss-120b (31s hangs, run 1013); must stay off.
    "semantic_turn_completion": False,
    "speculation_enabled": True,
}

# Every conversation-level suite written for a defect a real caller heard.
TEST_FILES = [
    "api/tests/test_a_whole_conversation_behaves.py",
    "api/tests/test_hello_means_say_it_again.py",
    "api/tests/test_the_subsidy_can_be_said.py",
    "api/tests/test_a_known_field_moves_on.py",
    "api/tests/test_the_repair_line_does_not_loop.py",
    "api/tests/test_an_answered_field_is_never_asked_again.py",
    "api/tests/test_the_canned_lines_sound_human.py",
    "api/tests/test_the_answer_bank_numbers_are_legal.py",
    "api/tests/test_the_blind_floor_is_the_real_lever.py",
    "api/tests/test_dead_turn_config_is_loud.py",
]

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((bool(ok), label, detail))
    print(f"  [{'OK ' if ok else 'BAD'}] {label:40} {detail}")
    return bool(ok)


def env(name: str) -> str:
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(name + "=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# --------------------------------------------------------------------------- 1
def gate_tests() -> None:
    print("\n1. Conversation-level tests (the real brain, whole calls)")
    py = VAANI / ".venv-test" / "Scripts" / "python.exe"
    if not py.exists():
        check(False, "test venv present", str(py))
        return
    envv = dict(os.environ, DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
                REDIS_URL="redis://localhost:6379", PYTHONIOENCODING="utf-8")
    missing = [f for f in TEST_FILES if not (VAANI / f).exists()]
    check(not missing, "every gate test file exists", ", ".join(missing))
    files = [f for f in TEST_FILES if (VAANI / f).exists()]
    out = subprocess.run([str(py), "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"],
                         cwd=VAANI, env=envv, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=900)
    tail = (out.stdout or "").strip().splitlines()[-1:] or [""]
    summary = tail[0]
    failed = re.search(r"(\d+) failed", summary)
    errors = re.search(r"(\d+) error", summary)
    passed = re.search(r"(\d+) passed", summary)
    check(out.returncode == 0 and not failed and not errors and bool(passed),
          "all gate tests pass", summary)
    if out.returncode != 0:
        for line in (out.stdout or "").splitlines():
            if line.startswith(("FAILED", "ERROR")):
                print(f"        {line[:160]}")


# --------------------------------------------------------------------------- 2
def gate_deploy() -> None:
    print("\n2. The code that will answer the phone")
    head = subprocess.run(["git", "rev-parse", "vaani/main"], cwd=VAANI,
                          capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "api/services"], cwd=VAANI,
                           capture_output=True, text=True).stdout.strip()
    check(not dirty, "no unpushed edits under api/services",
          "UNCOMMITTED: " + dirty.replace("\n", "; ") if dirty else "")
    build = str(V.get("/api/v1/health").get("build") or "")
    check(bool(head) and head.startswith(build[:8]) and len(build) >= 8,
          "live build == vaani/main", f"live {build[:12]}  main {head[:12]}")


# --------------------------------------------------------------------------- 3
def _published():
    vs = V.get(f"/api/v1/workflow/{WORKFLOW}/versions")
    vs = vs.get("versions") if isinstance(vs, dict) else vs
    return [v for v in vs if v.get("status") == "published"][0]


def gate_config_and_prompt() -> None:
    print("\n3. The PUBLISHED snapshot (drafts never reach a call)")
    live = _published()
    wc = live.get("workflow_configurations") or {}
    print(f"        published v{live.get('version_number')}")
    for k, want in EXPECTED_CONFIG.items():
        check(wc.get(k) == want, f"config {k}", f"{wc.get(k)!r} (want {want!r})")
    check(len(wc) >= 30, "config not wiped by a partial PUT", f"{len(wc)} keys")

    d = live.get("workflow_json")
    d = json.loads(d) if isinstance(d, str) else d
    prompt = (((d.get("nodes") or [])[0]).get("data") or {}).get("prompt") or ""
    want = io.open(PROMPT_FILE, encoding="utf-8").read() if PROMPT_FILE.exists() else None
    check(want is not None and prompt == want, "published prompt == intended file",
          f"{len(prompt)} chars vs {PROMPT_FILE.name}")

    # The things that silence the agent outright, checked on the LIVE text.
    sys.path.insert(0, str(VAANI))
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
    try:
        from api.services.vaani import client_reference as cr
        from api.services.vaani.reply_sanitizer import ReplySanitizer, ROLE_LABEL_RE
    except Exception as exc:                       # noqa: BLE001
        check(False, "import the live sanitizer", f"{type(exc).__name__}: {exc}")
        return
    check(not ROLE_LABEL_RE.search(prompt), "no role labels (silences every reply)")
    check("MODE:" not in prompt, "no MODE: lines")
    check(not any(c in prompt for c in "✓○◐"), "no turn markers")

    rows = cr.parse(cr.split(prompt)[1])
    dead = [r.title for r in rows if r.pattern is None and "Anything else" not in r.title]
    check(len(rows) >= 30, "answer-bank rows parsed", f"{len(rows)} rows")
    check(not dead, "every TRIGGER compiles", ", ".join(dead[:4]))

    def spoken(q: str) -> str:
        s = ReplySanitizer(); out = ""
        for i in range(0, len(q), 5):
            out += s.feed(q[i:i + 5]) or ""
        return out + (s.finish() or "")

    lost = []
    for r in rows:
        for q in re.findall(r'"([^"]+)"', r.body or ""):
            m = re.search(r"\?\s*(.{4,})$", q.strip(), re.S)
            if not m:
                continue
            w = re.findall(r"[ఀ-౿]{3,}|[A-Za-z]{3,}", m.group(1))
            if w and w[0] not in spoken(q):
                lost.append(r.title[:30])
    check(not lost, "no answer loses its tail at a '?'", ", ".join(lost[:4]))


# --------------------------------------------------------------------------- 4
def gate_voice_and_ears() -> None:
    print("\n4. Ears and voice actually work (the silent failures)")
    pipe = V.get("/api/v1/organizations/model-configurations/v2")["configuration"]["byok"]["pipeline"]
    stt, tts, llm = pipe.get("stt") or {}, pipe.get("tts") or {}, pipe.get("llm") or {}
    check(stt.get("provider") == "soniox" and stt.get("model") == "stt-rt-v5",
          "STT soniox ears-only", f"{stt.get('provider')}/{stt.get('model')}")
    check(llm.get("model") == "openai/gpt-oss-120b", "LLM model", str(llm.get("model")))

    # Cartesia: a real synthesis, so an exhausted account (402) is caught here
    # and not by a caller hearing a greeting and then nothing.
    # From .env only, never a literal in this file. Fails closed: a gate that
    # silently skipped the voice check would report GREEN on a mute agent.
    key = env("CARTESIA_API_KEY")
    if not key:
        check(False, "CARTESIA_API_KEY set in .env", "missing")
        return
    voice = tts.get("voice") or ""
    body = json.dumps({"model_id": tts.get("model") or "sonic-3.5",
                       "transcript": "నమస్కారం అండి.", "language": "te",
                       "voice": {"mode": "id", "id": voice},
                       "output_format": {"container": "raw", "encoding": "pcm_s16le",
                                         "sample_rate": 16000}}).encode()
    req = urllib.request.Request("https://api.cartesia.ai/tts/bytes", data=body, headers={
        "X-API-Key": key, "Cartesia-Version": "2024-06-10", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            n = len(r.read())
        check(n > 5000, "Cartesia synthesises (credit + voice)", f"{n} bytes")
    except urllib.error.HTTPError as e:
        check(False, "Cartesia synthesises (credit + voice)", f"HTTP {e.code}")
    except Exception as e:                          # noqa: BLE001
        check(False, "Cartesia synthesises (credit + voice)", type(e).__name__)
    tk = tts.get("api_key"); tk = tk[0] if isinstance(tk, list) else tk
    check(str(tk or "")[-4:] == key[-4:], "server TTS key == the tested key",
          f"...{str(tk or '')[-4:]} vs ...{key[-4:]}")

    # Soniox: the India-scoped key must authenticate on the India host.
    try:
        import asyncio
        import websockets

        async def probe():
            async with websockets.connect(
                    "wss://stt-rt.in.soniox.com/transcribe-websocket", open_timeout=25) as ws:
                await ws.send(json.dumps({"api_key": env("SONIOX_API_KEY"),
                                          "model": "stt-rt-v5", "audio_format": "pcm_s16le",
                                          "sample_rate": 16000, "num_channels": 1}))
                await ws.send(b"\x00\x00" * 1600)
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
                return m.get("error_code")
        err = asyncio.run(probe())
        check(not err, "Soniox India key authenticates", f"error {err}" if err else "")
    except Exception as e:                          # noqa: BLE001
        check(False, "Soniox India key authenticates", f"{type(e).__name__}: {e}"[:80])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    print("PRE-CALL GATE")
    if not a.skip_tests:
        gate_tests()
    for step in (gate_deploy, gate_config_and_prompt, gate_voice_and_ears):
        try:
            step()
        except Exception as exc:                    # noqa: BLE001
            check(False, f"{step.__name__} ran", f"{type(exc).__name__}: {exc}"[:120])
    bad = [label for ok, label, _ in _results if not ok]
    print(f"\n{len(_results) - len(bad)}/{len(_results)} green")
    if bad:
        print("GATE: RED -- do not call. Failing: " + "; ".join(bad))
        return 1
    print("GATE: GREEN -- safe to call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
