#!/usr/bin/env python3
"""
Merkandi.sk Scraper — lokálny PC, Playwright
URL: merkandi.sk/products
"""

import json, re, os, hashlib, asyncio, subprocess, shutil
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

OUTPUT_JSON = Path("data.json")
BASE_URL    = "https://merkandi.sk"
OFFERS_URL  = "https://merkandi.sk/products"
MAX_PAGES   = 8
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

EXTRACT_JS = """() => {
    const selectors = [
        '.offer-box','.offer_box','[class*="offer-box"]',
        '.offer-item','.offer__item',
        '.product-item','.product_item','[class*="product-item"]',
        'article','.grid-item','.list-item',
        '[data-offer-id]','[data-id]','li[class]'
    ];
    let cards = [], usedSel = null;
    for (const sel of selectors) {
        const found = [...document.querySelectorAll(sel)]
            .filter(el => el.querySelector('a[href]'));
        if (found.length >= 2) { cards = found; usedSel = sel; break; }
    }

    const results = [];
    for (const card of cards) {
        const titleEl = card.querySelector('h1,h2,h3,h4,[class*="title"],[class*="name"],strong');
        let title = titleEl ? titleEl.innerText.trim() : '';
        if (!title) { const img = card.querySelector('img[alt]'); title = img ? img.alt.trim() : ''; }
        if (title.length < 5) continue;

        const linkEl = card.querySelector('a[href]');
        if (!linkEl) continue;

        let currentPrice = null, originalPrice = null;
        for (const el of card.querySelectorAll('[class*="price"],[class*="Price"],[class*="cost"]')) {
            const cls = el.className || '';
            const raw = el.innerText.replace(/[^\\d,.]/g,'').replace(',','.');
            const val = parseFloat(raw);
            if (!val || val<=0) continue;
            if (/old|original|before|was|prev|strike/i.test(cls)) originalPrice = val;
            else if (!currentPrice) currentPrice = val;
        }
        if (!originalPrice) {
            const s = card.querySelector('s,del,strike');
            if (s) { const v=parseFloat(s.innerText.replace(/[^\\d,.]/g,'').replace(',','.')); if(v>0) originalPrice=v; }
        }
        const discPct = (currentPrice&&originalPrice&&originalPrice>currentPrice)
            ? Math.round((1-currentPrice/originalPrice)*1000)/10 : 0;

        const qtyEl = card.querySelector('[class*="quantity"],[class*="qty"],[class*="pcs"],[class*="min"],[class*="amount"]');
        const qtyNums = qtyEl ? qtyEl.innerText.match(/\\d+/) : null;
        const minQty = qtyNums ? parseInt(qtyNums[0]) : null;

        const catEl = card.querySelector('[class*="category"],[class*="cat"],[class*="tag"],[class*="label"]');
        const imgEl = card.querySelector('img');
        const ratEl = card.querySelector('[class*="rating"],[class*="star"],[class*="score"]');
        let rating = 0;
        if (ratEl) { const rn=ratEl.innerText.match(/[\\d.]+/); if(rn){rating=parseFloat(rn[0]);if(rating>10)rating/=10;} }

        results.push({
            title, link: linkEl.href,
            category: catEl ? catEl.innerText.trim() : '',
            image: imgEl ? (imgEl.dataset.src||imgEl.dataset.lazy||imgEl.src||'') : '',
            current_price: currentPrice, original_price: originalPrice,
            discount_pct: discPct, min_quantity: minQty, seller_rating: rating
        });
    }
    return {offers: results, selector: usedSel, cards: cards.length,
            pageTitle: document.title, url: location.href};
}"""

