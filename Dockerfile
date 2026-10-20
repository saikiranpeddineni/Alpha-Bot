FROM python:3.12.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    ALPHADESK_HOST=0.0.0.0

WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir --requirement requirements.lock

COPY backend ./backend
COPY .env.example ./.env.example

RUN mkdir -p /app/data

EXPOSE 8742

CMD ["python", "backend/run_server.py"]
