"""Where the filler player sits, and why it can only sit there.

The filler is raw audio pushed toward the transport. Its position decides
whether it works at all:

  before `tts`              it has to survive a processor built for text
  after `transport.output()` nothing downstream plays it, so it is never heard
  between the two           the transport queues it, and the real reply plays
                            after it rather than on top of it

That last property is what removes the need for ducking, mixing or cancellation
logic anywhere in this feature -- so it is worth a test rather than a comment.
"""

from __future__ import annotations

from api.services.vaani.pipeline import vaani_processor_order


class P:
    def __init__(self, name): self.name = name
    def __repr__(self): return self.name


class Transport:
    def __init__(self): self._in, self._out = P("transport.input"), P("transport.output")
    def input(self): return self._in
    def output(self): return self._out


def order(**kw):
    t = Transport()
    names = vaani_processor_order(
        t, P("stt"), P("audio_buffer"), P("llm"), P("tts"),
        P("user_agg"), P("assistant_agg"), P("engine_cb"), P("metrics"), **kw)
    return [p.name for p in names]


def test_the_filler_sits_between_speech_and_the_transport():
    names = order(filler_player=P("filler"))
    assert names.index("tts") < names.index("filler") < names.index("transport.output")


def test_the_pipeline_is_unchanged_when_there_is_no_filler():
    """Fillers are optional; their absence must not move anything else."""
    assert order() == [n for n in order(filler_player=P("filler")) if n != "filler"]


def test_the_filler_does_not_displace_the_brain():
    names = order(filler_player=P("filler"), state_injector=P("injector"),
                  reply_filter=P("reply_filter"))
    assert names.index("injector") < names.index("llm") < names.index("reply_filter")
    assert names.index("reply_filter") < names.index("filler")
