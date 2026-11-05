#!/usr/bin/env python3
"""
Monitor scuole — controlla i siti di più Istituti Comprensivi e invia un
messaggio Telegram per ogni nuova pubblicazione, ognuno al gruppo del proprio
istituto.

Istituti attualmente monitorati:
  - IC "E. Toti" di Lentate sul Seveso (Drupal, parsing HTML)
      Notizie / Circolari / Calendario eventi / Albo online
  - IC Via De Gasperi di Seveso (WordPress, feed RSS)
      Notizie / Circolari / Eventi / Documenti programmatici

Esecuzione "one-shot": lo script gira una volta, controlla tutti gli istituti
e termina (pensato per cron / GitHub Action).

Stato persistito su un unico file JSON (niente database), suddiviso per
istituto e poi per sezione:

    {
      "ictoti":    {"notizie": [...], "circolari": [...], ...},
      "degasperi": {"notizie": [...], "circolari": [...], ...}
    }

Ogni lista contiene gli identificativi già notificati (URL per il sito
Drupal, GUID del post per i feed WordPress), troncata a
STATE_MAX_PER_SECTION elementi. Il vecchio formato "piatto" (solo IC Toti)
viene convertito automaticamente al caricamento.
"""

import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Rome")
except Exception:  # tzdata assente: si ripiega sull'orario del feed
    LOCAL_TZ = None

# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "si", "sì"}


def env_str(*names: str) -> str | None:
    """Primo valore NON vuoto tra le variabili indicate (le secret GitHub
    non configurate arrivano come stringa vuota, non come assenti)."""
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


DEBUG_MODE = env_flag("DEBUG_MODE")

# BASE_URL resta accettato per compatibilità con le installazioni esistenti.
ICTOTI_BASE_URL = env_str("ICTOTI_BASE_URL", "BASE_URL") or "https://www.iclentate.edu.it"
DEGASPERI_BASE_URL = env_str("DEGASPERI_BASE_URL") or "https://www.icviadegasperi.edu.it"

