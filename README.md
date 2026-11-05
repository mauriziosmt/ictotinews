# ictotinews

Bot Telegram che monitora i siti di più Istituti Comprensivi e manda una
notifica per ogni nuova pubblicazione, ciascuna nel gruppo Telegram del
proprio istituto.

Nato per tenere d'occhio le comunicazioni della scuola dell'infanzia senza
dover controllare i siti ogni giorno.

## Istituti monitorati

| Istituto | Sito | Sezioni | Come vengono lette | Gruppo |
|---|---|---|---|---|
| IC "E. Toti" — Lentate sul Seveso | iclentate.edu.it (Drupal) | Notizie, Circolari, Calendario eventi, Albo online | parsing HTML delle pagine elenco | `TELEGRAM_CHAT_ICTOTI` |
| IC Via De Gasperi — Seveso | icviadegasperi.edu.it (WordPress) | Notizie, Circolari, Eventi, Documenti programmatici | feed RSS di WordPress | `TELEGRAM_CHAT_DEGASPERI` (default `@icdegasperisevesonews`) |

Un unico run controlla tutti gli istituti, uno dopo l'altro. Un errore su un
istituto o su una sezione non blocca gli altri; il messaggio di errore va nel
gruppo dell'istituto interessato.

## Come funziona

Per ogni nuova pubblicazione il bot manda un messaggio con titolo, data,
destinatari (quando disponibili) e link. Se il titolo o i destinatari
menzionano la scuola dell'infanzia, aggiunge un'etichetta 🟢 per riconoscerle
a colpo d'occhio.

Lo stato è un unico file `state/last_seen.json`, suddiviso per istituto e
sezione:

```json
{
  "ictoti":    { "notizie": ["https://..."], "circolari": [...], "eventi": [...], "albo": [...] },
  "degasperi": { "notizie": ["https://...?p=27764"], "circolari": [...], "eventi": [], "documenti": [...] }
}
```

- Per l'IC Toti la chiave è l'URL della pagina; per la De Gasperi è il `guid`
  del post WordPress (contiene l'ID del post, quindi non cambia se viene
  modificato il titolo).
- La prima volta che un istituto, o una singola sezione, viene controllato,
  tutto il contenuto attuale è marcato come già visto senza notifiche, così
  non arriva una valanga di messaggi retroattivi. All'avvio di un nuovo
  istituto il gruppo riceve un messaggio di benvenuto.
- Il vecchio formato del file (solo IC Toti, con le sezioni al primo livello)
  viene convertito automaticamente al primo run, senza perdere nulla.

### Aggiungere un istituto

Si aggiunge una voce al dizionario `INSTITUTES` in `monitor.py`. Se il sito è
un WordPress con tema Designers Italia, di solito basta usare sezioni
`"kind": "rss"` puntando a `<pagina elenco>/feed/`.

## Modalità debug locale

Imposta nel tuo `.env`:

```dotenv
DEBUG_MODE=true
```

In questa modalità Telegram è completamente disabilitato e le credenziali non
sono obbligatorie. Tutto il resto funziona come in produzione: il bootstrap e
il rilevamento delle novità aggiornano `state/last_seen.json`; ogni notifica
viene invece stampata nel log come messaggio simulato, con la chat di
destinazione:

```bash
docker compose --profile test run --rm ic-lentate-alert-test
```

## Prerequisiti

- Docker (per il dev locale)
- Un bot Telegram e il relativo token (da [@BotFather](https://t.me/BotFather)),
  unico per tutti gli istituti
- Il bot aggiunto a ciascun gruppo (come amministratore, se è un canale)

## Setup

```bash
cp env.example .env
# compila TELEGRAM_TOKEN, TELEGRAM_CHAT_ICTOTI e TELEGRAM_CHAT_DEGASPERI nel file .env
```

`TELEGRAM_CHAT` (la variabile della versione con un solo istituto) resta
valida come chat dell'IC Toti, quindi un `.env` esistente continua a
funzionare.

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

1. Aggiungi `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ICTOTI` (oppure il vecchio
   `TELEGRAM_CHAT`) e, se serve, `TELEGRAM_CHAT_DEGASPERI` nei secret del
   repository (*Settings → Secrets and variables → Actions*)
2. Abilita i permessi di scrittura per i workflow
   (*Settings → Actions → General → Workflow permissions → Read and write*)
3. Lancia il primo run manualmente dalla tab *Actions* per verificare che
   tutto funzioni

> **Nota**: il sito dell'IC Toti blocca le richieste provenienti da alcuni
> range IP dei datacenter cloud. Se i runner di GitHub Actions finiscono in
> uno di questi range, lo script non riesce a raggiungere il sito. In quel
> caso la soluzione più affidabile è eseguirlo su un VPS con IP non
> classificato come datacenter.

## Struttura

```
.
├── monitor.py                 # script principale (configurazione istituti in INSTITUTES)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── env.example
├── state/
│   └── last_seen.json         # identificativi già notificati, per istituto
└── .github/
    └── workflows/
        └── monitor.yml        # GitHub Action schedulata ogni 2 ore
```

## Note sui siti

**IC Toti** — Drupal "Porte Aperte sul Web" (tema Bootstrap Italia), senza
API pubblica. I campi disponibili nelle pagine elenco sono titolo, data e
destinatario testuale per le notizie; per le circolari c'è anche la
tassonomia destinatari (Docenti / Famiglie / Personale ATA / Alunni).
Campi come "plesso" o "ordine di scuola" non esistono come dati strutturati,
quindi non compaiono nei messaggi.

**IC De Gasperi** — WordPress con tema Designers Italia (Easyteam). I feed
RSS riportano titolo, link, data di pubblicazione e GUID delle ultime 10 voci.
La tipologia delle circolari (alunni e famiglie / docenti e ATA) non è nel
feed: viene letta dalla pagina `/circolare/` solo quando c'è almeno una
circolare nuova. Per gli eventi la data mostrata è quella di pubblicazione,
non quella dell'evento.
