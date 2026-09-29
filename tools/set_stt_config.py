"""Change the organization's STT / LLM / TTS provider settings — key, model, language.

Named for STT because that is what it was written for; `--section llm` and
`--section tts` were added on 3 Sep when the server's Groq key turned out to
differ from the one in .env and there was no way to compare, let alone fix it.
All three live in the same `byok.pipeline` object and need the same masked-secret
merge, so one tool covers them.

Why a tool and not a curl
-------------------------
The GET returns keys MASKED (`"*****5nyO"`), and the PUT takes the whole
pipeline. Round-tripping the GET naively would write the mask string over the
LLM and TTS keys and take the agent off the air in a way that looks like a
successful save.

That is survivable only because the server is built for it:
`save_model_configuration_v2` runs `merge_ai_model_configuration_v2_secrets`,
which restores any masked secret from the stored row, and then
`check_for_masked_keys_in_ai_model_configuration_v2`, which refuses the write if
one is still masked. So the correct move is to send the GET back unchanged
except for the field being edited, and let the merge do its job -- which is what
this does, and what a hand-written curl would get wrong.

    python tools/set_stt_config.py                          # show current
    python tools/set_stt_config.py --api-key sk_... --apply
    python tools/set_stt_config.py --model saaras:v3-realtime --apply
    python tools/set_stt_config.py --section llm            # show the LLM's
    python tools/set_stt_config.py --section llm --api-key gsk_... --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vaani_runs as V  # noqa: E402

PATH = "/api/v1/organizations/model-configurations/v2"


def put(body: dict):
    req = urllib.request.Request(
        f"{V.BASE}{PATH}", method="PUT",
        data=json.dumps(body).encode("utf-8"),
        headers={"X-API-Key": V.KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fingerprint(value) -> str:
    """Enough of a key to tell two apart, never enough to use one."""
    if isinstance(value, list):
        value = value[0] if value else ""
    value = str(value or "")
    return f"...{value[-4:]} ({len(value)} chars)" if value else "(unset)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", choices=("stt", "llm", "tts"), default="stt")
    # Added 19 Sep to put the ear back on Sarvam. Soniox went in at the ORG
    # level on 18 Sep and measured slower live than the Sarvam it replaced
    # -- endpoint 1.418s against 0.906s -- and there was no way to move the
    # provider back without hand-rolling the PUT, which this file exists to
    # stop anyone doing. Changing provider almost always means changing the
    # key in the same write: authenticating Sarvam with a Soniox key fails
    # as SILENCE, indistinguishable from the turn-detection bugs this
    # project spends its life chasing.
    ap.add_argument("--provider")
    ap.add_argument("--api-key")
    ap.add_argument("--model")
    ap.add_argument("--language")
    # TTS only. Added 6 Sep when Cartesia credits ran out and the account had to
    # be swapped: a new account means a new key AND a new voice id, and changing
    # the key alone leaves the agent authenticating fine against a voice that
    # does not exist on it -- which fails as SILENCE, indistinguishable from the
    # turn-detection bug being chased at the time.
    ap.add_argument("--voice")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    # The GET returns {configuration, effective_configuration, source}. The PUT
    # takes the FIRST of those. `effective_configuration` is the compiled view
    # -- it renders api_key as a LIST and carries derived fields the request
    # model rejects, which is a 422 that says nothing about which half was
    # wrong.
    # Shape, which is worth stating because getting it wrong returns a 200 that
    # changes nothing: {version, mode, byok:{mode, pipeline:{llm,tts,stt}}}.
    # An earlier version of this tool wrote the pipeline to the top level; the
    # server merged, saved, and reported success, and the key was untouched.
    cfg = V.get(PATH)["configuration"]
    pipeline = (cfg.get("byok") or {}).get("pipeline") or {}
    stt = pipeline.get(a.section) or {}
    print(f"section     {a.section}")
    print(f"{'provider':<12}{stt.get('provider')}")
    print(f"{'model':<12}{stt.get('model')}")
    print(f"{'language':<12}{stt.get('language')}")
    print(f"{'api_key':<12}{fingerprint(stt.get('api_key'))}")
    if stt.get("voice"):
        print(f"{'voice':<12}{stt.get('voice')}")

    changes = {}
    if a.provider and a.provider != stt.get("provider"):
        changes["provider"] = a.provider
    if a.api_key and fingerprint(a.api_key) != fingerprint(stt.get("api_key")):
        changes["api_key"] = a.api_key
    if a.model and a.model != stt.get("model"):
        changes["model"] = a.model
    if a.language and a.language != stt.get("language"):
        changes["language"] = a.language
    if a.voice and a.voice != stt.get("voice"):
        changes["voice"] = a.voice

    if not changes:
        print("\nNothing to change.")
        return 0
    print("\nwill change: " + ", ".join(
        f"{k}={fingerprint(v) if k == 'api_key' else v}" for k, v in changes.items()))
    if not a.apply:
        print("Re-run with --apply to write.")
        return 0

    stt.update(changes)
    pipeline[a.section] = stt
    cfg["byok"]["pipeline"] = pipeline
    put(cfg)

    live = (((V.get(PATH)["configuration"].get("byok") or {})
             .get("pipeline")) or {}).get(a.section) or {}
    bad = {k: (live.get(k), v) for k, v in changes.items()
           if k != "api_key" and live.get(k) != v}
    if "api_key" in changes and len(str(live.get("api_key") or "")) != len(a.api_key):
        # The read-back is masked, so the LENGTH is the only thing that proves
        # the write landed. Same length by coincidence is possible; silently
        # writing nothing is not worth the risk of missing.
        bad["api_key"] = (f"{len(str(live.get('api_key') or ''))} chars",
                          f"{len(a.api_key)} chars")
    if bad:
        print(f"\nWROTE BUT DID NOT STICK: {bad}")
        return 1
    print(f"\napplied. now: provider={live.get('provider')} model={live.get('model')} "
          f"language={live.get('language')} api_key={fingerprint(live.get('api_key'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
