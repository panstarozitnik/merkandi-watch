#!/usr/bin/env python3
"""
Merkandi.sk Scraper → Google Sheets
Spúšťa sa automaticky cez GitHub Actions každý deň o 7:00.
"""

import json
import time
import re
import os
import hashlib
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ─── KONFIGURÁCIA ─────────────────────────────────────────────────────────────

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]      # z GitHub Secrets
GOOGLE_CREDS   = os.environ["GOOGLE_CREDENTIALS"]  # JSON string zo Secrets

SHEET_TAB_OFFERS  = "ponuky"
SHEET_TAB_LOG     = "log"

BASE_URL     = "https://merkandi.sk"
LISTING_URLS = [
    f"{BASE_URL}/offers",              # všetky ponuky
    f"{BASE_URL}/offers?sort=newest",  # najnovšie
]

MAX_PAGES = 8          # koľko stránok prechádzame
DELAY_SEC = 1.5        # pauza medzi requestmi (slušnosť)

# Váhy hodnotenia (spolu = 1.0)
WEIGHTS = {
    "discount":  0.40,
    "unit_price": 0.25,
    "quantity":  0.20,
    "rating":    0.15,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "sk-SK,sk;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

def connect_sheets():
    creds_data = json.loads(GOOGLE_CREDS)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

def ensure_tabs(spreadsheet):
    """Vytvorí tabuľky ak neexistujú."""
    existing = [ws.title for ws in spreadsheet.worksheets()]

    if SHEET_TAB_OFFERS not in existing:
        ws = spreadsheet.add_worksheet(title=SHEET_TAB_OFFERS, rows=5000, cols=20)
        ws.append_row([
            "id", "datum", "nazov", "kategoria", "cena_eur",
            "orig_cena_eur", "zlava_pct", "cena_za_kus",
            "min_mnozstvo", "hodnotenie_predajcu",
            "skore", "link", "obrazok", "aktivna"
        ], value_input_option="RAW")
        # Zmraziť hlavičku
        ws.freeze(rows=1)
        print(f"  ✅ Vytvorený tab '{SHEET_TAB_OFFERS}'")

    if SHEET_TAB_LOG not in existing:
        ws = spreadsheet.add_worksheet(title=SHEET_TAB_LOG, rows=1000, cols=8)
        ws.append_row(["datum", "celkom_scraped", "novych", "aktualizovanych", "chyby", "trvanie_s"])
        print(f"  ✅ Vytvorený tab '{SHEET_TAB_LOG}'")

def load_existing_ids(ws_offers):
    """Načíta všetky existujúce ID z tabuľky."""
    try:
        ids = ws_offers.col_values(1)[1:]  # preskočiť hlavičku
        return set(ids)
    except:
        return set()

# ─── SCRAPING ─────────────────────────────────────────────────────────────────

def parse_price(text):
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.]", "", text.strip())
    # Európsky formát: 1.234,56 → 1234.56
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        val = float(cleaned)
        return round(val, 2) if val > 0 else None
    except:
        return None

def make_offer_id(title, link):
    """Unikátny hash pre každú ponuku."""
    raw = (title + link).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:12]

def scrape_page(session, url):
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"    ⚠️  Request failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Merkandi používa rôzne selektory — skúšame viacero
    cards = (
        soup.select(".offer-item") or
        soup.select(".offer__item") or
        soup.select("[data-offer-id]") or
        soup.select(".product-card") or
        soup.select("article.offer") or
        soup.select(".list-item")
    )

    if not cards:
        # Fallback: akékoľvek article/li s odkazom na /offer/
        cards = [
            el for el in soup.select("article, li")
            if el.find("a", href=re.compile(r"/offer/|/offers/"))
        ]

    offers = []
    for card in cards:
        try:
            o = extract_offer(card)
            if o:
                offers.append(o)
        except Exception as e:
            continue
    return offers

