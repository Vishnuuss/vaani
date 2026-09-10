"""The production container has no torch. Prove the model still runs.

The bug this locks out
----------------------
`api/Dockerfile` installs pipecat with a long list of extras and **not**
`local-smart-turn`. That extra is the only thing in the tree that pulls in
`torch`, `torchaudio` and `transformers`. `onnxruntime` and `soxr` are pipecat
BASE dependencies and are always present.

So on the server `import torch` raises ModuleNotFoundError.

Both the loader and the scorer used to import torch unconditionally -- the
loader to read one float out of the .pt, the scorer at the top of its `try`.
Either would have raised, been swallowed by the surrounding `except`, and left
the agent running **prosody** while the stored config said `audio-native`.

That failure has the worst possible shape: no crash, no traceback, a warning
line that looks like ordinary fallback chatter, and an agent quietly running
the model we replaced. It would have survived a deploy, a test call, and any
amount of reading the config back.

These tests import the module with torch made unavailable, exactly as the
container has it.
"""

import builtins
import sys

import numpy as np
import pytest

_BLOCKED = ("torch", "torchaudio", "transformers")


class _NoTorch:
    """Make the torch family unimportable for the duration of the block."""

    def __enter__(self):
        self._real = builtins.__import__
        self._saved = {k: v for k, v in sys.modules.items()
                       if k.split(".")[0] in _BLOCKED}
        for k in self._saved:
            del sys.modules[k]

        def guard(name, *a, **kw):
            if name.split(".")[0] in _BLOCKED:
                raise ModuleNotFoundError(f"No module named '{name}'")
            return self._real(name, *a, **kw)

        builtins.__import__ = guard
        return self

    def __exit__(self, *exc):
        builtins.__import__ = self._real
        sys.modules.update(self._saved)
        return False


def _fresh_runtime():
    """`_runtime` caches in a module global; clear it so the load is real."""
    import api.services.vaani.audio_native_turn as ant

    ant._RUNTIME = None
    return ant


def test_the_sidecar_carries_the_threshold():
    """A threshold is one number and must not need a tensor library to read."""
    import json

    from api.services.vaani.audio_native_turn import MODEL_PATH

    side = MODEL_PATH.with_suffix(".json")
    if not side.exists():
        pytest.skip("no exported model on this checkout")
    meta = json.loads(side.read_text(encoding="utf-8"))
    assert 0.0 < float(meta["threshold"]) < 1.0
    # The threshold the head was actually selected at. A drift here means the
    # sidecar and the graph came from different training runs.
    assert abs(float(meta["threshold"]) - 0.936) < 0.01


def test_the_model_loads_with_no_torch():
    from api.services.vaani.audio_native_turn import MODEL_PATH

    if not MODEL_PATH.with_suffix(".onnx").exists():
        pytest.skip("no exported ONNX graph on this checkout")

    with _NoTorch():
        ant = _fresh_runtime()
        model, thr = ant._runtime()
        assert isinstance(model, ant._Onnx), (
            "fell back off the ONNX path with torch absent -- on the server "
            "this is a silent no-op that runs prosody under an audio-native "
            "config")
        assert 0.0 < thr < 1.0
    _fresh_runtime()


def test_scoring_does_not_need_torch_either():
    """The loader being clean is not enough; `_probability` runs per turn."""
    from api.services.vaani.audio_native_turn import MODEL_PATH

    if not MODEL_PATH.with_suffix(".onnx").exists():
        pytest.skip("no exported ONNX graph on this checkout")

    with _NoTorch():
        ant = _fresh_runtime()
        from api.services.vaani.telugu_turn import TeluguTurnParams

        a = ant.AudioNativeTurnAnalyzer(
            sample_rate=8000, params=TeluguTurnParams())
        assert a.enabled

        # One second of 8 kHz speech-shaped noise is enough to exercise the
        # whole path: resample -> mel -> session.run.
        rng = np.random.default_rng(0)
        audio = (rng.normal(0, 2000, 8000 * 3)).astype(np.int16)
        a._buffer = [(0.0, audio)]

        p = a._probability()
        assert p is not None, "scoring fell through to prosody without torch"
        assert 0.0 <= p <= 1.0
    _fresh_runtime()
