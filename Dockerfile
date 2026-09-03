FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY live/ ./live/
COPY zoneXing_Trading_signal_engine.py ./

RUN useradd -m -u 10001 zonexing && mkdir -p /app/state && chown -R zonexing /app
USER zonexing

ENV STATE_DIR=/app/state

# Fails the build if the engine is not causal / violates the gate.
RUN python -m live.trader --selftest

CMD ["python", "-m", "live.trader"]
