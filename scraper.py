#!/usr/bin/env python3
"""
Merkandi.sk Scraper
- Prehľadáva kategórie na merkandi.sk
- Ohodnotí ponuky (zľava, cena/ks, množstvo, rating)
- Zapíše data.json pre GitHub Pages dashboard
- Voliteľne zapíše do Google Sheets
"""

import json, time, re, os, hashlib
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SPREADSHEET_ID   = os.environ.get("SPREADSHEET_ID", "")
GOOGLE_CREDS_RAW = os.environ.get("GOOGLE_CREDENTIALS", "")
USE_SHEETS = bool(SPREADSHEET_ID and GOOGLE_CREDS_RAW)
if USE_SHEETS:
    import gspread
    from google.oauth2.service_account import Credentials

OUTPUT_JSON = Path("data.json")
MAX_PAGES   = 5
DELAY       = 2.0
BASE_URL    = "https://merkandi.sk"

# Hlavné kategórie Merkandi — ID z ich URL
CATEGORIES = [
    ("Všetko", f"{BASE_URL}/offers"),
    ("Oblečenie", f"{BASE_URL}/categories/oblecenie/1"),
    ("Elektro", f"{BASE_URL}/categories/elektronika/2"),
    ("Dom a záhrada", f"{BASE_URL}/categories/dom-zahrada/3"),
    ("Obuv", f"{BASE_URL}/categories/obuv/4"),
    ("Hračky", f"{BASE_URL}/categories/hracky/5"),
]

WEIGHTS = {"discount":.40, "unit_price":.25, "quantity":.20, "rating":.15}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://merkandi.sk/",
}

def parse_price(text):
    if not text: return None
    # Odstráň meny a medzery, nechaj len čísla a oddeľovače
    c = re.sub(r"[^\d,.]", "", text.strip())
    if not c: return None
    # Európsky formát: 1.234,56
    if "," in c and "." in c:
        c = c.replace(".", "").replace(",", ".")
    elif "," in c:
        c = c.replace(",", ".")
    try:
        v = float(c)
        return round(v, 2) if v > 0 else None
    except:
        return None

def make_id(title, link):
    return hashlib.md5((title + link).encode()).hexdigest()[:12]

def scrape_page(session, url):
    try:
        r = session.get(url, timeout=25)
        r.raise_for_status()
    except Exception as e:
        print(f"    ! Request failed: {e}")
        return [], False

    soup = BeautifulSoup(r.text, "html.parser")

    # Merkandi používa grid/list zobrazenie — skúšame všetky možné selektory
    # v poradí od najpravdepodobnejšieho
    card_selectors = [
        ".offer-box",
        ".offer_box",
        ".offer-item",
        ".offer__item",
        ".offer",
        "[class*='offer-box']",
        "[class*='offerBox']",
        ".product-item",
        ".product_item",
        "[class*='product-item']",
        "article",
        ".grid-item",
        "[class*='grid-item']",
        ".list-item",
        "[data-offer]",
        "[data-id]",
    ]

    cards = []
    used_selector = None
    for sel in card_selectors:
        found = soup.select(sel)
        # Musí obsahovať aspoň odkaz na ponuku
        valid = [c for c in found if c.find("a", href=True)]
        if len(valid) >= 2:
            cards = valid
            used_selector = sel
            break

    if not cards:
        # Fallback: hľadáme akékoľvek elementy s odkazom obsahujúcim kľúčové slová
        all_links = soup.find_all("a", href=re.compile(r"/(offer|product|stock|tovar|ponuka)"))
        # Vezmi ich rodičov
        parents = []
        seen_parents = set()
        for a in all_links:
            p = a.parent
            pid = id(p)
            if pid not in seen_parents:
                seen_parents.add(pid)
                parents.append(p)
        cards = parents
        used_selector = "fallback-links"

    print(f"    selektor: '{used_selector}' → {len(cards)} kariet")

    # Debug: vypíš CSS triedy prvých kariet
    if cards and len(cards) > 0:
        first = cards[0]
        print(f"    prvá karta tag: <{first.name}> classes: {first.get('class', [])}")

    offers = [o for c in cards for o in [extract_offer(c)] if o]
    
    # Zisti či existuje ďalšia stránka
    next_page = bool(soup.select_one(
        "a[rel='next'], .pagination .next, [class*='next']:not([class*='prev']), "
        ".pager-next, [aria-label='Next']"
    ))

    return offers, next_page

