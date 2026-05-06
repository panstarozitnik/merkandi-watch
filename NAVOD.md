# Merkandi Watch — Návod na nastavenie

## Čo dostaneš
- **scraper.py** — každý deň stiahne ponuky z Merkandi a zapíše ich do Google Sheets
- **dashboard.html** — prehľad ponúk s filtrovaním, triedením a skórovaním
- **GitHub Actions** — automatické spúšťanie každý deň o 7:00 (bez servera, zadarmo)

---

## KROK 1 — Google Cloud: Service Account

1. Choď na https://console.cloud.google.com
2. Vytvor nový projekt (napr. „merkandi-watch")
3. Ľavé menu → **APIs & Services → Library**
   - Aktivuj: **Google Sheets API**
   - Aktivuj: **Google Drive API**
4. Ľavé menu → **APIs & Services → Credentials**
5. Klikni **+ Create Credentials → Service Account**
   - Meno: `merkandi-scraper`
   - Klikni **Create and Continue → Done**
6. Klikni na vytvorený service account
7. Záložka **Keys → Add Key → Create new key → JSON**
8. Stiahne sa súbor `credentials.json` — **UCHOVAJ HO V BEZPEČÍ!**

---

## KROK 2 — Google Sheets: Vytvor tabuľku

1. Choď na https://sheets.google.com
2. Vytvor nový spreadsheet — pomenuj ho napr. „Merkandi Ponuky"
3. Skopíruj **Spreadsheet ID** z URL:
   ```
   https://docs.google.com/spreadsheets/d/  →[TOTO JE ID]←  /edit
   ```
4. Zdieľaj tabuľku so service accountom:
   - Klikni **Share**
   - Vlož email service accountu (nájdeš ho v credentials.json ako `client_email`)
   - Nastav rolu **Editor**
   - Klikni **Send**
5. Pre dashboard nastav aj verejné zobrazenie:
   - **Share → Change to anyone with the link → Viewer**

---

## KROK 3 — GitHub: Vytvor repozitár

1. Choď na https://github.com/new
2. Vytvor **private** repozitár (napr. `merkandi-watch`)
3. Nahraj tieto súbory:
   ```
   merkandi-watch/
   ├── scraper.py
   ├── requirements.txt
   └── .github/
       └── workflows/
           └── scraper.yml
   ```
4. Najjednoduchšie cez GitHub Desktop alebo:
   ```bash
   git init
   git add .
   git commit -m "init"
   git remote add origin https://github.com/TVOJE_MENO/merkandi-watch.git
   git push -u origin main
   ```

---

## KROK 4 — GitHub Secrets: Pridaj credentials

1. V repozitári choď na **Settings → Secrets and variables → Actions**
2. Klikni **New repository secret** — pridaj 2 secrets:

   **Secret 1:**
   - Name: `SPREADSHEET_ID`
   - Value: ID tvojej Google Sheets tabuľky (z Kroku 2)

   **Secret 2:**
   - Name: `GOOGLE_CREDENTIALS`
   - Value: celý obsah súboru `credentials.json` (otvor ho v textovom editore, skopíruj všetko)

---

## KROK 5 — Prvé spustenie

1. V repozitári choď na **Actions**
2. Klikni na workflow „Merkandi Daily Scraper"
3. Klikni **Run workflow → Run workflow**
4. Sleduj výstup — po ~2 minútach by mala tabuľka mať dáta

---

## KROK 6 — Dashboard

1. Otvor `dashboard.html` v prehliadači (stačí dvojklik)
2. Vlož Spreadsheet ID do poľa hore
3. Klikni **Uložiť & načítať**
4. Hotovo! 🎉

Dashboard si uloží ID do localStorage — pri ďalšom otvorení načíta automaticky.

---

## Automatické spúšťanie

Po nastavení repozitára bude GitHub Actions automaticky spúšťať scraper:
- **Každý deň o 7:00 UTC** (= 9:00 SK letný čas / 8:00 zimný)
- Zadarmo (GitHub Free = 2000 min/mesiac, scraper trvá ~2-3 min)

---

## Úprava selektorov (ak scraper nenájde ponuky)

Merkandi môže zmeniť HTML štruktúru. Ak scraper vráti 0 ponúk:

1. Otvor merkandi.sk/offers v prehliadači
2. Klikni pravým tlačidlom na kartu ponuky → **Inspect**
3. Nájdi CSS class karty (napr. `.offer-item`, `.product-card`)
4. Uprav v `scraper.py` sekciu `scrape_page()` → `cards = soup.select(".NOVA_CLASS")`
5. Commitni zmenu — GitHub Actions použije nový kód

---

## Štruktúra Google Sheets

Tab **"ponuky"** obsahuje stĺpce:
| id | datum | nazov | kategoria | cena_eur | orig_cena_eur | zlava_pct | cena_za_kus | min_mnozstvo | hodnotenie_predajcu | skore | link | obrazok | aktivna |

Tab **"log"** obsahuje históriu spustení:
| datum | celkom_scraped | novych | aktualizovanych | chyby | trvanie_s |
