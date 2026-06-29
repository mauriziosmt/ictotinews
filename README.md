# IC Lentate Alert

Bot Telegram che monitora le pagine **Notizie**, **Circolari** e **Calendario eventi**
del sito dell'Istituto Comprensivo "E. Toti" di Lentate sul Seveso
([www.iclentate.edu.it](https://www.iclentate.edu.it)) e invia un messaggio
per ogni nuova pubblicazione.

Il sito è un Drupal "Porte Aperte sul Web" senza endpoint
strutturati, quindi si fa scraping HTML delle pagine elenco e (per
notizie/circolari) della pagina di dettaglio per recuperare i destinatari.

## Cosa c'è davvero nei dati (e cosa no)

Ho controllato le pagine prima di scrivere il codice. Il sito espone in modo
strutturato:

- **Notizie**: titolo, data, destinatario testuale libero (es. "Alla
  Comunità"), autore, link.
- **Circolari**: numero circolare, titolo, data, **destinatari come
  tassonomia** (Docenti / Famiglie / Personale ATA / Alunni), autore, link.
- **Calendario eventi**: titolo, tipo (Notizia/Circolare/Evento/Corso),
  data inizio/fine, link all'elemento collegato.

**Non esistono campi strutturati per "Plesso" o "Ordine di scuola"
(Infanzia/Primaria/...)**. Per questo il messaggio Telegram riporta solo i
campi realmente disponibili, ed è stato scelto — di comune accordo — di
aggiungere solo un'etichetta automatica 🟢 *(possibile Infanzia)* quando
titolo o destinatario contengono parole chiave come "infanzia", senza
inventare o forzare campi che il sito non fornisce. L'etichetta è un bonus
indicativo, non un filtro: ricevi sempre tutto.

## Formato dei messaggi

```
📰 NOTIZIA 🟢 (possibile Infanzia)
Titolo: Festa di Natale alla Scuola dell'Infanzia di Birago
📅 Data: 10/12/2026
👥 Destinatari: Famiglie Infanzia
🔗 https://www.iclentate.edu.it/novita/notizie/...
```

```
📄 CIRCOLARE
Titolo: Lettera sciopero ai genitori del 29 maggio 2026
📅 Data: 27/05/2026
👥 Destinatari: Docenti, Famiglie, Personale ATA
🔗 https://www.iclentate.edu.it/novita/circolari/...
```

```
📅 EVENTO
Titolo: Concerto di fine anno
Tipo: Notizia
📅 Data: 18/12/26 - 18:00
🔗 https://www.iclentate.edu.it/novita/notizie/...
```

## Come funziona (architettura)

- **Esecuzione one-shot** lo script gira, controlla le 3 pagine, invia eventuali
  notifiche, salva lo stato e termina. È il modello giusto per cron/Actions.
- **Stato senza database**: un file `state/last_seen.json` con, per
  ciascuna sezione, l'elenco degli URL già notificati (troncato agli ultimi
  300 per evitare crescita illimitata). Ad ogni run si confrontano gli URL
  trovati in lista con quelli già noti: i nuovi vengono notificati **tutti**,
  non solo l'ultimo — questo gestisce correttamente il caso di più
  pubblicazioni nelle 2 ore tra un controllo e l'altro.
- **Bootstrap al primo avvio**: se lo stato è completamente vuoto (prima
  esecuzione in assoluto), tutto il contenuto attualmente in lista viene
  marcato come "già visto" SENZA inviare notifiche retroattive. Altrimenti
  al primo giro riceveresti decine di messaggi per tutto l'arretrato.
- **Destinatari**: per notizie e circolari viene fatta una seconda richiesta
  alla pagina di dettaglio per leggere il campo "Destinatari" (assente nella
  lista). C'è una pausa di 1 secondo tra le richieste per non sovraccaricare
  il sito della scuola.
- **Resilienza**: se l'invio Telegram di un elemento fallisce, quell'URL
  NON viene segnato come notificato — verrà ritentato al run successivo
  invece di andare perso silenziosamente.

## Uso in locale (dev, con Docker)

```bash
cp .env.example .env
# Compila .env con il token del bot e il chat_id (vedi sotto come ottenerli)

docker compose run --rm ic-lentate-alert
```

Se preferisci farlo girare in background con un controllo automatico ogni 2
ore (invece di lanciarlo a mano):

```bash
docker compose --profile loop up -d ic-lentate-alert-loop
```

### Creare il bot Telegram e ottenere token/chat_id

1. Su Telegram apri una chat con **@BotFather**, manda `/newbot` e segui le
   istruzioni: otterrai il `TELEGRAM_TOKEN`.
2. Crea un canale (consigliato) o usa
   una chat privata, e aggiungi il bot come amministratore del canale.
3. Per ottenere il `TELEGRAM_CHAT`:
   - Canale pubblico: usa `@nomecanale` direttamente come chat id.
   - Canale privato o chat privata: manda un messaggio qualsiasi nel
     canale/chat, poi visita
     `https://api.telegram.org/bot<IL_TUO_TOKEN>/getUpdates` nel browser e
     leggi il campo `chat.id` (sarà un numero negativo per i canali, tipo
     `-1001234567890`).

## Guida passo-passo: GitHub Action per la produzione

Questa è la parte di "messa in produzione" che mi hai chiesto, spiegata per
intero dato che non hai mai impostato un'Action schedulata prima.

### 1. Crea il repository su GitHub

Se non l'hai già fatto:

```bash
cd ic_lentate_alert
git init
git add .
git commit -m "Initial commit: monitor IC Lentate"
git branch -M main
git remote add origin https://github.com/<mauriziosmt>/ic-lentate-alert.git
git push -u origin main
```

(Sostituisci `<mauriziosmt>` con il tuo handle reale e il nome repo che
preferisci.)

### 2. Aggiungi i secret del repository

I secret sono variabili sensibili che GitHub inietta negli Actions senza
mai mostrarle nei log. Vanno in:

**Repository → Settings → Secrets and variables → Actions → New repository
secret**

Crea questi due secret:

| Nome | Valore |
|---|---|
| `TELEGRAM_TOKEN` | il token ottenuto da @BotFather |
| `TELEGRAM_CHAT` | l'id della chat/canale Telegram |

Il workflow (`.github/workflows/monitor.yml`, già incluso nel progetto) li
legge automaticamente come `secrets.TELEGRAM_TOKEN` e `secrets.TELEGRAM_CHAT`
— non devi modificare nulla nel file per questo.

### 3. Verifica i permessi di scrittura del workflow

L'Action deve poter fare un commit per salvare `state/last_seen.json`
aggiornato (altrimenti ogni run riparte da zero e ri-notifica tutto). Vai in:

**Repository → Settings → Actions → General → Workflow permissions**

e seleziona **"Read and write permissions"**, poi salva. Il workflow ha già
`permissions: contents: write` dichiarato, ma questa impostazione a livello
repo deve comunque essere abilitata.

### 4. Primo avvio: lancia manualmente il workflow

Non aspettare il cron per il primo test. Vai in:

**Repository → Actions → "Monitor IC Lentate" → Run workflow**

Questo userà il `workflow_dispatch` già configurato. Controlla i log
dell'esecuzione: al primissimo avvio (stato vuoto, file
`state/last_seen.json` con liste vuote come quello già incluso nel repo)
vedrai i log di "bootstrap" e riceverai **un solo** messaggio Telegram di
conferma avvio, non una raffica di notifiche per tutto l'arretrato.

### 5. Verifica il commit automatico

Dopo l'esecuzione, controlla che nel repo sia apparso un nuovo commit
"chore: aggiorna stato notifiche" che aggiorna `state/last_seen.json`. Se
non lo vedi, il problema più probabile è il punto 3 (permessi non
abilitati) — controlla i log dello step "Commit updated state" nell'Action.

### 6. Lascialo andare

Da qui in avanti il cron `0 */2 * * *` (ogni 2 ore, orari UTC) farà tutto da
solo: controlla, notifica le novità, committa lo stato. Non devi fare nulla,
salvo controllare occasionalmente la tab **Actions** per eventuali errori
(in caso di errore in una sezione, il bot stesso ti manda un messaggio
Telegram di avviso, quindi te ne accorgi anche da lì).

### Nota su GitHub Actions e repository pubblici/privati

Le GitHub Actions sono gratuite e illimitate per i repository pubblici. Per
repository privati hai un certo numero di minuti gratuiti al mese (più che
sufficienti per un job che gira ogni 2 ore e dura pochi secondi) — dato il
tuo approccio cost-conscious con gli strumenti, value per value questa è
probabilmente la soluzione a costo più basso possibile, persino rispetto a
tenerlo sul VPS.

## File del progetto

```
.
├── monitor.py                      # script principale
├── requirements.txt
├── Dockerfile
├── docker-compose.yml              # dev locale (one-shot o loop opzionale)
├── .env.example                    # template credenziali (NON committare .env)
├── .gitignore
├── state/
│   └── last_seen.json              # stato iniziale vuoto, committato nel repo
└── .github/
    └── workflows/
        └── monitor.yml             # GitHub Action schedulata ogni 2 ore
```

## Possibili migliorie future

- **Allegati PDF**: notizie e circolari spesso hanno PDF allegati (es.
  testo completo dello sciopero). Si potrebbe aggiungere il link diretto al
  PDF nel messaggio, oltre al link alla pagina.
- **Filtro per anno scolastico**: la pagina circolari ha un filtro per anno
  scolastico nell'URL; irrilevante ora ma utile se in futuro si vuole
  limitare la finestra di controllo.
- **Estrazione "azione richiesta"**: per ottenerla davvero (non indovinata)
  servirebbe leggere il testo del PDF allegato con un parser PDF — fattibile,
  ma è un salto di complessità e fragilità che abbiamo deciso di non fare
  ora. Se in futuro vorrai esplorare questa via, tienila come step separato
  con margine di errore esplicito, non come dato "certo" nel messaggio.

## Changelog

### Correzione parsing (dopo il primo test in locale)

Il primo test in locale ha mostrato `recapiti-0` tra le notizie/eventi
rilevati: non è un bug di filtro, ma un caso reale — quel nodo è una
notizia Drupal vera (usata come pagina "Contatti", linkata dal menu in
alto), quindi indistinguibile per URL da una notizia normale. Il parser
iniziale cercava i link su tutta la pagina, header e menu inclusi.

Analizzando l'HTML grezzo reale del sito (non più solo il markdown
semplificato) sono emerse due cose:

1. Tutto il contenuto vero vive dentro `<main id="it-main-content">`:
   restringere la ricerca lì dentro esclude per costruzione i link di
   header/menu/footer, `recapiti-0` compreso.
2. La struttura delle card è diversa da quella ipotizzata inizialmente: per
   notizie/circolari il titolo (`h3.card-title`) è testo puro e il link sta
   nel footer della card; per gli eventi il link sta invece dentro il
   titolo stesso. Il parser ora distingue correttamente i due pattern.

**Se hai già fatto girare la versione precedente**: il tuo
`state/last_seen.json` locale probabilmente contiene `recapiti-0` insieme
alle notizie vere. Non è un problema — è solo un URL extra "già visto" che
non verrà più ri-proposto comunque, dato che è un nodo del 2022 che non
cambia. Puoi lasciarlo così oppure rimuoverlo manualmente dal file JSON,
a tua scelta.
