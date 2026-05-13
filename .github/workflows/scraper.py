#!/usr/bin/env python3
"""
Merkandi.sk Scraper — Playwright (reálny Chrome, obchádza 403)
"""

import json, re, os, hashlib, asyncio
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

SPREADSHEET_ID   = os.environ.get("SPREADSHEET_ID", "")
GOOGLE_CREDS_RAW = os.environ.get("GOOGLE_CREDENTIALS", "")
USE_SHEETS = bool(SPREADSHEET_ID and GOOGLE_CREDS_RAW)
if USE_SHEETS:
    import gspread
    from google.oauth2.service_account import Credentials

OUTPUT_JSON = Path("data.json")
BASE_URL    = "https://merkandi.sk"
MAX_PAGES   = 6
WEIGHTS     = {"discount":.40, "unit_price":.25, "quantity":.20, "rating":.15}

def parse_price(text):
    if not text: return None
    c = re.sub(r"[^\d,.]", "", text.strip())
    if not c: return None
    if "," in c and "." in c: c = c.replace(".", "").replace(",", ".")
    elif "," in c: c = c.replace(",", ".")
    try: v = float(c); return round(v,2) if v>0 else None
    except: return None

def make_id(title, link):
    return hashlib.md5((title+link).encode()).hexdigest()[:12]

def score_offer(o):
    sd = min(100, o["discount_pct"]*1.25)
    cpu = o["unit_price"] or o["current_price"] or 999
    sp = 100 if cpu<=1 else 85 if cpu<=3 else 70 if cpu<=5 else 45 if cpu<=15 else 20 if cpu<=50 else 5
    mq = o["min_quantity"]
    sq = 50 if not mq else 100 if mq<=5 else 80 if mq<=20 else 55 if mq<=100 else 25 if mq<=500 else 5
    sr = min(100, o["seller_rating"]*20)
    o["score"] = round(WEIGHTS["discount"]*sd + WEIGHTS["unit_price"]*sp +
                       WEIGHTS["quantity"]*sq + WEIGHTS["rating"]*sr, 1)
    return o

