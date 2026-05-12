#!/usr/bin/env python3
"""
Merkandi.sk Scraper - s realistickými hlavičkami
"""

import json, time, re, os, hashlib, random
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
BASE_URL    = "https://merkandi.sk"

WEIGHTS = {"discount":.40, "unit_price":.25, "quantity":.20, "rating":.15}

# Rotácia User-Agentov — reálne prehliadače
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

def make_session():
    session = requests.Session()
    ua = random.choice(USER_AGENTS)
    session.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en-US;q=0.7,en;q=0.6",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "DNT": "1",
    })
    return session

def warm_up_session(session):
    """Navštív hlavnú stránku najprv — ako reálny používateľ."""
    try:
        print("  Warm-up: navštevujem hlavnú stránku...")
        r = session.get(BASE_URL, timeout=20, allow_redirects=True)
        print(f"  Hlavná stránka: {r.status_code}")
        # Nastav Referer pre ďalšie requesty
        session.headers.update({"Referer": BASE_URL + "/"})
        time.sleep(random.uniform(2, 4))
        return r.status_code == 200
    except Exception as e:
        print(f"  Warm-up chyba: {e}")
        return False

def get_page(session, url, retry=3):
    for attempt in range(retry):
        try:
            # Náhodný delay medzi requestmi
            if attempt > 0:
                wait = random.uniform(3, 7)
                print(f"  Retry {attempt+1}, čakám {wait:.1f}s...")
                time.sleep(wait)

            r = session.get(url, timeout=25, allow_redirects=True)
            print(f"  Status {r.status_code} — {url[:70]}")

            if r.status_code == 200:
                session.headers.update({"Referer": url})
                return r
            elif r.status_code == 403:
                print(f"  403 Forbidden — Merkandi blokuje tento request")
                # Skús zmeniť User-Agent
                session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
            elif r.status_code == 429:
                print(f"  429 Rate limit — čakám 30s...")
                time.sleep(30)

        except Exception as e:
            print(f"  Chyba: {e}")

    return None

def scrape_page(session, url):
    r = get_page(session, url)
    if not r:
        return [], False

    soup = BeautifulSoup(r.text, "html.parser")

    # Debug: vypíš title stránky
    title_tag = soup.find("title")
    print(f"  Page title: {title_tag.get_text()[:60] if title_tag else 'N/A'}")

    # Skontroluj či sme na CAPTCHA / login stránke
    page_text = soup.get_text().lower()
    if "captcha" in page_text or "robot" in page_text:
        print("  ! CAPTCHA detekovaná")
        return [], False
    if "prihlás" in page_text and "heslo" in page_text and len(soup.select("input[type=password]")) > 0:
        print("  ! Login stránka — presmerovanie na login")
        return [], False

    # Selektory pre karty ponúk
    card_selectors = [
        ".offer-box", ".offer_box", "[class*='offer-box']",
        ".offer-item", ".offer__item", ".offer",
        ".product-item", ".product_item",
        "article", ".grid-item", ".list-item",
        "[data-offer]", "[data-id]", "li[class]",
    ]

    cards = []
    used = None
    for sel in card_selectors:
        found = [c for c in soup.select(sel) if c.find("a", href=True)]
        if len(found) >= 2:
            cards = found; used = sel; break

    if not cards:
        # Fallback
        links = soup.find_all("a", href=re.compile(r"/(offer|product|stock|tovar|ponuka|sklady)"))
        seen_p, parents = set(), []
        for a in links:
            p = a.parent
            if id(p) not in seen_p:
                seen_p.add(id(p)); parents.append(p)
        cards = parents; used = "fallback"

    print(f"  Selektor '{used}' → {len(cards)} kariet")
    if cards:
        c0 = cards[0]
        print(f"  Prvá karta: <{c0.name}> classes={c0.get('class', [])[:3]}")

    offers = [o for c in cards for o in [extract_offer(c)] if o]
    has_next = bool(soup.select_one(
        "a[rel='next'], .pagination .next, [class*='next'], .pager-next, [aria-label='Next']"
    ))
    return offers, has_next

