#!/usr/bin/env python3
"""
Merkandi.sk Scraper
- Stiahne ponuky z merkandi.sk
- Ohodnotí ich (zľava, cena/ks, množstvo, rating)
- Zapíše data.json  → GitHub Pages dashboard ho číta
- Zapíše do Google Sheets → história / záloha (voliteľné)
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

OUTPUT_JSON  = Path("data.json")
MAX_PAGES    = 8
DELAY        = 1.5
BASE_URL     = "https://merkandi.sk"
LISTING_URLS = [f"{BASE_URL}/offers", f"{BASE_URL}/offers?sort=newest"]
WEIGHTS      = {"discount":.40, "unit_price":.25, "quantity":.20, "rating":.15}
HEADERS      = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "sk-SK,sk;q=0.9",
}

def parse_price(text):
    if not text: return None
    c = re.sub(r"[^\d,.]", "", text.strip())
    if "," in c and "." in c: c = c.replace(".", "").replace(",", ".")
    elif "," in c: c = c.replace(",", ".")
    try: v = float(c); return round(v,2) if v>0 else None
    except: return None

def make_id(title, link):
    return hashlib.md5((title+link).encode()).hexdigest()[:12]

def scrape_page(session, url):
    try:
        r = session.get(url, timeout=20); r.raise_for_status()
    except Exception as e:
        print(f"    warning: {e}"); return []
    soup = BeautifulSoup(r.text, "html.parser")
    cards = (soup.select(".offer-item") or soup.select(".offer__item") or
             soup.select("[data-offer-id]") or soup.select(".product-card") or
             soup.select("article.offer") or
             [el for el in soup.select("article,li")
              if el.find("a", href=re.compile(r"/offer"))])
    return [o for c in cards for o in [extract(c)] if o]

def extract(card):
    t = card.select_one("h2,h3,.offer-title,.offer__title,.title,[class*='title'],[class*='name']")
    title = t.get_text(strip=True) if t else ""
    if len(title) < 5: return None
    a = card.select_one("a[href]")
    if not a: return None
    href = a["href"]
    link = href if href.startswith("http") else BASE_URL + href
    pe = card.select_one(".price,.offer-price,.offer__price")
    oe = card.select_one(".original-price,.price-old,.offer__price--old,s,del,strike")
    cp = parse_price(pe.get_text() if pe else "")
    op = parse_price(oe.get_text() if oe else "")
    disc = round((1 - cp/op)*100, 1) if cp and op and op > cp else 0.0
    qe = card.select_one("[class*='quantity'],[class*='amount'],[class*='pcs'],[class*='min']")
    qt = re.findall(r"\d[\d\s]*", qe.get_text() if qe else "")
    mq = int(qt[0].replace(" ","")) if qt else None
    up = round(cp/mq, 4) if cp and mq else None
    re_el = card.select_one("[class*='rating'],[class*='star'],.rating")
    rn = re.findall(r"\d+\.?\d*", re_el.get_text() if re_el else "")
    rat = float(rn[0]) if rn else 0.0
    if rat > 10: rat /= 10
    ce = card.select_one("[class*='category'],[class*='cat']")
    cat = ce.get_text(strip=True) if ce else ""
    ie = card.select_one("img[src],img[data-src]")
    img = ""
    if ie:
        img = ie.get("data-src") or ie.get("src") or ""
        if img and not img.startswith("http"): img = BASE_URL + img
    return {"id":make_id(title,link),"title":title,"category":cat,
            "current_price":cp,"original_price":op,"discount_pct":disc,
            "unit_price":up,"min_quantity":mq,"seller_rating":round(rat,1),
            "link":link,"image":img}

def crawl():
    session = requests.Session(); session.headers.update(HEADERS)
    seen, all_ = set(), []
    for base in LISTING_URLS:
        for page in range(1, MAX_PAGES+1):
            url = (f"{base}&page={page}" if "?" in base else
                   (base if page==1 else f"{base}?page={page}"))
            print(f"  -> {url}")
            offers = scrape_page(session, url)
            if not offers: break
            new = [o for o in offers if o["id"] not in seen]
            seen.update(o["id"] for o in new); all_.extend(new)
            print(f"     +{len(new)} ({len(all_)} total)")
            time.sleep(DELAY)
    return all_

def score(o):
    d = o["discount_pct"]
    sd = min(100, d*1.25)
    cpu = o["unit_price"] or o["current_price"] or 999
    sp = 100 if cpu<=1 else 85 if cpu<=3 else 70 if cpu<=5 else 45 if cpu<=15 else 20 if cpu<=50 else 5
    mq = o["min_quantity"]
    sq = 50 if not mq else 100 if mq<=5 else 80 if mq<=20 else 55 if mq<=100 else 25 if mq<=500 else 5
    sr = min(100, o["seller_rating"]*20)
    o["score"] = round(WEIGHTS["discount"]*sd+WEIGHTS["unit_price"]*sp+
                       WEIGHTS["quantity"]*sq+WEIGHTS["rating"]*sr, 1)
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
        print(f"  Sheets: {len(rows)} novych riadkov")
    else:
        print("  Sheets: ziadne nove")

def main():
    t0 = time.time()
    print(f"\n{'='*50}")
    print(f"  Merkandi Scraper  {datetime.now().strftime('%d.%m.%Y %H:%M UTC')}")
    print(f"{'='*50}\n")
    print("Stahujem ponuky...")
    raw = crawl()
    print(f"\n  Najdenych: {len(raw)}\n")
    print("Hodnotim...")
    scored = sorted([score(o) for o in raw], key=lambda x: -x["score"])
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(scored),
        "offers": scored[:200]
    }
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"data.json ulozeny ({len(scored)} ponuk)\n")
    if USE_SHEETS:
        print("Zapisujem do Google Sheets...")
        try: write_sheets(scored)
        except Exception as e: print(f"  Sheets chyba: {e}")
    print(f"\nTOP 5:")
    for i, o in enumerate(scored[:5], 1):
        print(f"  {i}. [{o['score']:.0f}] {o['title'][:55]}")
    print(f"\nHotovo za {time.time()-t0:.1f}s\n")

if __name__ == "__main__":
    main()
