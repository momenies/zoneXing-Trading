# Cloud Run Job image for the zoneXing backtest.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_DIR=/app

WORKDIR /app

COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements.txt

COPY backtest/ /app/backtest/
COPY zoneXing_Trading_signal_engine.py /app/
COPY run_backtest.py /app/
COPY deploy/entrypoint.py /app/deploy/entrypoint.py

# Run as a non-root user; the job needs no write access to the image.
RUN useradd --create-home --uid 1001 runner && chown -R runner:runner /app
USER runner

ENTRYPOINT ["python3", "/app/deploy/entrypoint.py"]