def extract_offer(card):
    title_el = card.select_one(
        "h1,h2,h3,h4,.offer-title,.offer__title,.product-title,"
        "[class*='title'],[class*='name'],strong"
    )
    title = title_el.get_text(strip=True) if title_el else ""
    if len(title) < 5:
        img = card.select_one("img[alt]")
        title = img["alt"].strip() if img else ""
    if len(title) < 5:
        return None

    link_el = card.select_one("a[href]")
    if not link_el: return None
    href = link_el["href"]
    link = href if href.startswith("http") else BASE_URL + href

    all_price_els = card.select("[class*='price'],[class*='Price'],[class*='cost']")
    current_price = original_price = None
    for el in all_price_els:
        cls = " ".join(el.get("class", []))
        val = parse_price(el.get_text(strip=True))
        if not val: continue
        if any(k in cls.lower() for k in ["old","original","before","was","prev"]):
            original_price = val
        elif current_price is None:
            current_price = val
    if not original_price:
        for tag in card.select("s,del,strike"):
            v = parse_price(tag.get_text())
            if v: original_price = v; break

    disc = round((1 - current_price/original_price)*100, 1) \
           if current_price and original_price and original_price > current_price else 0.0

    qty_el = card.select_one("[class*='quantity'],[class*='qty'],[class*='pcs'],[class*='min']")
    qty_nums = re.findall(r"\b\d[\d\s]*\b", qty_el.get_text() if qty_el else "")
    min_qty = int(qty_nums[0].replace(" ","")) if qty_nums else None
    unit_price = round(current_price/min_qty, 4) if current_price and min_qty else None

    rat_el = card.select_one("[class*='rating'],[class*='star'],[class*='score']")
    rn = re.findall(r"\d+\.?\d*", rat_el.get_text() if rat_el else "")
    rat = float(rn[0]) if rn else 0.0
    if rat > 10: rat /= 10

    cat_el = card.select_one("[class*='category'],[class*='cat'],[class*='tag']")
    cat = cat_el.get_text(strip=True) if cat_el else ""

    img_el = card.select_one("img[src],img[data-src],img[data-lazy]")
    img = ""
    if img_el:
        img = img_el.get("data-src") or img_el.get("data-lazy") or img_el.get("src") or ""
        if img.startswith("/"): img = BASE_URL + img

    return {"id": hashlib.md5((title+link).encode()).hexdigest()[:12],
            "title": title, "category": cat,
            "current_price": current_price, "original_price": original_price,
            "discount_pct": disc, "unit_price": unit_price, "min_quantity": min_qty,
            "seller_rating": round(rat, 1), "link": link, "image": img}

def parse_price(text):
    if not text: return None
    c = re.sub(r"[^\d,.]", "", text.strip())
    if not c: return None
    if "," in c and "." in c: c = c.replace(".", "").replace(",", ".")
    elif "," in c: c = c.replace(",", ".")
    try: v = float(c); return round(v,2) if v>0 else None
    except: return None

def crawl():
    session = make_session()
    ok = warm_up_session(session)
    if not ok:
        print("  ! Warm-up zlyhal, pokračujem...")

    # Skúsime rôzne vstupné URL
    candidate_urls = [
        f"{BASE_URL}/offers",
        f"{BASE_URL}/sklady",
        f"{BASE_URL}/categories/oblecenie/1",
        f"{BASE_URL}/categories/elektronika/2",
    ]

    working_url = None
    for url in candidate_urls:
        time.sleep(random.uniform(1.5, 3))
        r = get_page(session, url)
        if r and r.status_code == 200:
            working_url = url
            print(f"  ✓ Funguje: {url}")
            break

    if not working_url:
        print("  ! Všetky URL blokované (403)")
        return []

    seen, all_offers = set(), []
    for page in range(1, MAX_PAGES + 1):
        sep = "&" if "?" in working_url else "?"
        url = working_url if page == 1 else f"{working_url}{sep}page={page}"
        print(f"\n  → Strana {page}:")
        offers, has_next = scrape_page(session, url)
        new = [o for o in offers if o["id"] not in seen]
        seen.update(o["id"] for o in new)
        all_offers.extend(new)
        print(f"  +{len(new)} nových ({len(all_offers)} celkom)")
        if not has_next or not new: break
        time.sleep(random.uniform(2.5, 5))

    return all_offers

def score_offer(o):
    d = o["discount_pct"]
    sd = min(100, d*1.25)
    cpu = o["unit_price"] or o["current_price"] or 999
    sp = 100 if cpu<=1 else 85 if cpu<=3 else 70 if cpu<=5 else 45 if cpu<=15 else 20 if cpu<=50 else 5
    mq = o["min_quantity"]
    sq = 50 if not mq else 100 if mq<=5 else 80 if mq<=20 else 55 if mq<=100 else 25 if mq<=500 else 5
    sr = min(100, o["seller_rating"]*20)
    o["score"] = round(WEIGHTS["discount"]*sd + WEIGHTS["unit_price"]*sp +
                       WEIGHTS["quantity"]*sq + WEIGHTS["rating"]*sr, 1)
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

def main():
    t0 = time.time()
    print(f"\n{'='*52}")
    print(f"  Merkandi Scraper  {datetime.now().strftime('%d.%m.%Y %H:%M UTC')}")
    print(f"{'='*52}\n")

    raw = crawl()
    print(f"\n  Nájdených: {len(raw)}")

    if not raw:
        print("\n⚠️  Scraper nenašiel žiadne ponuky!")
        print("   Merkandi blokuje GitHub Actions IP adresy (403).")
        print("   Riešenie: prejdi na Playwright alebo lokálny scraper.\n")
        result = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "total": 0, "offers": [],
                  "error": "403 Forbidden — Merkandi blokuje GitHub Actions"}
        OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        # Nevypíšeme exit code 1 — necháme workflow dokončiť (data.json sa commitne)
        return

    scored = sorted([score_offer(o) for o in raw], key=lambda x: -x["score"])
    result = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "total": len(scored), "offers": scored[:200]}
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 data.json uložený ({len(scored)} ponúk)")

    if USE_SHEETS:
        try: write_sheets(scored)
        except Exception as e: print(f"  Sheets chyba: {e}")

    print(f"\n🏆 TOP 5:")
    for i, o in enumerate(scored[:5], 1):
        print(f"  {i}. [{o['score']:.0f}] {o['title'][:55]}")
    print(f"\n✅ Hotovo za {time.time()-t0:.1f}s\n")

if __name__ == "__main__":
    main()
