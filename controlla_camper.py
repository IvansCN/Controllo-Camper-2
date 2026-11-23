#!/usr/bin/env python3
"""Controlla i camper usati di Grosso Vacanze (grossovacanze.com) e ti avvisa
su WhatsApp (Twilio) o Telegram quando compare un annuncio nuovo con prezzo
<= MAX_PRICE.

Uso:
    python controlla_camper.py --dry-run   # prova: stampa, non invia, non salva
    python controlla_camper.py             # controllo reale
    python controlla_camper.py --all       # ignora la memoria, mostra tutti i match

Variabili d'ambiente (in GitHub: Settings > Secrets and variables > Actions):
    MAX_PRICE              soglia in euro (default 30000)
    CHANNEL                "whatsapp" (default) oppure "telegram"
    INCLUDE_CARAVAN        se impostata, include anche le caravan (default: solo camper)

  WhatsApp / Twilio:
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
    TWILIO_WHATSAPP_FROM   es. whatsapp:+14155238886 (numero sandbox o il tuo)
    WHATSAPP_TO            es. whatsapp:+393331234567
    TWILIO_CONTENT_SID     (opzionale) SID del template approvato, con variabile {{1}}

  Telegram:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HOST = "www.grossovacanze.com"
BASE = f"https://{HOST}"
LISTING_URL = f"{BASE}/camper-usati/"
CARAVAN_URL = f"{BASE}/categoria-camper-usati/caravan/"

MAX_PRICE = int(os.getenv("MAX_PRICE", "30000"))
STATE_FILE = Path(os.getenv("STATE_FILE", "visti.json"))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; controllo-camper-personale/1.0)"}

PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d{4,6})\s*€")
DETAIL_PATH_RE = re.compile(r"^/camper-usati/[^/]+/?$")


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------
def get_soup(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_listings(soup):
    """Estrae {url: annuncio} da una pagina elenco."""
    found = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"]).split("#")[0].split("?")[0]
        parts = urlparse(url)
        if parts.netloc != HOST or not DETAIL_PATH_RE.match(parts.path):
            continue
        url = url.rstrip("/") + "/"

        text = a.get_text(" ", strip=True)
        m = PRICE_RE.search(text)
        if not m:  # es. link "Dettagli": prova col testo del blocco <li>
            if url in found:
                continue
            li = a.find_parent("li")
            if li:
                text = li.get_text(" ", strip=True)
                m = PRICE_RE.search(text)
        if not m:
            continue

        title = re.sub(r"^Segnaposto\s+", "", text[: m.start()].strip())
        found[url] = {
            "url": url,
            "title": title or url.rstrip("/").split("/")[-1],
            "price": int(m.group(1).replace(".", "")),
            "details": re.sub(r"\s*Dettagli\s*$", "", text[m.end():].strip()),
        }
    return found


def scrape(url, max_pages=5):
    """Scarica l'elenco seguendo l'eventuale paginazione."""
    listings = {}
    for _ in range(max_pages):
        soup = get_soup(url)
        listings.update(parse_listings(soup))
        nxt = soup.select_one("a.next, a[rel=next]")
        if not nxt or not nxt.get("href"):
            break
        url = urljoin(BASE, nxt["href"])
    return listings


# --------------------------------------------------------------------------
# Memoria degli annunci già segnalati
# --------------------------------------------------------------------------
def load_state():
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Messaggio
# --------------------------------------------------------------------------
def euro(n):
    return f"{n:,}".replace(",", ".") + " €"


def build_chunks(new, limit):
    header = f"🚐 Grosso Vacanze: annunci usati fino a {euro(MAX_PRICE)}\n"
    blocks = []
    for l in sorted(new, key=lambda x: x["price"]):
        det = f" ({l['details']})" if l["details"] else ""
        blocks.append(f"• {l['title']} – {euro(l['price'])}{det}\n{l['url']}")

    chunks, current = [], header
    for b in blocks:
        if len(current) + len(b) + 2 > limit and current != header:
            chunks.append(current.strip())
            current = header
        current += "\n" + b + "\n"
    chunks.append(current.strip())
    return chunks


# --------------------------------------------------------------------------
# Invio
# --------------------------------------------------------------------------
def need(name):
    v = os.getenv(name)
    if not v:
        sys.exit(f"Manca la variabile d'ambiente {name}")
    return v


def send_whatsapp(text):
    sid, token = need("TWILIO_ACCOUNT_SID"), need("TWILIO_AUTH_TOKEN")
    data = {"From": need("TWILIO_WHATSAPP_FROM"), "To": need("WHATSAPP_TO")}
    template = os.getenv("TWILIO_CONTENT_SID")
    if template:  # i template non ammettono a-capo nelle variabili
        data["ContentSid"] = template
        data["ContentVariables"] = json.dumps({"1": re.sub(r"\s*\n\s*", " | ", text)})
    else:
        data["Body"] = text
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=data, auth=(sid, token), timeout=30,
    )
    if not r.ok:
        sys.exit(f"Errore Twilio {r.status_code}: {r.text}")


def send_telegram(text):
    token, chat = need("TELEGRAM_BOT_TOKEN"), need("TELEGRAM_CHAT_ID")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"},
        timeout=30,
    )
    if not r.ok:
        sys.exit(f"Errore Telegram {r.status_code}: {r.text}")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="non invia e non salva")
    ap.add_argument("--all", action="store_true", help="ignora gli annunci già visti")
    args = ap.parse_args()

    listings = scrape(LISTING_URL)
    if not listings:
        sys.exit("Nessun annuncio trovato: la struttura del sito potrebbe essere cambiata.")

    if not os.getenv("INCLUDE_CARAVAN"):
        try:
            for url in scrape(CARAVAN_URL):
                listings.pop(url, None)
        except requests.RequestException as e:
            print(f"Avviso: non riesco a escludere le caravan ({e})", file=sys.stderr)

    matches = [l for l in listings.values() if l["price"] <= MAX_PRICE]
    seen = set() if args.all else load_state()
    new = [l for l in matches if l["url"] not in seen]

    print(f"Annunci letti: {len(listings)} | sotto soglia: {len(matches)} | nuovi: {len(new)}")
    if not new:
        return

    channel = os.getenv("CHANNEL", "whatsapp").lower()
    limit = 1000 if (channel == "whatsapp" and os.getenv("TWILIO_CONTENT_SID")) else 1500
    chunks = build_chunks(new, limit)

    if args.dry_run:
        print("\n\n---\n\n".join(chunks))
        return

    send = send_telegram if channel == "telegram" else send_whatsapp
    for c in chunks:
        send(c)

    save_state(seen | {l["url"] for l in new})
    print("Messaggio inviato e memoria aggiornata.")


if __name__ == "__main__":
    main()