async def scrape():
    all_offers = []
    seen = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="sk-SK",
            timezone_id="Europe/Bratislava",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        # Načítaj uložené cookies ak existujú
        cookies_file = Path("cookies.json")
        if cookies_file.exists():
            cookies = json.loads(cookies_file.read_text(encoding="utf-8"))
            await context.add_cookies(cookies)
            print("  Cookies načítané zo súboru.")
        else:
            print("  Cookies nenájdené — prihlás sa manuálne.")

        # Warm-up
        print(f"  Otvaram {BASE_URL} ...")
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        print(f"  Title: {await page.title()}")

        # Skontroluj či sme prihlásení
        is_logged_in = await page.query_selector("[class*='logout'],[class*='account'],[class*='profile'],[href*='logout'],[href*='profil']")
        if not is_logged_in and not cookies_file.exists():
            print("\n  *** PRIHLÁS SA DO MERKANDI V OTVORENOM OKNE ***")
            print("  Po prihlásení stlač Enter tu v cmd...")
            input()
            # Ulož cookies
            cookies = await context.cookies()
            cookies_file.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  Cookies uložené ({len(cookies)} ks) — nabudúce sa prihlásenie preskočí.")

        # Ponuky
        print(f"\n  Prechadzam na: {OFFERS_URL}")
        await page.goto(OFFERS_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        print(f"  Title: {await page.title()}")
        print(f"  URL:   {page.url}")

        for page_num in range(1, MAX_PAGES + 1):
            if page_num > 1:
                url = f"{OFFERS_URL}?page={page_num}"
                print(f"\n  --- Strana {page_num} ---")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)

            # Scroll
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
            await page.wait_for_timeout(800)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

            data = await page.evaluate(EXTRACT_JS)
            sel     = data.get("selector", "None")
            n_cards = data.get("cards", 0)
            raw     = data.get("offers", [])
            print(f"  Selektor: '{sel}' | kariet: {n_cards} | ponuk: {len(raw)}")

            # Debug ak nič nenašli
            if n_cards == 0:
                all_cls = await page.evaluate("""() => {
                    const cls = new Set();
                    document.querySelectorAll('[class]').forEach(el =>
                        el.className.split(' ').forEach(c => { if(c) cls.add(c); })
                    );
                    return [...cls].filter(c =>
                        /offer|product|item|card|list|grid|result/i.test(c)
                    ).slice(0, 30);
                }""")
                print(f"  Relevantné CSS triedy na stránke: {all_cls}")

            for o in raw:
                o["id"] = make_id(o["title"], o["link"])
                o["unit_price"] = round(o["current_price"]/o["min_quantity"], 4) \
                    if o["current_price"] and o["min_quantity"] else None
                if o["id"] not in seen:
                    seen.add(o["id"]); all_offers.append(o)

            print(f"  Celkom: {len(all_offers)}")

            has_next = await page.query_selector(
                "a[rel='next'],.pagination .next,[class*='next']:not([class*='prev']),.pager-next,[aria-label='Next']"
            )
            if not has_next or not raw:
                print("  Koniec stránkovania."); break

            await page.wait_for_timeout(1500)

        await browser.close()
    return all_offers

def git_push():
    git_cmd = shutil.which("git")
    if not git_cmd:
        for path in [
            os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\Git\cmd\git.exe"),
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files\Git\bin\git.exe",
        ]:
            if os.path.exists(path):
                git_cmd = path; break
    if not git_cmd:
        print("  ⚠️  Git nenájdený"); return

    try:
        subprocess.run([git_cmd, "add", "data.json"], check=True)
        changed = subprocess.run(
            [git_cmd, "diff", "--cached", "--quiet"], capture_output=True
        )
        if changed.returncode != 0:
            now = datetime.now().strftime("%d.%m.%Y %H:%M")
            subprocess.run([git_cmd, "commit", "-m", f"data: {now}"], check=True)
            subprocess.run([git_cmd, "push"], check=True)
            print("  ✅ Pushnuté do GitHub")
        else:
            print("  ℹ️  Žiadne zmeny")
    except Exception as e:
        print(f"  ⚠️  Git chyba: {e}")

def main():
    import time
    t0 = time.time()
    print(f"\n{'='*52}")
    print(f"  Merkandi Scraper — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    print(f"  URL: {OFFERS_URL}")
    print(f"{'='*52}\n")

    raw = asyncio.run(scrape())
    print(f"\n  Nájdených: {len(raw)}")

    if not raw:
        print("⚠️  Žiadne ponuky")
        result = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "total": 0, "offers": [], "error": "Žiadne ponuky"}
    else:
        scored = sorted([score_offer(o) for o in raw], key=lambda x: -x["score"])
        result = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "total": len(scored), "offers": scored[:200]}
        print(f"\n🏆 TOP 5:")
        for i, o in enumerate(scored[:5], 1):
            print(f"  {i}. [{o['score']:.0f}] {o['title'][:55]}")

    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 data.json uložený")

    print("\n📤 Pushujem do GitHub...")
    git_push()

    print(f"\n✅ Hotovo za {time.time()-t0:.1f}s")
    print(f"   Dashboard: https://panstarozitnik.github.io/merkandi-watch/\n")

if __name__ == "__main__":
    main()