def extract_offer(card):
    # Názov — hľadáme nadpis alebo silný text
    title_el = card.select_one(
        "h1, h2, h3, h4, "
        ".offer-title, .offer__title, .product-title, "
        "[class*='title'], [class*='name'], strong, b"
    )
    title = title_el.get_text(strip=True) if title_el else ""
    # Ak nemáme nadpis, skús alt obrázka
    if len(title) < 5:
        img = card.select_one("img[alt]")
        title = img["alt"].strip() if img else ""
    if len(title) < 5:
        return None

    # Odkaz
    link_el = card.select_one("a[href]")
    if not link_el:
        return None
    href = link_el["href"]
    link = href if href.startswith("http") else BASE_URL + href

    # Ceny — hľadáme aktuálnu a pôvodnú
    all_price_els = card.select("[class*='price'], [class*='Price'], [class*='cost']")
    current_price = None
    original_price = None

    for el in all_price_els:
        cls_str = " ".join(el.get("class", []))
        text = el.get_text(strip=True)
        val = parse_price(text)
        if not val:
            continue
        if any(kw in cls_str.lower() for kw in ["old", "original", "before", "was", "prev", "strike"]):
            original_price = val
        elif current_price is None:
            current_price = val

    # Fallback: skús s,del,strike tagmi pre pôvodnú cenu
    if not original_price:
        for tag in card.select("s, del, strike"):
            v = parse_price(tag.get_text())
            if v:
                original_price = v
                break

    # Výpočet zľavy
    if current_price and original_price and original_price > current_price:
        discount_pct = round((1 - current_price / original_price) * 100, 1)
    else:
        discount_pct = 0.0

    # Minimálne množstvo
    qty_el = card.select_one(
        "[class*='quantity'], [class*='amount'], [class*='qty'], "
        "[class*='pcs'], [class*='min'], [class*='units']"
    )
    qty_text = qty_el.get_text(strip=True) if qty_el else ""
    # Hľadaj číslo v texte
    qty_nums = re.findall(r"\b\d[\d\s]*\b", qty_text)
    min_qty = int(qty_nums[0].replace(" ", "")) if qty_nums else None

    # Cena za kus
    unit_price = round(current_price / min_qty, 4) if current_price and min_qty and min_qty > 0 else None

    # Hodnotenie predajcu
    rating_el = card.select_one(
        "[class*='rating'], [class*='star'], [class*='score'], [class*='review']"
    )
    seller_rating = 0.0
    if rating_el:
        rn = re.findall(r"\d+\.?\d*", rating_el.get_text())
        if rn:
            seller_rating = float(rn[0])
            if seller_rating > 10:
                seller_rating /= 10

    # Kategória
    cat_el = card.select_one("[class*='category'], [class*='cat'], [class*='tag'], [class*='label']")
    category = cat_el.get_text(strip=True) if cat_el else ""

    # Obrázok
    img_el = card.select_one("img[src], img[data-src], img[data-lazy]")
    image = ""
    if img_el:
        image = (img_el.get("data-src") or img_el.get("data-lazy") or img_el.get("src") or "")
        if image and image.startswith("/"):
            image = BASE_URL + image

    return {
        "id": make_id(title, link),
        "title": title,
        "category": category,
        "current_price": current_price,
        "original_price": original_price,
        "discount_pct": discount_pct,
        "unit_price": unit_price,
        "min_quantity": min_qty,
        "seller_rating": round(seller_rating, 1),
        "link": link,
        "image": image,
    }