async def scrape():
    all_offers = []
    seen = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="sk-SK",
            timezone_id="Europe/Bratislava",
        )

        # Skry že ide o automatizovaný prehliadač
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['sk-SK','sk','cs','en']});
        """)

        page = await context.new_page()

        # 1. Najprv navštív hlavnú stránku (ako reálny user)
        print("  Otvaram hlavnu stranku...")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        print(f"  Title: {await page.title()}")

        # 2. Prejdi na ponuky
        offers_url = f"{BASE_URL}/offers"
        print(f"  Prechadzam na: {offers_url}")
        await page.goto(offers_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        print(f"  Title: {await page.title()}")

        for page_num in range(1, MAX_PAGES + 1):
            if page_num > 1:
                next_url = f"{offers_url}?page={page_num}"
                print(f"\n  Stranka {page_num}: {next_url}")
                await page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

            # Scroll dolu — načítaj lazy obsah
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await page.wait_for_timeout(1000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

            # Extrahuj ponuky cez JavaScript priamo v prehliadači
            offers_data = await page.evaluate("""
            () => {
                const results = [];

                // Skúsime rôzne selektory pre karty
                const selectors = [
                    '.offer-box', '.offer_box', '[class*="offer-box"]',
                    '.offer-item', '.offer__item',
                    '.product-item', '.product_item',
                    'article', '.grid-item', '.list-item',
                    '[data-offer-id]', '[data-id]',
                ];

                let cards = [];
                let usedSel = null;
                for (const sel of selectors) {
                    const found = [...document.querySelectorAll(sel)]
                        .filter(el => el.querySelector('a[href]'));
                    if (found.length >= 2) {
                        cards = found;
                        usedSel = sel;
                        break;
                    }
                }

                console.log('Selector:', usedSel, 'Cards:', cards.length);

                for (const card of cards) {
                    // Názov
                    const titleEl = card.querySelector('h1,h2,h3,h4,[class*="title"],[class*="name"],strong');
                    let title = titleEl ? titleEl.innerText.trim() : '';
                    if (!title) {
                        const img = card.querySelector('img[alt]');
                        title = img ? img.alt.trim() : '';
                    }
                    if (title.length < 5) continue;

                    // Odkaz
                    const linkEl = card.querySelector('a[href]');
                    if (!linkEl) continue;
                    const link = linkEl.href;

                    // Ceny
                    let currentPrice = null, originalPrice = null;
                    const priceEls = card.querySelectorAll('[class*="price"],[class*="Price"],[class*="cost"]');
                    for (const el of priceEls) {
                        const cls = el.className || '';
                        const txt = el.innerText.replace(/[^\\d,.]/g, '');
                        let val = parseFloat(txt.replace(',','.'));
                        if (!val || val <= 0) continue;
                        if (/old|original|before|was|prev/i.test(cls)) {
                            originalPrice = val;
                        } else if (!currentPrice) {
                            currentPrice = val;
                        }
                    }
                    // Fallback pre pôvodnú cenu
                    if (!originalPrice) {
                        const strikeEl = card.querySelector('s,del,strike');
                        if (strikeEl) {
                            const txt = strikeEl.innerText.replace(/[^\\d,.]/g, '');
                            originalPrice = parseFloat(txt.replace(',','.')) || null;
                        }
                    }

                    const discPct = (currentPrice && originalPrice && originalPrice > currentPrice)
                        ? Math.round((1 - currentPrice/originalPrice)*1000)/10
                        : 0;

                    // Množstvo
                    const qtyEl = card.querySelector('[class*="quantity"],[class*="qty"],[class*="pcs"],[class*="min"]');
                    let minQty = null;
                    if (qtyEl) {
                        const nums = qtyEl.innerText.match(/\\d+/);
                        if (nums) minQty = parseInt(nums[0]);
                    }

                    // Kategória
                    const catEl = card.querySelector('[class*="category"],[class*="cat"],[class*="tag"]');
                    const category = catEl ? catEl.innerText.trim() : '';

                    // Obrázok
                    const imgEl = card.querySelector('img');
                    const image = imgEl ? (imgEl.dataset.src || imgEl.dataset.lazy || imgEl.src || '') : '';

                    // Hodnotenie
                    const ratEl = card.querySelector('[class*="rating"],[class*="star"]');
                    let rating = 0;
                    if (ratEl) {
                        const rn = ratEl.innerText.match(/[\\d.]+/);
                        if (rn) { rating = parseFloat(rn[0]); if (rating > 10) rating /= 10; }
                    }

                    results.push({title, link, category, image,
                        current_price: currentPrice,
                        original_price: originalPrice,
                        discount_pct: discPct,
                        min_quantity: minQty,
                        seller_rating: rating});
                }

                return {offers: results, selector: usedSel, total_cards: cards.length};
            }
            """)

            sel = offers_data.get("selector", "none")
            total_cards = offers_data.get("total_cards", 0)
            raw_offers = offers_data.get("offers", [])
            print(f"  Selektor: '{sel}' | kariet: {total_cards} | ponuk: {len(raw_offers)}")

            # Ak nenašli sme nič, vypíš debug HTML
            if total_cards == 0:
                html_snippet = await page.evaluate(
                    "() => document.body.innerHTML.substring(0, 2000)"
                )
                print(f"  DEBUG HTML (prvých 2000 znakov):\n{html_snippet}\n")

            for o in raw_offers:
                o["id"] = make_id(o["title"], o["link"])
                o["unit_price"] = round(o["current_price"]/o["min_quantity"], 4) \
                    if o["current_price"] and o["min_quantity"] else None
                if o["id"] not in seen:
                    seen.add(o["id"])
                    all_offers.append(o)

            print(f"  Celkom: {len(all_offers)}")

            # Skontroluj ďalšiu stránku
            has_next = await page.query_selector(
                "a[rel='next'], .pagination .next, [class*='next']:not([class*='prev']), .pager-next"
            )
            if not has_next or not raw_offers:
                print("  Koniec stránkovania.")
                break

        await browser.close()

    return all_offers

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
    import time
    t0 = time.time()
    print(f"\n{'='*52}")
    print(f"  Merkandi Scraper (Playwright)")
    print(f"  {datetime.now().strftime('%d.%m.%Y %H:%M UTC')}")
    print(f"{'='*52}\n")

    raw = asyncio.run(scrape())
    print(f"\n  Celkom nájdených: {len(raw)}")

    if not raw:
        result = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "total": 0, "offers": [],
                  "error": "Scraper nenašiel ponuky"}
        OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return

    scored = sorted([score_offer(o) for o in raw], key=lambda x: -x["score"])
    result = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "total": len(scored), "offers": scored[:200]}
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 data.json: {len(scored)} ponúk")

    if USE_SHEETS:
        try: write_sheets(scored)
        except Exception as e: print(f"  Sheets chyba: {e}")

    print(f"\n🏆 TOP 5:")
    for i, o in enumerate(scored[:5], 1):
        print(f"  {i}. [{o['score']:.0f}] {o['title'][:55]}")
    print(f"\n✅ Hotovo za {time.time()-t0:.1f}s\n")

if __name__ == "__main__":
    main()
