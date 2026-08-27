"""Where do the three vendors actually answer from?

research/latency.md ranks this #1 by (ms saved)/(risk): 100-250ms for an hour of
work, if any vendor is being served from the US when an India/Singapore region
exists. Nothing in this project has ever measured it.

Measures TCP connect time (not ICMP -- most of these sit behind clouds that drop
ping) and the TLS handshake, which together are ~2 round trips and therefore a
decent proxy for RTT.
"""
from __future__ import annotations

import socket, ssl, statistics, sys, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOSTS = [
    ("Groq        (LLM)",     "api.groq.com"),
    ("Cartesia    (TTS)",     "api.cartesia.ai"),
    ("Sarvam      (STT)",     "api.sarvam.ai"),
    ("Smallest.ai (TTS alt)", "waves-api.smallest.ai"),
    ("ElevenLabs  (alt)",     "api.elevenlabs.io"),
    ("Deepgram    (alt)",     "api.deepgram.com"),
]


def probe(host: str, reps: int = 5) -> dict:
    tcp, tls, ips = [], [], set()
    for _ in range(reps):
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            addr = infos[0][4]
            ips.add(addr[0])
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            t0 = time.perf_counter()
            s.connect(addr)
            t1 = time.perf_counter()
            ctx = ssl.create_default_context()
            w = ctx.wrap_socket(s, server_hostname=host)
            t2 = time.perf_counter()
            tcp.append(t1 - t0)
            tls.append(t2 - t1)
            w.close()
        except Exception as e:
            print(f"    {host}: {e!r}")
    if not tcp:
        return {}
    return {"tcp": statistics.median(tcp), "tls": statistics.median(tls),
            "ips": sorted(ips)}


print(f"{'vendor':<24} {'TCP connect':>12} {'TLS':>8} {'~1 RTT':>8}   resolved IP")
print("-" * 78)
for label, host in HOSTS:
    r = probe(host)
    if not r:
        print(f"{label:<24}  unreachable")
        continue
    print(f"{label:<24} {r['tcp']*1000:10.1f}ms {r['tls']*1000:6.1f}ms "
          f"{r['tcp']*1000:6.1f}ms   {', '.join(r['ips'][:2])}")

print("\nRule of thumb from India: <40ms = Mumbai/India, ~60-90ms = Singapore,")
print("~200-260ms = US east/west. A US-served vendor on the critical path is")
print("paying ~220ms per round trip that an India region would not.")
