"""Telugu fillers: something to hear while the answer is still being built.

The problem, measured
---------------------
Run 262 (2026-08-28, a clean 130s call):

    endpoint + STT   0.921s average      <- the caller is listening to silence
    LLM              0.290s
    TOTAL p50        1.297s

The LLM is not the wait any more. Two thirds of every gap is spent deciding the
caller has finished and finalising their transcript, and during all of it the
line is dead. A human agent does not do that. They say "సరే..." the moment you
stop, and you never notice the pause while they think.

That is what this is. It does not make the answer arrive sooner; it removes the
silence in front of it, which is the part the caller actually experiences.

Why this costs no tokens
------------------------
The obvious implementation -- ask the model to open with a filler -- is the
wrong one twice over: it is billed on every turn, and it cannot be spoken until
the model has already responded, which is the very thing being waited for.

So fillers never touch the prompt, the context or the LLM. Each one is
synthesised ONCE through the live TTS voice, cached on disk as raw PCM, and
played straight to the transport. Marginal cost per call: zero tokens, zero TTS
characters, one memcpy. That is the whole point of the design.

When it fires, and why that is the hard part
--------------------------------------------
A filler played while the caller is drawing breath is an interruption, and this
project has a standing rule that a latency win bought with interruptions is not
a win (`latency_budget.yaml`: max_false_interruption_rate 0.02). Telugu callers
on the incumbent protested at 350ms.

So it is gated on the Telugu turn detector already trained on this agent's own
calls -- the same model, the same 2% false-cutoff bar, read at the moment the
VAD reports a pause. Confident the caller has finished: speak. Not confident:
stay silent and lose nothing. The failure mode is a missing filler, never a
filler on top of the caller.

Ordering is free
----------------
The filler is pushed to the transport, which plays audio in the order it is
queued. The real reply lands behind it rather than on top of it, so no mixing,
no ducking, no cancellation logic. `strip_leading_ack` exists because the state
block also asks the model to open with "సరే" -- without it the caller hears
"సరే... సరే, మీ బిల్లు".
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from loguru import logger

CACHE_DIR = Path(__file__).parent / "models" / "filler_audio"

# Real Telugu call-centre fillers, not translations of English ones.
#
# The four marked "harvested" are cut from the agent's OWN recorded voice by
# tools/harvest_fillers.py, which is better than a fresh TTS render and not just
# a workaround for the missing key: they are already at 8 kHz, already through
# the phone codec, and already carry the line character of a real call. A
# studio-clean render would audibly not match the sentence that follows it.
#
# The rest have no clip yet and are simply never played. A filler with no audio
# is skipped, not faked.
#
# Every entry is a *continuation* -- it promises the sentence is coming. Words
# that can stand as a complete reply are excluded on purpose: "సరే." alone
# sounds like the agent finished talking, and the caller starts speaking into
# the answer.
#
# Kept short. This is audio the caller sits through on every gated turn, so a
# long filler spends the very budget it was added to protect.
FILLERS: tuple[str, ...] = (
    "సరే",              # "alright"        <- harvested
    "మంచిది",           # "good"           <- harvested
    "అర్థమైంది",         # "understood"     <- harvested
    "అవును",            # "yes"            <- harvested
    "అలాగే",            # "very well"
    "ఒక్క నిమిషం",       # "one moment"
    "చూద్దాం",           # "let us see"
)

# Openers the model is told to use by the state block. If a filler has just
# played, these are removed from the front of the reply so the caller does not
# hear the same word twice in a row.
# The honorific is part of the greeting, not the sentence: leaving "సార్,"
# stranded at the front of the reply reads worse than the duplicate did.
_LEADING_ACK = re.compile(
    r"^\s*(సరే|అలాగే|మంచిది|అర్థమైంది|అవునండి|ఓకే|ఆc)"
    r"(\s*(అండి|సార్|ండి|గారు|మేడమ్))?"
    r"\s*[,\-–—.!]*\s*",
)


def strip_leading_ack(text: str) -> str:
    """Drop one leading acknowledgement, because the filler already said it.

    One only. A reply that genuinely begins "సరే, సరే" is the model stammering
    and the second one is worth keeping as evidence rather than hiding.
    """
    stripped = _LEADING_ACK.sub("", text or "", count=1)
    # Never hand back an empty reply: if the acknowledgement WAS the whole
    # reply, the caller is better served hearing it than hearing nothing.
    return stripped if stripped.strip() else (text or "")


# Clips cut from the agent's own recordings are stored under this name instead
# of a voice id, and are used whatever voice is configured.
#
# That is not a shortcut. A harvested clip IS the live voice -- it was recorded
# off a real call by this agent -- so keying it to a provider's voice id would
# be recording a fact that is already true in a form that can go stale. It also
# survives the provider being wrong: this pipeline was believed to run Sarvam
# "anushka" and actually runs Cartesia sonic-3.5, which would have keyed every
# clip to a voice that never appears at runtime and left fillers silently off.
#
# The consequence, stated so it is not forgotten: CHANGING THE AGENT'S VOICE
# REQUIRES RE-HARVESTING. Run tools/harvest_fillers.py against a call made with
# the new voice, or the caller hears the old one.
HARVESTED = "harvested"


def cache_path(text: str, voice: str, sample_rate: int) -> Path:
    """Where one rendered filler lives.

    Keyed by voice and sample rate as well as text: a cached clip in the wrong
    voice is worse than no filler, because the caller hears two different people.
    """
    key = f"{text}|{voice}|{sample_rate}".encode("utf-8")
    return CACHE_DIR / f"{hashlib.sha256(key).hexdigest()[:16]}.pcm"


def load_cached(text: str, voice: str, sample_rate: int) -> bytes | None:
    """A clip for this filler: the agent's own harvested voice first."""
    for key in (HARVESTED, voice):
        try:
            data = cache_path(text, key, sample_rate).read_bytes()
        except OSError:
            continue
        if len(data) >= int(0.04 * sample_rate) * 2:
            return data
        return None
    return None



def store_cached(text: str, voice: str, sample_rate: int, pcm: bytes) -> None:
    p = cache_path(text, voice, sample_rate)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(pcm)
    except OSError as e:
        # A read-only filesystem must not take a call down; it only means the
        # clip is re-rendered next time.
        logger.warning(f"[filler] could not cache {text!r}: {e!r}")