def extract_offer(card):
    # Názov
    title_el = card.select_one(
        "h2, h3, .offer-title, .offer__title, .title, [class*='title'], [class*='name']"
    )
    title = title_el.get_text(strip=True) if title_el else ""
    if len(title) < 5:
        return None

    # Odkaz
    link_el = card.select_one("a[href]")
    if not link_el:
        return None
    href = link_el["href"]
    link = href if href.startswith("http") else BASE_URL + href

    # Ceny
    price_el = card.select_one(
        ".price, .offer-price, .offer__price, [class*='price']:not([class*='original']):not([class*='old'])"
    )
    orig_el  = card.select_one(
        ".original-price, .price-old, .offer__price--old, [class*='original'], [class*='old'], s, del, strike"
    )
    current_price  = parse_price(price_el.get_text()  if price_el  else "")
    original_price = parse_price(orig_el.get_text()   if orig_el   else "")

    # Výpočet zľavy
    if current_price and original_price and original_price > current_price:
        discount_pct = round((1 - current_price / original_price) * 100, 1)
    else:
        discount_pct = 0.0

    # Množstvo
    qty_el = card.select_one(
        "[class*='quantity'], [class*='amount'], [class*='pcs'], [class*='units'], [class*='min']"
    )
    qty_text    = qty_el.get_text(strip=True) if qty_el else ""
    qty_numbers = re.findall(r"\d[\d\s]*", qty_text)
    min_qty     = int(qty_numbers[0].replace(" ", "")) if qty_numbers else None

    # Cena za kus
    unit_price = None
    if current_price and min_qty and min_qty > 0:
        unit_price = round(current_price / min_qty, 4)

    # Hodnotenie
    rating_el = card.select_one("[class*='rating'], [class*='star'], .rating, [class*='score']")
    rating_text = rating_el.get_text(strip=True) if rating_el else ""
    rating_nums = re.findall(r"\d+\.?\d*", rating_text)
    seller_rating = float(rating_nums[0]) if rating_nums else 0.0
    if seller_rating > 10:
        seller_rating = seller_rating / 10  # normalizácia

    # Kategória
    cat_el = card.select_one("[class*='category'], [class*='cat'], [class*='tag']")
    category = cat_el.get_text(strip=True) if cat_el else ""

    # Obrázok
    img_el = card.select_one("img[src], img[data-src]")
    image = ""
    if img_el:
        image = img_el.get("data-src") or img_el.get("src") or ""
        if image and not image.startswith("http"):
            image = BASE_URL + image

    return {
        "id":             make_offer_id(title, link),
        "title":          title,
        "category":       category,
        "current_price":  current_price,
        "original_price": original_price,
        "discount_pct":   discount_pct,
        "unit_price":     unit_price,
        "min_quantity":   min_qty,
        "seller_rating":  round(seller_rating, 1),
        "link":           link,
        "image":          image,
    }

def crawl_all(max_pages=MAX_PAGES):
    session = requests.Session()
    session.headers.update(HEADERS)
    seen_ids = set()
    all_offers = []

    for base_url in LISTING_URLS:
        for page in range(1, max_pages + 1):
            url = f"{base_url}&page={page}" if "?" in base_url else \
                  (base_url if page == 1 else f"{base_url}?page={page}")
            print(f"  → {url}")
            offers = scrape_page(session, url)
            if not offers:
                print(f"    Prázdna stránka, idem ďalej.")
                break
            new = [o for o in offers if o["id"] not in seen_ids]
            seen_ids.update(o["id"] for o in new)
            all_offers.extend(new)
            print(f"    {len(new)} nových ({len(all_offers)} celkom)")
            time.sleep(DELAY_SEC)

    return all_offers

# ─── SCORING ──────────────────────────────────────────────────────────────────