def crawl():
    session = requests.Session()
    session.headers.update(HEADERS)
    seen, all_offers = set(), []

    # Skúsime najprv hlavnú stránku ponúk
    urls_to_try = [
        f"{BASE_URL}/offers",
        f"{BASE_URL}/offers/list",
        f"{BASE_URL}/stock",
        f"{BASE_URL}/categories",
    ]

    working_base = None
    for url in urls_to_try:
        print(f"  Testujem URL: {url}")
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                working_base = url
                print(f"  ✓ Funguje: {url}")
                break
            else:
                print(f"  ✗ Status {r.status_code}")
        except Exception as e:
            print(f"  ✗ Chyba: {e}")
        time.sleep(1)

    if not working_base:
        print("  ! Žiadna URL nefunguje, skúšam kategórie...")
        working_base = f"{BASE_URL}/categories/oblecenie/1"

    for page in range(1, MAX_PAGES + 1):
        sep = "&" if "?" in working_base else "?"
        url = working_base if page == 1 else f"{working_base}{sep}page={page}"
        print(f"  → Strana {page}: {url}")

        offers, has_next = scrape_page(session, url)

        new = [o for o in offers if o["id"] not in seen]
        seen.update(o["id"] for o in new)
        all_offers.extend(new)
        print(f"    +{len(new)} nových ({len(all_offers)} celkom)")

        if not has_next or not new:
            print("  Koniec stránkovania.")
            break
        time.sleep(DELAY)

    return all_offers

def score_offer(o):
    d = o["discount_pct"]
    sd = min(100, d * 1.25)
    cpu = o["unit_price"] or o["current_price"] or 999
    sp = 100 if cpu<=1 else 85 if cpu<=3 else 70 if cpu<=5 else 45 if cpu<=15 else 20 if cpu<=50 else 5
    mq = o["min_quantity"]
    sq = 50 if not mq else 100 if mq<=5 else 80 if mq<=20 else 55 if mq<=100 else 25 if mq<=500 else 5
    sr = min(100, o["seller_rating"] * 20)
    o["score"] = round(
        WEIGHTS["discount"] * sd + WEIGHTS["unit_price"] * sp +
        WEIGHTS["quantity"] * sq + WEIGHTS["rating"] * sr, 1
    )
    return o

def write_sheets(offers):
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_RAW),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)
    sp = client.open_by_key(SPREADSHEET_ID)
    tabs = [w.title for w in sp.worksheets()]
    if "ponuky" not in tabs:
        ws = sp.add_worksheet("ponuky", 5000, 14)
        ws.append_row(["id","datum","nazov","kategoria","cena_eur","orig_cena_eur",
                        "zlava_pct","cena_za_kus","min_mnozstvo","hodnotenie_predajcu",
                        "skore","link","obrazok","aktivna"])
        ws.freeze(rows=1)
    else:
        ws = sp.worksheet("ponuky")
    existing = set(ws.col_values(1)[1:])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    rows = [[o["id"],now,o["title"],o["category"],o["current_price"] or "",
             o["original_price"] or "",o["discount_pct"],o["unit_price"] or "",
             o["min_quantity"] or "",o["seller_rating"],o["score"],
             o["link"],o["image"],"ano"]
            for o in offers if o["id"] not in existing]
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"  Sheets: {len(rows)} novych")
    else:
        print("  Sheets: ziadne nove")

def main():
    t0 = time.time()
    print(f"\n{'='*52}")
    print(f"  Merkandi Scraper  {datetime.now().strftime('%d.%m.%Y %H:%M UTC')}")
    print(f"{'='*52}\n")

    print("📥 Sťahujem ponuky...")
    raw = crawl()
    print(f"\n  Nájdených: {len(raw)}\n")

    if not raw:
        print("⚠️  Scraper nenašiel žiadne ponuky!")
        print("   Merkandi možno zmenil štruktúru stránky.")
        print("   Skontroluj logy a uprav selektory v scraper.py\n")
        # Ulož prázdny data.json aby dashboard nezobrazoval chybu
        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total": 0,
            "offers": [],
            "error": "Scraper nenašiel ponuky — skontroluj selektory"
        }
        OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print("🔢 Hodnotím ponuky...")
    scored = sorted([score_offer(o) for o in raw], key=lambda x: -x["score"])

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(scored),
        "offers": scored[:200]
    }
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 data.json uložený ({len(scored)} ponúk)\n")

    if USE_SHEETS:
        print("📊 Zapisujem do Google Sheets...")
        try:
            write_sheets(scored)
        except Exception as e:
            print(f"  ⚠️  Sheets chyba: {e}")

    print(f"\n🏆 TOP 5:")
    for i, o in enumerate(scored[:5], 1):
        print(f"  {i}. [{o['score']:.0f}] {o['title'][:55]}")
        print(f"      {o['current_price']} € | -{o['discount_pct']}% | qty: {o['min_quantity']}")

    print(f"\n✅ Hotovo za {time.time()-t0:.1f}s\n")

if __name__ == "__main__":
    main()
