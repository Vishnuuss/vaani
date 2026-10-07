"""Audio constants and framing for the single-format pipeline.

The whole pipeline runs mu-law 8kHz mono, end to end, with ZERO transcoding:

    Vobiz (audio/x-mulaw 8000)
      -> Sarvam STT (encoding=mulaw, sample_rate=8000)
      -> LLM (text)
      -> Cartesia TTS (pcm_mulaw, 8000)
      -> Vobiz (audio/x-mulaw 8000)

All four hops were verified against the live APIs on 2026-08-25. Do not
introduce a resample anywhere without re-reading vaani/bench/FINDINGS.md -- every
conversion costs latency and CPU and buys nothing.
"""

from __future__ import annotations

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000   # 160 bytes of mu-law

# Wire format identifiers, per vendor. Kept here so a change is a one-line edit.
VOBIZ_CONTENT_TYPE = "audio/x-mulaw"
CARTESIA_OUTPUT_FORMAT = {
    "container": "raw",
    "encoding": "pcm_mulaw",
    "sample_rate": SAMPLE_RATE,
}
SARVAM_ENCODING = "mulaw"

# mu-law silence. 0xFF is the mu-law encoding of zero amplitude; 0x00 is NOT
# silence in mu-law and produces full-scale noise if used by mistake.
MULAW_SILENCE = 0xFF


def frames(audio: bytes, frame_bytes: int = FRAME_BYTES):
    """Split a buffer into fixed-size frames, padding the tail with silence.

    Vobiz recommends 20-60ms chunks. Sending a short final frame is legal but
    makes downstream duration maths drift, so we pad instead.
    """
    for offset in range(0, len(audio), frame_bytes):
        chunk = audio[offset:offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk += bytes([MULAW_SILENCE]) * (frame_bytes - len(chunk))
        yield chunk


def duration_ms(audio: bytes) -> float:
    """Playback duration of a mu-law buffer, in milliseconds."""
    return len(audio) / SAMPLE_RATE * 1000.0


def energy(frame: bytes) -> float:
    """Rough loudness of a mu-law frame, 0.0-1.0, for barge-in detection.

    mu-law is logarithmic and biased so that 0xFF is silence and values further
    from 0xFF are louder. We do not decode to linear PCM -- this is only ever
    compared against a threshold, and skipping the decode keeps the hot path
    free of per-sample maths.
    """
    if not frame:
        return 0.0
    total = sum(abs(byte - MULAW_SILENCE) for byte in frame)
    return min(1.0, total / (len(frame) * 127.0))