def score_offer(o):
    s = {}

    # Zľava %
    d = o["discount_pct"]
    s["discount"] = min(100, d * 1.25)  # 80% zľava = 100 bodov

    # Cena za kus
    cpu = o["unit_price"] or o["current_price"] or 999
    if   cpu <= 1:   s["unit_price"] = 100
    elif cpu <= 3:   s["unit_price"] = 85
    elif cpu <= 5:   s["unit_price"] = 70
    elif cpu <= 15:  s["unit_price"] = 45
    elif cpu <= 50:  s["unit_price"] = 20
    else:            s["unit_price"] = 5

    # Min. množstvo
    mq = o["min_quantity"]
    if   not mq:    s["quantity"] = 50
    elif mq <= 5:   s["quantity"] = 100
    elif mq <= 20:  s["quantity"] = 80
    elif mq <= 100: s["quantity"] = 55
    elif mq <= 500: s["quantity"] = 25
    else:           s["quantity"] = 5

    # Hodnotenie predajcu
    r = o["seller_rating"]
    s["rating"] = min(100, r * 20)

    total = sum(WEIGHTS[k] * s[k] for k in WEIGHTS)
    o["score"] = round(total, 1)
    return o

# ─── ZÁPIS DO SHEETS ──────────────────────────────────────────────────────────

def write_to_sheets(spreadsheet, offers):
    ws = spreadsheet.worksheet(SHEET_TAB_OFFERS)
    existing_ids = load_existing_ids(ws)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    new_rows   = []
    upd_count  = 0

    for o in offers:
        row = [
            o["id"],
            now,
            o["title"],
            o["category"],
            o["current_price"]  or "",
            o["original_price"] or "",
            o["discount_pct"],
            o["unit_price"]     or "",
            o["min_quantity"]   or "",
            o["seller_rating"],
            o["score"],
            o["link"],
            o["image"],
            "áno",
        ]
        if o["id"] not in existing_ids:
            new_rows.append(row)

    # Batch append nových riadkov (1 API call)
    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
        print(f"  ✅ Zapísaných {len(new_rows)} nových ponúk")
    else:
        print("  ℹ️  Žiadne nové ponuky (všetky už existujú)")

    return len(new_rows), upd_count

def write_log(spreadsheet, total, new_count, upd_count, errors, duration):
    ws = spreadsheet.worksheet(SHEET_TAB_LOG)
    ws.append_row([
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        total, new_count, upd_count, errors, round(duration, 1)
    ])

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    t_start = time.time()
    errors  = 0
    print(f"\n{'━'*52}")
    print(f"  Merkandi Scraper  {datetime.now().strftime('%d.%m.%Y %H:%M UTC')}")
    print(f"{'━'*52}\n")

    print("🔗 Pripájam sa na Google Sheets...")
    spreadsheet = connect_sheets()
    ensure_tabs(spreadsheet)
    print(f"  Sheet: {spreadsheet.title}\n")

    print("📥 Sťahujem ponuky z Merkandi...")
    raw_offers = crawl_all()
    print(f"\n  Celkom nájdených: {len(raw_offers)} ponúk\n")

    print("🔢 Hodnotím ponuky...")
    scored = [score_offer(o) for o in raw_offers]
    scored.sort(key=lambda x: x["score"], reverse=True)

    print("\n💾 Zapisujem do Google Sheets...")
    new_count, upd_count = write_to_sheets(spreadsheet, scored)

    duration = time.time() - t_start
    write_log(spreadsheet, len(scored), new_count, upd_count, errors, duration)

    print(f"\n🏆 TOP 5 dnes:")
    for i, o in enumerate(scored[:5], 1):
        print(f"  {i}. [{o['score']:.0f}] {o['title'][:55]}")
        print(f"      {o['current_price']} € | -{o['discount_pct']}% | {o['link']}")

    print(f"\n✅ Hotovo za {duration:.1f}s — {new_count} nových ponúk v Sheets\n")

if __name__ == "__main__":
    main()
