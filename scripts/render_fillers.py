"""Pre-render the cover audio bank. Run ONCE per voice, before any calls.

These clips are what make dead air impossible: when the real response is slow,
the watchdog plays one of these from disk. No network, no TTS latency, no
failure mode. They also cost nothing after today -- the characters are paid for
once, not on every call.

    python scripts/render_fillers.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vaani.core.filler import FillerBank, PHRASES  # noqa: E402
from vaani.core.tts import CartesiaTTS  # noqa: E402


def load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    load_env()
    voice = os.environ.get("CARTESIA_VOICE_ID")
    if not voice:
        print("CARTESIA_VOICE_ID not set in .env")
        return 1

    total = sum(len(v) for v in PHRASES.values())
    print(f"rendering up to {total} clips for voice {voice} "
          f"({os.environ.get('CARTESIA_MODEL_ID', 'sonic-3')}) ...")

    bank = FillerBank(voice_id=voice).load()
    async with CartesiaTTS() as tts:
        written = await bank.render(tts)

    print(f"  {written} new clip(s) written; bank ready: {bank.ready}")
    for kind, phrases in PHRASES.items():
        have = len(bank._clips.get(kind, []))
        print(f"  {kind.value:9} {have}/{len(phrases)}")
    return 0 if bank.ready else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
