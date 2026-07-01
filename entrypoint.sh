#!/bin/sh
# entrypoint.sh

mkdir -p /app/state

# Salva le variabili d'ambiente (cron gira con ambiente minimale)
printenv > /app/state/.env_runtime
chmod 600 /app/state/.env_runtime

LOG=/app/state/monitor.log
echo "========================================" >> "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Container avviato — installazione crontab" >> "$LOG"

# Installa il crontab per root direttamente dal file del progetto.
# Più affidabile di /etc/cron.d/ nei container slim.
crontab /app/crontab
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Crontab attivo:" >> "$LOG"
crontab -l >> "$LOG"
echo "========================================" >> "$LOG"

exec cron -f