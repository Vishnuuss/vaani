"""Cover audio bank and the silence watchdog.

THIS IS THE PIECE THAT STRUCTURALLY KILLS THE DOGRAH DEAD-AIR BUG, and it lives
in code, not in a prompt. The invariant:

    After the turn is committed, if no agent audio has started within 250ms,
    play cover audio. No exceptions, no prompt involved.

Cover clips are pre-synthesised ONCE per voice and stored as raw mu-law on disk.
Zero TTS latency, zero marginal TTS cost, and they cannot fail mid-call because
no network call is involved.

Budget discipline: cover should fire on under 25% of turns. Higher than that
means the endpointer or the speculator has regressed -- it is an alarm, not a
crutch.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

BANK_DIR = Path(__file__).resolve().parents[1] / "assets" / "fillers"
WATCHDOG_MS = 250


class Cover(Enum):
    """Why we are covering. Each maps to a different set of clips."""
    THINKING = "thinking"        # speculation miss -- a breath, an acknowledgement
    WORKING = "working"          # tool call in flight
    TROUBLE = "trouble"          # vendor error, retrying
    CLOSING = "closing"          # total failure -- graceful exit


# Text is only used to RENDER the bank ahead of time; at call time we play files.
PHRASES: dict[Cover, tuple[str, ...]] = {
    Cover.THINKING: (
        "హా,", "సరే,", "అలాగే,", "అవునా,", "మ్మ్,",
    ),
    Cover.WORKING: (
        "ఒక్క సెకను సార్...",
        "చూస్తున్నాను సార్, ఒక్క నిమిషం...",
        "అది చెక్ చేస్తున్నాను...",
    ),
    Cover.TROUBLE: (
        "ఒక్క నిమిషం సార్, లైన్ కొంచెం...",
        "క్షమించండి సార్, కొంచెం ఆగండి...",
    ),
    Cover.CLOSING: (
        "సార్, లైన్ సరిగ్గా లేదు. నేను మళ్ళీ కాల్ చేస్తాను, సరేనా?",
    ),
}


def _clip_path(voice_id: str, kind: Cover, text: str) -> Path:
    digest = hashlib.sha1(f"{voice_id}|{text}".encode()).hexdigest()[:12]
    return BANK_DIR / voice_id / kind.value / f"{digest}.raw"


@dataclass
class FillerBank:
    """Pre-rendered cover clips for one voice."""

    voice_id: str
    _clips: dict[Cover, list[bytes]] = field(default_factory=dict)
    _last_played: dict[Cover, int] = field(default_factory=dict)

    def load(self) -> "FillerBank":
        for kind, phrases in PHRASES.items():
            found = []
            for text in phrases:
                path = _clip_path(self.voice_id, kind, text)
                if path.exists():
                    found.append(path.read_bytes())
            self._clips[kind] = found
        return self

    @property
    def ready(self) -> bool:
        return any(self._clips.get(k) for k in Cover)

    def pick(self, kind: Cover) -> bytes | None:
        """Choose a clip, avoiding an immediate repeat of the last one.

        Hearing the same "హా," twice in a row is worse than a slightly longer
        pause -- it is the tell that gives away a machine.
        """
        clips = self._clips.get(kind) or self._clips.get(Cover.THINKING) or []
        if not clips:
            return None
        if len(clips) == 1:
            return clips[0]
        last = self._last_played.get(kind, -1)
        choices = [i for i in range(len(clips)) if i != last]
        idx = random.choice(choices)
        self._last_played[kind] = idx
        return clips[idx]

    async def render(self, tts) -> int:
        """Synthesise every missing clip once. Run at setup, never during a call."""
        written = 0
        for kind, phrases in PHRASES.items():
            for text in phrases:
                path = _clip_path(self.voice_id, kind, text)
                if path.exists():
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                audio = await tts.synthesize(text, context_id=f"filler-{written}")
                path.write_bytes(audio)
                written += 1
        self.load()
        return written


class SilenceWatchdog:
    """Fires cover audio if real audio has not started in time.

    Usage:
        async with watchdog.guard(Cover.THINKING):
            await produce_and_send_real_audio()

    `notify_audio_started()` disarms it. If the guarded block does not call that
    within WATCHDOG_MS, `on_cover` is invoked with a clip.
    """

    def __init__(self, bank: FillerBank, on_cover, threshold_ms: int = WATCHDOG_MS):
        self.bank = bank
        self.on_cover = on_cover
        self.threshold_ms = threshold_ms
        self._audio_started = asyncio.Event()
        self.covers_fired = 0
        self.turns = 0

    def notify_audio_started(self) -> None:
        self._audio_started.set()

    def guard(self, kind: Cover = Cover.THINKING):
        return _Guard(self, kind)

    @property
    def cover_rate(self) -> float:
        """Share of turns that needed cover. Alarm above 0.25."""
        return self.covers_fired / self.turns if self.turns else 0.0


class _Guard:
    def __init__(self, watchdog: SilenceWatchdog, kind: Cover):
        self.wd = watchdog
        self.kind = kind
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        self.wd.turns += 1
        self.wd._audio_started = asyncio.Event()
        self._task = asyncio.create_task(self._arm())
        return self

    async def _arm(self) -> None:
        try:
            await asyncio.wait_for(self.wd._audio_started.wait(),
                                   timeout=self.wd.threshold_ms / 1000.0)
        except asyncio.TimeoutError:
            clip = self.wd.bank.pick(self.kind)
            if clip:
                self.wd.covers_fired += 1
                await self.wd.on_cover(clip)

    async def __aexit__(self, *exc) -> None:
        self.wd._audio_started.set()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
