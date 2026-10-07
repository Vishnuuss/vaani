# Vaani gateway -- Option A (WebSocket transport, no LiveKit, no SIP stack).
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
      fastapi==0.115.* "uvicorn[standard]==0.32.*" httpx==0.27.* \
      "websockets>=14,<18" pyyaml==6.*

COPY vaani/ /app/vaani/
COPY clients/ /app/clients/

ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8

EXPOSE 8090
# One worker on purpose: a call holds in-process state (warm TTS socket, live
# CallState, in-flight speculations). Multiple workers would split that.
CMD ["uvicorn", "vaani.gateway.server:app", \
     "--host", "0.0.0.0", "--port", "8090", "--workers", "1", \
     "--log-level", "info", "--ws-ping-interval", "20"]
