"""Raw network RTT probes.

This directly tests Finding 2 in the plan: Groq is in the US, and from Mumbai
that round trip is the single largest fixed cost on the critical path. If the
Groq RTT here comes back at ~200ms+ while Sarvam and Cartesia are low, that is
the empirical proof that speculative execution is load-bearing rather than a
nice-to-have.
"""

from __future__ import annotations

import socket
import ssl
import time

from .report import Section

# host -> (label, budget_ms). Budgets are "what we'd need for a serial design
# to work"; blowing them is the argument FOR speculation, not a failure.
HOSTS = {
    "api.groq.com": ("Groq (LLM)", 60.0),
    "api.cartesia.ai": ("Cartesia (TTS)", 60.0),
    "api.sarvam.ai": ("Sarvam (STT)", 40.0),
}


def _tls_rtt(host: str, port: int = 443, timeout: float = 5.0) -> float:
    """Full TCP+TLS handshake time in ms.

    We measure TCP+TLS rather than ICMP because that is what a cold HTTPS
    request actually pays, and because ICMP is frequently rate-limited or
    dropped by cloud edges, which would give us a flattering fake number.
    """
    ctx = ssl.create_default_context()
    start = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host):
            pass
    return (time.perf_counter() - start) * 1000.0


def run(samples: int = 10) -> Section:
    sec = Section(title="NETWORK -- TCP+TLS handshake RTT from this machine")
    for host, (label, budget) in HOSTS.items():
        m = sec.metric(f"{label:<18} {host}", budget=budget)
        for _ in range(samples):
            try:
                m.add(_tls_rtt(host))
            except Exception as exc:  # noqa: BLE001 - report, never abort the suite
                m.note = f"unreachable: {type(exc).__name__}"
                break
            time.sleep(0.05)
    sec.metrics.append(
        _interpretation(sec)
    )
    return sec


def _interpretation(sec: Section):
    """Turn the raw RTTs into the one sentence that actually matters.

    CAVEAT, and it is a big one: this measures the TLS handshake, which for
    Groq and Cartesia terminates at a nearby anycast/CDN edge -- NOT at the
    inference cluster. So these numbers are a FLOOR, not the real round trip a
    completion pays. The honest LLM RTT is groq_bench's t_first_FINAL minus
    Groq's own compute. Treat this section as "is the network sane", and let
    the Groq suite decide the speculation question.
    """
    from .report import Metric, pct

    groq = next((m for m in sec.metrics if "Groq" in m.name), None)
    note = "run from the Mumbai box for a meaningful number"
    if groq and groq.samples:
        p50 = pct(groq.samples, 50)
        note = (
            f"Groq TLS-edge p50 {p50:.0f}ms -- this is a FLOOR (anycast edge, not "
            f"the inference cluster). Real LLM RTT is higher; see the Groq suite's "
            f"t_first_FINAL. Speculation is judged there, not here."
        )
    return Metric(name="=> VERDICT", note=note)
