FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installa cron
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

COPY monitor.py .
COPY run.sh .
COPY entrypoint.sh .

# La directory di stato viene montata come volume (vedi docker-compose.yml)
RUN mkdir -p /app/state

# Il crontab viene installato a runtime da entrypoint.sh (via `crontab /app/crontab`)
# Non usiamo /etc/cron.d/ per evitare problemi di formato nei container slim.
ENTRYPOINT ["sh", "/app/entrypoint.sh"]