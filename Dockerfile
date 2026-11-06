FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LIVE_TRADING_ENABLED=false \
    BROKER_MODE=paper \
    RISK_PROFILE=aggressive_growth

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
COPY trading ./trading
COPY apps ./apps

RUN pip install --upgrade pip && pip install -e .

EXPOSE 8080

# Default command is the API; Compose overrides for the worker.
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
