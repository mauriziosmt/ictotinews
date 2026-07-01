#!/bin/sh
# run.sh
#
# Wrapper invocato da cron ad ogni esecuzione pianificata.

ENV_FILE=/app/state/.env_runtime
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERRORE: $ENV_FILE non trovato." \
         >> /app/state/monitor.log
    exit 1
fi

# Imposta la working directory a /app: monitor.py usa path relativi
# (state/last_seen.json) che devono risolversi rispetto a /app, non a /
cd /app

LOG=/app/state/monitor.log

echo "" >> "$LOG"
echo "--- [$(date '+%Y-%m-%d %H:%M:%S')] Run avviato ---" >> "$LOG"

python /app/monitor.py >> "$LOG" 2>&1
STATUS=$?

echo "--- [$(date '+%Y-%m-%d %H:%M:%S')] Run terminato (exit: $STATUS) ---" >> "$LOG"

exit "$STATUS"