INSTITUTES = {
    "ictoti": {
        "name": 'IC "E. Toti" di Lentate sul Seveso',
        "short": "IC Toti",
        "base_url": ICTOTI_BASE_URL,
        # TELEGRAM_CHAT resta valido come fallback (installazioni pre-esistenti)
        "chat_env": ["TELEGRAM_CHAT_ICTOTI", "TELEGRAM_CHAT"],
        "chat_default": None,
        "sections": {
            "notizie": {
                "kind": "drupal_list",
                "label": "NOTIZIA",
                "emoji": "📰",
                "list_url": f"{ICTOTI_BASE_URL}/novita/notizie",
                "path_prefix": "/novita/notizie/",
                "destinatari": "drupal_detail",
            },
            "circolari": {
                "kind": "drupal_list",
                "label": "CIRCOLARE",
                "emoji": "📄",
                "list_url": f"{ICTOTI_BASE_URL}/novita/circolari",
                "path_prefix": "/novita/circolari/",
                "destinatari": "drupal_detail",
            },
            "eventi": {
                "kind": "drupal_eventi",
                "label": "EVENTO",
                "emoji": "📅",
                "list_url": f"{ICTOTI_BASE_URL}/novita/calendario-eventi",
                # gli eventi puntano a /novita/notizie/... o /novita/circolari/...
            },
            "albo": {
                "kind": "drupal_albo",
                "label": "ALBO ONLINE",
                "emoji": "🏛️",
                "list_url": f"{ICTOTI_BASE_URL}/novita/albo-pretorio",
            },
        },
    },
    "degasperi": {
        "name": "IC Via De Gasperi di Seveso",
        "short": "IC De Gasperi",
        "base_url": DEGASPERI_BASE_URL,
        "chat_env": ["TELEGRAM_CHAT_DEGASPERI"],
        "chat_default": "@icdegasperisevesonews",
        "sections": {
            "notizie": {
                "kind": "rss",
                "label": "NOTIZIA",
                "emoji": "📰",
                "feed_url": f"{DEGASPERI_BASE_URL}/tipologia-articolo/notizie/feed/",
            },
            "circolari": {
                "kind": "rss",
                "label": "CIRCOLARE",
                "emoji": "📄",
                "feed_url": f"{DEGASPERI_BASE_URL}/circolare/feed/",
                # Il feed non riporta la tipologia (alunni e famiglie / docenti
                # e ATA): la si legge dalla pagina elenco, solo se serve.
                "destinatari": "wp_listing",
                "list_url": f"{DEGASPERI_BASE_URL}/circolare/",
            },
            "eventi": {
                "kind": "rss",
                "label": "EVENTO",
                "emoji": "📅",
                "feed_url": f"{DEGASPERI_BASE_URL}/evento/feed/",
            },
            "documenti": {
                "kind": "rss",
                "label": "DOCUMENTO PROGRAMMATICO",
                "emoji": "📑",
                "feed_url": f"{DEGASPERI_BASE_URL}/tipologia-documento/documento-programmatico/feed/",
            },
        },
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

# Chiavi del vecchio formato piatto (solo IC Toti), usate per la migrazione
LEGACY_INSTITUTE = "ictoti"
LEGACY_SECTIONS = ("notizie", "circolari", "eventi", "albo")

TELEGRAM_TOKEN = env_str("TELEGRAM_TOKEN")

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

def is_legacy_state(data: dict) -> bool:
    """Il formato vecchio ha le sezioni come chiavi di primo livello."""
    return any(isinstance(data.get(key), list) for key in LEGACY_SECTIONS)


def migrate_legacy_state(data: dict) -> dict:
    """
    Converte {"notizie": [...], ...} in {"ictoti": {"notizie": [...], ...}}.
    Le liste vuote NON vengono migrate: una sezione vuota nel vecchio formato
    significa "mai inizializzata con successo", quindi deve ripassare dal
    bootstrap invece di notificare in blocco tutto quello che trova.
    """
    migrated = {
        key: value
        for key, value in data.items()
        if key in LEGACY_SECTIONS and isinstance(value, list) and value
    }
    logger.warning(
        "Stato nel vecchio formato: lo converto sotto '%s' (sezioni: %s)",
        LEGACY_INSTITUTE,
        ", ".join(migrated) or "nessuna",
    )
    return {LEGACY_INSTITUTE: migrated}


def load_state() -> dict:
    if not STATE_FILE.exists():
        logger.info("Nessun file di stato trovato, parto da zero (%s)", STATE_FILE)
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Stato corrotto o illeggibile (%s), riparto da zero", e)
        return {}

    if not isinstance(data, dict):
        logger.error("Stato con struttura inattesa, riparto da zero")
        return {}

    if is_legacy_state(data):
        data = migrate_legacy_state(data)

    return data


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Troncamento: mantieni solo gli ultimi N identificativi per sezione
    for inst_state in state.values():
        for section_key in inst_state:
            inst_state[section_key] = inst_state[section_key][-STATE_MAX_PER_SECTION:]

    tmp_path = STATE_FILE.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(STATE_FILE)  # scrittura atomica


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def resolve_chat(inst: dict) -> str | None:
    return env_str(*inst["chat_env"]) or inst.get("chat_default")


def send_telegram(chat: str | None, text: str) -> bool:
    if DEBUG_MODE:
        logger.info("[DEBUG] Messaggio Telegram simulato verso %s (nessun invio):\n%s", chat, text)
        return True

    if not TELEGRAM_TOKEN or not chat:
        logger.error("TELEGRAM_TOKEN o chat di destinazione non configurati, salto invio")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": chat,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            try:
                description = r.json().get("description", "risposta senza descrizione")
            except (ValueError, AttributeError):
                description = "risposta Telegram non decodificabile"
            logger.error(
                "Errore invio Telegram verso %s (HTTP %s): %s",
                chat,
                r.status_code,
                description,
            )
            return False
        return True
    except requests.RequestException as e:
        # Non stampare l'eccezione completa: requests vi include l'URL, che
        # contiene il token del bot.
        logger.error("Errore di rete durante l'invio Telegram: %s", type(e).__name__)
        return False


# --------------------------------------------------------------------------
# Utility
# --------------------------------------------------------------------------

def fetch(url: str) -> requests.Response | None:
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.error("Errore scaricando %s: %s", url, e)
        return None
    return r


def fetch_html(url: str) -> BeautifulSoup | None:
    r = fetch(url)
    return BeautifulSoup(r.text, "html.parser") if r is not None else None


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def contains_infanzia(*texts: str) -> bool:
    joined = " ".join(t.lower() for t in texts if t)
    return any(kw in joined for kw in INFANZIA_KEYWORDS)


# --------------------------------------------------------------------------
# IC Toti — parsing HTML (Drupal "Porte Aperte sul Web", tema Bootstrap Italia)
# --------------------------------------------------------------------------

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


def parse_drupal_list(soup: BeautifulSoup, cfg: dict, base_url: str) -> list[dict]:
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
    list_url_norm = cfg["list_url"].rstrip("/")
    prefix = cfg["path_prefix"]

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

        full_url = urljoin(base_url, href).split("?")[0]
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
            date_match = re.search(r"(\d{2}/\d{2}/\d{4})\b", block_text)
            date_str = date_match.group(1) if date_match else "N/D"

        items.append({
            "key": full_url,
            "url": full_url,
            "title": title,
            "date": date_str,
            "block_text": block_text,
        })

    return items


def parse_drupal_destinatario(detail_soup: BeautifulSoup) -> str:
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


def parse_drupal_eventi(soup: BeautifulSoup, base_url: str) -> list[dict]:
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

        full_url = urljoin(base_url, href).split("?")[0]
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
            "key": full_url,
            "url": full_url,
            "title": title,
            "date": date_str,
            "tipo": tipo,
            "block_text": block_text,
        })

    return items


