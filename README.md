# ictotinews

Bot Telegram che monitora il sito dell'IC "E. Toti" di Lentate sul Seveso e
manda una notifica per ogni nuova notizia, circolare, evento o atto dell'Albo online.

Scritto principalmente per tenere d'occhio le comunicazioni della scuola
dell'infanzia senza dover controllare il sito ogni giorno.

## Come funziona

Lo script controlla le quattro sezioni del sito:

- `/novita/notizie`
- `/novita/circolari`
- `/novita/calendario-eventi`
- `/novita/albo-pretorio`

Per ogni nuova pubblicazione manda un messaggio Telegram con titolo, data,
destinatari (quando disponibili) e link. Se il titolo o i destinatari
menzionano la scuola dell'infanzia, aggiunge un'etichetta 🟢 per riconoscerle
a colpo d'occhio.

Lo stato viene salvato in `state/last_seen.json`: al primo avvio tutto il
contenuto attuale viene marcato come già visto, così non ricevi una valanga
di notifiche retroattive.

## Modalità debug locale

Imposta nel tuo `.env`:

```dotenv
DEBUG_MODE=true
```

In questa modalità Telegram è completamente disabilitato, le credenziali non
sono obbligatorie e `state/last_seen.json` non viene modificato. Ogni elemento
trovato viene invece stampato nel log come messaggio simulato:

```bash
docker compose --profile test run --rm ic-lentate-alert-test
```

## Prerequisiti

- Docker (per il dev locale)
- Un bot Telegram e il relativo token (da [@BotFather](https://t.me/BotFather))
- L'ID della chat o canale su cui ricevere le notifiche

## Setup

```bash
cp env.example .env
# compila TELEGRAM_TOKEN e TELEGRAM_CHAT nel file .env
```

Esecuzione singola:

```bash
docker compose run --rm ic-lentate-alert
```

Se preferisci tenerlo in esecuzione continua in locale (controllo ogni 2 ore):

```bash
docker compose --profile loop up -d ic-lentate-alert-loop
```

## Deploy con GitHub Actions

Il workflow in `.github/workflows/monitor.yml` esegue lo script ogni 2 ore
e committa automaticamente il file di stato aggiornato.

Per attivarlo:

1. Aggiungi `TELEGRAM_TOKEN` e `TELEGRAM_CHAT` nei secret del repository
   (*Settings → Secrets and variables → Actions*)
2. Abilita i permessi di scrittura per i workflow
   (*Settings → Actions → General → Workflow permissions → Read and write*)
3. Lancia il primo run manualmente dalla tab *Actions* per verificare che
   tutto funzioni

> **Nota**: il sito della scuola blocca le richieste provenienti da alcuni
> range IP dei datacenter cloud. Se i runner di GitHub Actions finiscono in
> uno di questi range, lo script non riesce a raggiungere il sito. In quel
> caso la soluzione più affidabile è eseguirlo su un VPS con IP non
> classificato come datacenter.

## Struttura

```
.
├── monitor.py                 # script principale
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── env.example
├── state/
│   └── last_seen.json         # URL già notificati (committato nel repo)
└── .github/
    └── workflows/
        └── monitor.yml        # GitHub Action schedulata ogni 2 ore
```

## Note

Il sito è un Drupal "Porte Aperte sul Web" (tema Bootstrap Italia), senza
API pubblica. I campi disponibili nelle pagine lista sono titolo, data e
destinatario testuale per le notizie; per le circolari c'è anche la
tassonomia destinatari (Docenti / Famiglie / Personale ATA / Alunni).
Campi come "plesso" o "ordine di scuola" non esistono come dati strutturati,
quindi non compaiono nei messaggi.
