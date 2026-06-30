#!/usr/bin/env python3
"""
Monitor IC Lentate — controlla le pagine Notizie / Circolari / Calendario eventi
del sito dell'Istituto Comprensivo "E. Toti" di Lentate sul Seveso e invia un
messaggio Telegram per ogni nuova pubblicazione trovata.

Esecuzione "one-shot": lo script gira una volta e termina (pensato per cron /
GitHub Action).

Stato persistito su file JSON (niente database): si tiene per ciascuna
sezione l'elenco degli URL già notificati, troncato a STATE_MAX_PER_SECTION
elementi, per recuperare eventuali notifiche perse tra un run e l'altro.
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------

BASE_URL = os.environ.get("BASE_URL", "https://www.iclentate.edu.it")

SECTIONS = {
    "notizie": {
        "label": "NOTIZIA",
        "emoji": "📰",
        "list_url": f"{BASE_URL}/novita/notizie",
        "path_prefix": "/novita/notizie/",
    },
    "circolari": {
        "label": "CIRCOLARE",
        "emoji": "📄",
        "list_url": f"{BASE_URL}/novita/circolari",
        "path_prefix": "/novita/circolari/",
    },
    "eventi": {
        "label": "EVENTO",
        "emoji": "📅",
        "list_url": f"{BASE_URL}/novita/calendario-eventi",
        "path_prefix": None,  # gli eventi puntano a /novita/notizie/... o /novita/circolari/...
    },
}

# Parole chiave usate SOLO per etichettare (mai per filtrare/escludere nulla).
# Match case-insensitive su titolo + testo destinatario.
INFANZIA_KEYWORDS = [
    "infanzia",
    "scuola dell'infanzia",
    "asilo",
]

STATE_FILE = Path(os.environ.get("STATE_FILE", "state/last_seen.json"))
STATE_MAX_PER_SECTION = 300  # tetto per evitare crescita illimitata del file

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT")

REQUEST_TIMEOUT = 20
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ICLentateMonitor/1.0; +https://github.com/)"
}

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ic_lentate_monitor")


# --------------------------------------------------------------------------
# Stato persistito (file JSON, niente DB)
# --------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        logger.info("Nessun file di stato trovato, parto da zero (%s)", STATE_FILE)
        return {key: [] for key in SECTIONS}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Stato corrotto o illeggibile (%s), riparto da zero", e)
        return {key: [] for key in SECTIONS}

    for key in SECTIONS:
        data.setdefault(key, [])
    return data


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Troncamento: mantieni solo gli ultimi N URL per sezione
    for key in state:
        state[key] = state[key][-STATE_MAX_PER_SECTION:]

    tmp_path = STATE_FILE.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(STATE_FILE)  # scrittura atomica


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        logger.error("TELEGRAM_TOKEN / TELEGRAM_CHAT non configurati, salto invio")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Errore invio Telegram: %s", e)
        return False


# --------------------------------------------------------------------------
# Parsing pagine
# --------------------------------------------------------------------------

def fetch_html(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.error("Errore scaricando %s: %s", url, e)
        return None
    return BeautifulSoup(r.text, "html.parser")


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def contains_infanzia(*texts: str) -> bool:
    joined = " ".join(t.lower() for t in texts if t)
    return any(kw in joined for kw in INFANZIA_KEYWORDS)


def get_main_content(soup: BeautifulSoup) -> BeautifulSoup:
    """
    Restringe la ricerca al tag <main id="it-main-content">, che su questo
    sito (tema Bootstrap Italia / eduitalia) racchiude SOLO il contenuto
    della pagina, escludendo header, menu di navigazione e footer.
    Questo evita falsi positivi come il link "Contatti" del menu in alto,
    che punta a /novita/notizie/recapiti-0 (un nodo "Notizia" reale usato
    come pagina dei contatti, quindi indistinguibile per URL da una notizia
    vera — va escluso per POSIZIONE nel DOM, non per pattern di URL).
    Se per qualche motivo il tag non viene trovato, ritorna l'intera pagina
    come fallback (meglio un falso positivo occasionale che un crash totale).
    """
    main = soup.find("main", id="it-main-content")
    return main if main is not None else soup


def parse_notizie_circolari(soup: BeautifulSoup, section_key: str) -> list[dict]:
    """
    Parsing per le sezioni 'notizie' e 'circolari'.
    Struttura reale verificata sull'HTML del sito (tema Bootstrap Italia):
    ogni elemento è una card così strutturata:

      <div class="card ...">
        <div class="card-body">
          <div class="head-tags">
            <span class="card-tag">Notizia</span>
            <span class="data"><time datetime="...">29/05/2026</time></span>
          </div>
          <h3 class="card-title">Titolo (testo puro, NESSUN link qui)</h3>
          <div class="card-text">...destinatario...</div>
          <div class="it-card-footer">
            <a href="/novita/notizie/slug-pagina">Leggi tutto</a>
          </div>
        </div>
      </div>

    Il link sta SOLO nel footer della card, non nel titolo. Si individua
    ogni card tramite il titolo (h3.card-title) e si risale/scende nel DOM
    per trovare il link e la data associati alla stessa card.
    """
    main = get_main_content(soup)
    list_url_norm = SECTIONS[section_key]["list_url"].rstrip("/")
    prefix = SECTIONS[section_key]["path_prefix"]

    items = []
    seen_urls = set()

    for title_tag in main.find_all(class_="card-title"):
        title = clean_text(title_tag.get_text())
        if not title:
            continue

        # La card è il contenitore comune a titolo, data e link "Leggi tutto".
        card = title_tag.find_parent(class_="card") or title_tag.find_parent("div")
        if card is None:
            continue

        # Link: o dentro il titolo stesso (pattern eventi/altre viste), o nel
        # footer della card (pattern notizie/circolari verificato).
        link_tag = title_tag.find("a", href=True) or card.find("a", href=True)
        if link_tag is None:
            continue

        href = link_tag["href"]
        if prefix not in href:
            continue  # link ad altra sezione, non pertinente a questa card

        full_url = urljoin(BASE_URL, href).split("?")[0]
        if full_url.rstrip("/") == list_url_norm:
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        block_text = clean_text(card.get_text(" "))

        # Data: preferenza al tag <time datetime="..."> se presente (più
        # affidabile), altrimenti fallback su regex sul testo della card.
        time_tag = card.find("time")
        if time_tag and time_tag.get_text():
            date_str = clean_text(time_tag.get_text())
        else:
            date_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", block_text)
            date_str = date_match.group(1) if date_match else "N/D"

        items.append({
            "url": full_url,
            "title": title,
            "date": date_str,
            "block_text": block_text,
        })

    return items


def parse_destinatario(detail_soup: BeautifulSoup) -> str:
    """
    Estrae i destinatari dalla pagina di dettaglio. Su questo sito Drupal i
    destinatari (Docenti / Famiglie / Personale ATA / Alunni, o l'"Argomenti"
    delle notizie) sono link di tassonomia con href del tipo
    /taxonomy/term/<id>. Si individua la zona "Destinatari" cercando
    l'etichetta testuale e raccogliendo i link di tassonomia che la seguono
    nel documento; se l'etichetta non si trova, ritorna stringa vuota
    (nessuna invenzione di dati).
    """
    main = get_main_content(detail_soup)

    label_node = main.find(
        string=lambda s: isinstance(s, str) and clean_text(s).lower() == "destinatari"
    )
    if label_node is None:
        return ""

    # Cerca i link /taxonomy/term/ che seguono l'etichetta nel flusso del documento
    tags = []
    for el in label_node.find_all_next(limit=15):
        if el.name == "a" and "/taxonomy/term/" in el.get("href", ""):
            text = clean_text(el.get_text())
            if text:
                tags.append(text)
        elif el.name in ("h1", "h2", "h3"):
            break  # entrato in un'altra sezione, smetti di cercare

    return ", ".join(tags)


def parse_eventi(soup: BeautifulSoup) -> list[dict]:
    """
    Parsing per 'calendario-eventi'. Struttura reale verificata:

      <div class="card ...">
        <div class="card-body">
          <h3 class="card-title"><a href="/novita/notizie/slug">Titolo</a></h3>
          <p class="card-subtitle">Notizia</p>
          <div class="card-text">Data inizio  gg/mm/aa - hh:mm<br>Data di fine ...</div>
        </div>
      </div>

    A differenza di notizie/circolari, qui il link sta DENTRO il titolo.
    Ogni voce punta a /novita/notizie/... o /novita/circolari/....
    """
    main = get_main_content(soup)

    items = []
    seen_urls = set()

    for title_tag in main.find_all(class_="card-title"):
        title = clean_text(title_tag.get_text())
        if not title:
            continue

        link_tag = title_tag.find("a", href=True)
        if link_tag is None:
            continue

        href = link_tag["href"]
        if "/novita/notizie/" not in href and "/novita/circolari/" not in href:
            continue

        full_url = urljoin(BASE_URL, href).split("?")[0]
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        card = title_tag.find_parent(class_="card") or title_tag.find_parent("div")
        block_text = clean_text(card.get_text(" ")) if card else ""

        subtitle_tag = (card.find(class_="card-subtitle") if card else None)
        tipo = clean_text(subtitle_tag.get_text()) if subtitle_tag else "Evento"
        if not tipo:
            tipo = "Evento"

        start_match = re.search(r"Data inizio\s*([\d/]+\s*-\s*[\d:]+)", block_text)
        date_str = clean_text(start_match.group(1)) if start_match else "N/D"

        items.append({
            "url": full_url,
            "title": title,
            "date": date_str,
            "tipo": tipo,
            "block_text": block_text,
        })

    return items


# --------------------------------------------------------------------------
# Costruzione messaggi
# --------------------------------------------------------------------------

def build_message(section_key: str, item: dict, destinatario: str = "") -> str:
    cfg = SECTIONS[section_key]
    infanzia_tag = " 🟢 (possibile Infanzia)" if contains_infanzia(
        item.get("title", ""), item.get("block_text", ""), destinatario
    ) else ""

    lines = [f"{cfg['emoji']} {cfg['label']}{infanzia_tag}"]
    lines.append(f"Titolo: {item['title']}")

    if section_key == "eventi":
        lines.append(f"Tipo: {item.get('tipo', 'N/D')}")
        lines.append(f"📅 Data: {item.get('date', 'N/D')}")
    else:
        lines.append(f"📅 Data: {item.get('date', 'N/D')}")
        if destinatario:
            lines.append(f"👥 Destinatari: {destinatario}")

    lines.append(f"🔗 {item['url']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Logica principale per sezione
# --------------------------------------------------------------------------

def bootstrap_section(section_key: str, state: dict) -> int:
    """
    Usata solo al primissimo avvio: legge la lista attuale e marca tutti gli
    URL trovati come già notificati, SENZA inviare alcun messaggio Telegram.
    Evita di scaricare una valanga di notifiche retroattive al setup iniziale.
    """
    cfg = SECTIONS[section_key]
    logger.info("Bootstrap sezione '%s' (%s)", section_key, cfg["list_url"])

    soup = fetch_html(cfg["list_url"])
    if soup is None:
        return 0

    if section_key == "eventi":
        items = parse_eventi(soup)
    else:
        items = parse_notizie_circolari(soup, section_key)

    urls = [it["url"] for it in items]
    state.setdefault(section_key, [])
    for url in urls:
        if url not in state[section_key]:
            state[section_key].append(url)

    logger.info("  %d elementi marcati come già noti (nessuna notifica inviata)", len(urls))
    return 0


def process_section(section_key: str, state: dict) -> int:
    cfg = SECTIONS[section_key]
    logger.info("Controllo sezione '%s' (%s)", section_key, cfg["list_url"])

    soup = fetch_html(cfg["list_url"])
    if soup is None:
        return 0

    if section_key == "eventi":
        items = parse_eventi(soup)
    else:
        items = parse_notizie_circolari(soup, section_key)

    already_seen = set(state.get(section_key, []))
    new_items = [it for it in items if it["url"] not in already_seen]

    if not new_items:
        logger.info("  nessuna novità (%d elementi in lista, già tutti noti)", len(items))
        return 0

    # Notifica dal più vecchio al più recente, così l'ordine in chat è cronologico
    new_items.reverse()

    sent_count = 0
    for item in new_items:
        destinatario = ""
        if section_key in ("notizie", "circolari"):
            detail_soup = fetch_html(item["url"])
            if detail_soup is not None:
                destinatario = parse_destinatario(detail_soup)
            time.sleep(1)  # piccola pausa, non bombardare il sito

        msg = build_message(section_key, item, destinatario)
        ok = send_telegram(msg)
        if ok:
            sent_count += 1
            state.setdefault(section_key, []).append(item["url"])
            logger.info("  notificato: %s", item["title"])
        else:
            logger.error("  invio fallito, NON segno come notificato: %s", item["title"])
            # Non aggiungo l'URL allo stato: verrà ritentato al prossimo run.

        time.sleep(1)  # rate limit gentile verso l'API Telegram

    return sent_count


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        logger.error("TELEGRAM_TOKEN e TELEGRAM_CHAT devono essere definiti nell'ambiente")
        return 1

    state = load_state()
    is_bootstrap = all(len(state.get(key, [])) == 0 for key in SECTIONS)

    if is_bootstrap:
        logger.warning(
            "Primo avvio rilevato (stato vuoto): marco tutto il "
            "contenuto attuale come già notificato, senza inviare messaggi "
            "retroattivi. Dal prossimo run verranno notificate solo le novità."
        )

    total_sent = 0

    for section_key in SECTIONS:
        try:
            if is_bootstrap:
                total_sent += bootstrap_section(section_key, state)
            else:
                total_sent += process_section(section_key, state)
        except Exception as e:  # non bloccare le altre sezioni per un errore isolato
            logger.exception("Errore imprevisto nella sezione '%s': %s", section_key, e)
            send_telegram(f"⚠️ Errore monitor IC Lentate, sezione {section_key}: {e}")

    save_state(state)

    if is_bootstrap:
        send_telegram(
            "✅ Monitor IC Lentate avviato per la prima volta. "
            "Da ora in poi riceverai un messaggio per ogni nuova "
            "notizia, circolare o evento pubblicato sul sito."
        )

    logger.info("Run completato. Messaggi inviati: %d", total_sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