def parse_drupal_albo(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Estrae gli atti dalla tabella Drupal dell'Albo online."""
    main = get_main_content(soup)
    items = []
    seen_urls = set()

    for row in main.select("table tbody tr"):
        title_cell = row.select_one("td.views-field-title")
        link_tag = title_cell.find("a", href=True) if title_cell else None
        if link_tag is None or "/albo-pretorio/" not in link_tag["href"]:
            continue

        full_url = urljoin(base_url, link_tag["href"]).split("?")[0]
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        number_cell = row.select_one("td.views-field-field-numero-inserimento")
        protocol_cell = row.select_one("td.views-field-field-atto-protocollo")
        publication_cell = row.select_one("td.views-field-field-atto-fine-pubblicazione")
        category_cell = row.select_one("td.views-field-field-categoria-albo-pretorio")

        protocol_text = clean_text(protocol_cell.get_text(" ")) if protocol_cell else ""
        date_match = re.search(
            r"(?:Lun|Mar|Mer|Gio|Ven|Sab|Dom),?\s+\d{1,2}\s+\w+\s+\d{4}",
            protocol_text,
            re.IGNORECASE,
        )

        items.append({
            "key": full_url,
            "url": full_url,
            "title": clean_text(link_tag.get_text()),
            "date": date_match.group(0) if date_match else "N/D",
            "number": clean_text(number_cell.get_text()) if number_cell else "N/D",
            "protocol": protocol_text.split(" ", 1)[0] if protocol_text else "N/D",
            "publication": clean_text(publication_cell.get_text(" ")) if publication_cell else "",
            "category": clean_text(category_cell.get_text(" ")) if category_cell else "",
            "block_text": clean_text(row.get_text(" ")),
        })

    return items


# --------------------------------------------------------------------------
# IC De Gasperi — feed RSS (WordPress, tema Designers Italia di Easyteam)
# --------------------------------------------------------------------------

def format_rss_date(pub_date: str) -> str:
    if not pub_date:
        return "N/D"
    try:
        dt = parsedate_to_datetime(pub_date)
    except (TypeError, ValueError):
        return clean_text(pub_date)
    if LOCAL_TZ is not None and dt.tzinfo is not None:
        dt = dt.astimezone(LOCAL_TZ)
    return dt.strftime("%d/%m/%Y")


def parse_rss(xml_bytes: bytes) -> list[dict]:
    """
    Feed RSS 2.0 standard di WordPress. Come chiave di stato si usa il
    <guid> (es. https://.../?post_type=circolare&p=27764), che contiene l'ID
    del post e non cambia se la redazione modifica titolo o slug: evita le
    notifiche doppie che si avrebbero usando l'URL.
    I feed sono ordinati dal più recente e contengono le ultime 10 voci.
    """
    root = ET.fromstring(xml_bytes)
    items = []
    seen_keys = set()

    for node in root.iter("item"):
        title = clean_text(node.findtext("title"))
        link = clean_text(node.findtext("link"))
        guid = clean_text(node.findtext("guid"))
        key = guid or link
        if not key or not title or key in seen_keys:
            continue
        seen_keys.add(key)

        description = clean_text(node.findtext("description"))
        items.append({
            "key": key,
            "url": link or guid,
            "title": title,
            "date": format_rss_date(clean_text(node.findtext("pubDate"))),
            "block_text": f"{title} {description}",
        })

    return items


def fetch_rss(url: str) -> list[dict] | None:
    r = fetch(url)
    if r is None:
        return None
    try:
        return parse_rss(r.content)
    except ET.ParseError as e:
        logger.error("Feed non valido %s: %s", url, e)
        return None


def parse_wp_circolari_destinatari(soup: BeautifulSoup) -> dict[str, str]:
    """
    Dalla pagina elenco /circolare/ ricava URL -> tipologia. Ogni card è un
    <a class="presentation-card-link" href="URL circolare"> che contiene un
    badge <a class="badge" href=".../tipologia-circolare/...">.
    "Circolari per alunni e famiglie" -> "Alunni e famiglie".
    """
    result = {}
    for card in soup.select("a.presentation-card-link[href]"):
        badges = [
            clean_text(b.get_text())
            for b in card.find_all("a", href=re.compile(r"/tipologia-circolare/"))
        ]
        labels = []
        for text in badges:
            text = re.sub(r"^circolari\s+per\s+", "", text, flags=re.IGNORECASE)
            if text:
                labels.append(text[0].upper() + text[1:])
        if labels:
            result[card["href"]] = ", ".join(labels)
    return result


# --------------------------------------------------------------------------
# Raccolta elementi (indipendente dal tipo di sito)
# --------------------------------------------------------------------------

def collect_items(inst: dict, cfg: dict) -> list[dict] | None:
    """Ritorna la lista degli elementi attuali, o None se il sito non risponde."""
    kind = cfg["kind"]
    if kind == "rss":
        return fetch_rss(cfg["feed_url"])

    soup = fetch_html(cfg["list_url"])
    if soup is None:
        return None
    if kind == "drupal_list":
        return parse_drupal_list(soup, cfg, inst["base_url"])
    if kind == "drupal_eventi":
        return parse_drupal_eventi(soup, inst["base_url"])
    if kind == "drupal_albo":
        return parse_drupal_albo(soup, inst["base_url"])
    raise ValueError(f"Tipo di sezione sconosciuto: {kind}")


def add_destinatari(cfg: dict, items: list[dict]) -> None:
    """Arricchisce SOLO gli elementi nuovi con i destinatari, se disponibili."""
    mode = cfg.get("destinatari")
    if mode == "drupal_detail":
        for item in items:
            detail_soup = fetch_html(item["url"])
            if detail_soup is not None:
                item["destinatario"] = parse_drupal_destinatario(detail_soup)
            time.sleep(1)  # piccola pausa, non bombardare il sito
    elif mode == "wp_listing":
        soup = fetch_html(cfg["list_url"])
        if soup is None:
            return
        mapping = parse_wp_circolari_destinatari(soup)
        for item in items:
            item["destinatario"] = mapping.get(item["url"], "")


# --------------------------------------------------------------------------
# Costruzione messaggi
# --------------------------------------------------------------------------

def build_message(cfg: dict, item: dict) -> str:
    destinatario = item.get("destinatario", "")
    infanzia_tag = " 🟢 (possibile Infanzia)" if contains_infanzia(
        item.get("title", ""), item.get("block_text", ""), destinatario
    ) else ""

    lines = [f"{cfg['emoji']} {cfg['label']}{infanzia_tag}"]
    lines.append(f"Titolo: {item['title']}")

    if "number" in item:  # atto dell'Albo online
        lines.append(f"📅 Data atto: {item.get('date', 'N/D')}")
        lines.append(f"🔢 Inserimento: {item.get('number', 'N/D')} — Protocollo: {item.get('protocol', 'N/D')}")
        if item.get("category"):
            lines.append(f"📁 Categoria: {item['category']}")
        if item.get("publication"):
            lines.append(f"🗓️ Pubblicazione: {item['publication']}")
    elif "tipo" in item:  # evento del calendario Drupal
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

def bootstrap_section(inst: dict, section_key: str, inst_state: dict) -> None:
    """
    Primo controllo di una sezione: marca tutto il contenuto attuale come già
    notificato, SENZA inviare messaggi, per evitare una valanga di notifiche
    retroattive. La sezione risulta inizializzata solo se il sito ha risposto:
    da quel momento la sua chiave esiste nello stato (anche con lista vuota,
    es. un feed eventi senza voci), così il primo elemento pubblicato in
    futuro verrà notificato e non "assorbito" da un nuovo bootstrap.
    """
    cfg = inst["sections"][section_key]
    logger.info("[%s] Bootstrap sezione '%s'", inst["short"], section_key)

    items = collect_items(inst, cfg)
    if items is None:
        logger.warning("[%s]   sito non raggiungibile, riprovo al prossimo run", inst["short"])
        return

    known = inst_state.setdefault(section_key, [])
    for item in items:
        if item["key"] not in known:
            known.append(item["key"])

    logger.info(
        "[%s]   %d elementi marcati come già noti (nessuna notifica inviata)",
        inst["short"],
        len(items),
    )


def process_section(inst: dict, chat: str | None, section_key: str, inst_state: dict) -> int:
    cfg = inst["sections"][section_key]
    logger.info("[%s] Controllo sezione '%s'", inst["short"], section_key)

    items = collect_items(inst, cfg)
    if items is None:
        return 0

    already_seen = set(inst_state.get(section_key, []))
    new_items = [it for it in items if it["key"] not in already_seen]

    if not new_items:
        logger.info("[%s]   nessuna novità (%d elementi in lista, già tutti noti)", inst["short"], len(items))
        return 0

    # Notifica dal più vecchio al più recente, così l'ordine in chat è cronologico
    new_items.reverse()
    add_destinatari(cfg, new_items)

    sent_count = 0
    for item in new_items:
        ok = send_telegram(chat, build_message(cfg, item))
        if ok:
            sent_count += 1
            inst_state.setdefault(section_key, []).append(item["key"])
            logger.info("[%s]   %s: %s", inst["short"], "simulato" if DEBUG_MODE else "notificato", item["title"])
        else:
            logger.error("[%s]   invio fallito, NON segno come notificato: %s", inst["short"], item["title"])
            # Non aggiungo la chiave allo stato: verrà ritentato al prossimo run.

        time.sleep(1)  # rate limit gentile verso l'API Telegram

    return sent_count


def process_institute(inst_key: str, inst: dict, state: dict) -> int:
    chat = resolve_chat(inst)
    if not DEBUG_MODE and not chat:
        logger.error(
            "[%s] Nessuna chat Telegram configurata (%s): istituto saltato",
            inst["short"],
            " / ".join(inst["chat_env"]),
        )
        return 0

    inst_state = state.setdefault(inst_key, {})
    is_first_start = not inst_state

    if is_first_start:
        logger.warning(
            "[%s] Primo avvio per questo istituto: marco tutto il contenuto "
            "attuale come già notificato, senza inviare messaggi retroattivi.",
            inst["short"],
        )

    total_sent = 0
    for section_key in inst["sections"]:
        try:
            if section_key not in inst_state:
                bootstrap_section(inst, section_key, inst_state)
            else:
                total_sent += process_section(inst, chat, section_key, inst_state)
        except Exception as e:  # non bloccare le altre sezioni per un errore isolato
            logger.exception("[%s] Errore imprevisto nella sezione '%s': %s", inst["short"], section_key, e)
            send_telegram(chat, f"⚠️ Errore monitor {inst['short']}, sezione {section_key}: {e}")

    if is_first_start and inst_state:
        send_telegram(
            chat,
            f"✅ Monitor {inst['name']} avviato. "
            "Da ora in poi riceverai un messaggio per ogni nuova "
            "pubblicazione sul sito della scuola.",
        )

    return total_sent


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    if not DEBUG_MODE and not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN deve essere definito nell'ambiente")
        return 1

    if DEBUG_MODE:
        logger.warning(
            "DEBUG_MODE attiva: Telegram disabilitato; lo stato verrà aggiornato normalmente"
        )

    state = load_state()

    total_sent = 0
    for inst_key, inst in INSTITUTES.items():
        total_sent += process_institute(inst_key, inst, state)

    save_state(state)

    logger.info("Run completato. Messaggi inviati: %d", total_sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
