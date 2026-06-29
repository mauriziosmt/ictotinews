FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

# Lo stato persiste su file (montato come volume in docker-compose.yml)
RUN mkdir -p /app/state

# Esecuzione "one-shot": un solo controllo e poi termina.
# In dev locale lo richiami manualmente o con un cron esterno al container;
# in produzione ci pensa la GitHub Action (vedi README.md).
CMD ["python", "monitor.py"]
