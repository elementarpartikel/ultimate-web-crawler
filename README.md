# 🕸️ Webbdammsugare Pro / Web Crawler Pro v6.0

[![Ladda ner .exe för Windows](https://img.shields.io/badge/Ladda_ner-.exe-blue?style=for-the-badge&logo=windows)](https://github.com/elementarpartikel/ultimate-web-crawler/releases/latest)

![Skärmdump av GUI](screenshots/gui_preview.png)

**Webbdammsugare Pro** är ett professionellt verktyg för att skrapa, strukturera och lagra innehåll från webbplatser – särskilt framtaget för att generera högkvalitativ textdata för AI-modeller, RAG-pipelines och vektordatabaser.

---

## 🆕 Nyheter i v6.0

- **Strukturerade chunks:** Varje chunk är nu ett objekt med `heading`, `content`, `chunk_index` och `total_chunks` – redo för RAG-pipelines som behöver rubrikkontext och positionsdata.
- **Absoluta länkar i chunks:** Alla relativa länkar görs absoluta vid extraktion. Gäller både Trafilatura-vägen och fallback-parsern, så att en AI-assistent kan hänvisa vidare med fungerande URL:er.
- **Mallar via sites.json:** Lägg en `sites.json` i samma mapp som programmet. En dropdown visas i GUI:t där du väljer mall – alla fält fylls i automatiskt. Samma fil fungerar i serverläge (`--config`).
- **Sessionsdetektering:** Crawlern upptäcker automatiskt om en intranäts-session har löpt ut (inloggningssida returneras) och loggar det som varning istället för att spara skräpdata.
- **Kraschsäker TaskGroup:** `bounded_process` fångar nu `CancelledError` och oväntade undantag separat, så att ett enstaka sidfel inte dödar hela crawlen.
- **Korrekt cookie-överföring:** Playwright-cookies överförs nu korrekt till aiohttp-sessionen via Cookie-header, inklusive domäninformation.
- **Playwright körs dold efter inloggning:** Fixat bugg där Playwright startade synlig i `login_then_headless`-läget.
- **Ljust tema som standard:** GUI:t startar nu i ljust läge. Mörkt läge aktiveras via switchen.
- **Tydligare URL-filter:** Etiketterna förtydligar att filtren gäller ord i URL:en (inte sidans text), och att "Kräv"-fältet använder ELLER-logik. Placeholder-texter ger visuella exempel.

---

## 🚀 Huvudfunktioner

- **Ren async-arkitektur:** Byggd på `asyncio` och `aiohttp` med upp till 10 parallella HTTP-anslutningar och separata Playwright-semaforer (max 2) för stabilitet.
- **Playwright-rendering:** Faller automatiskt tillbaka på Playwright (`networkidle`-väntan) för JS-tunga sidor. En enda webbläsarinstans återanvänds under hela körningen.
- **Hybridmotor:** Hämtar sidor snabbt med aiohttp och använder Playwright bara när sidan kräver det.
- **Per-domän rate limiting:** Respekterar `Crawl-Delay` från `robots.txt` och håller en konfigurerbar fördröjning per domän.
- **Exponentiell backoff:** Återförsöker automatiskt vid 429/5xx med ökande väntetid.
- **Gzip-sitemaps:** Parsar komprimerade `.xml.gz`-sitemaps utan extra konfiguration.
- **Rekursiva sitemap-index:** Undersitemaps hämtas parallellt med loop-skydd.
- **Prioriterad URL-kö:** URL:er från sitemap ges högre prioritet. Kön boostar URL:er med ord som "policy", "guide" och nedprioriterar arkiv och nyheter.
- **URL-filter:** Uteslut eller kräv nyckelord i URL:er (ELLER-logik för kräv).
- **Strikt Domän:** Håller crawlern på exakt angiven domän.
- **Incremental crawling:** SHA-256-hash förhindrar omskrivning av oförändrade sidor.
- **GDPR PII-tvätt:** E-post, telefonnummer, personnummer och IP-adresser maskeras automatiskt innan sparning.
- **Overlapping Semantic Chunking:** Texten delas i strukturerade block om ~400 ord med 50 ords överlappning, uppdelat vid rubrikgränser.
- **HEAD-request för filidentifiering:** Kontrollerar Content-Type innan nedladdning – bilder, videor och fonter avvisas direkt.
- **Automatisk index.csv:** Genereras vid körningens slut med URL, titel, datum och filnamn.
- **Manuell inloggning:** Playwright öppnar synlig webbläsare, väntar på OK-klick och överför sedan cookies till aiohttp-sessionen.
- **Pausa & Återuppta:** Omedelbar respons på knapptryck.
- **Tvåspråkigt gränssnitt (SV/EN):** Byt språk i realtid.
- **Ljust/Mörkt tema:** Switch (☀️ / 🌙) utan omstart.
- **Serverläge (CLI):** Kör headless med JSON-konfigurationsfil och valfri webhook-notis.

---

## ✅ Krav

| Krav | Detalj |
|---|---|
| **Python** | **3.11+** |
| **Chromium** | Installeras via `playwright install chromium` (se nedan) |

> **Obs!** Playwright laddar ned och hanterar sin egen Chromium-instans – du behöver inte installera Google Chrome manuellt.

---

## 🛠️ Installation

**1. Klona repositoryt:**
```bash
git clone https://github.com/elementarpartikel/ultimate-web-crawler.git
cd ultimate-web-crawler
```

**2. Installera beroenden:**
```bash
pip install -r requirements.txt
```

| Paket | Funktion |
|---|---|
| `aiohttp` + `aiosqlite` | Asynkron HTTP-hämtning och databas |
| `beautifulsoup4` + `lxml` | HTML- och XML-parsning |
| `playwright` | JS-rendering med networkidle-väntan |
| `trafilatura` | AI-optimerad textextraktion |
| `customtkinter` | Modernt GUI med ljust/mörkt tema |

**3. Installera Playwrights webbläsare** ⚠️ Obligatoriskt steg:
```bash
playwright install chromium
```

> Laddar ned Playwrights Chromium (~150 MB). Görs bara en gång.

**4. Installera valfria beroenden:**
```bash
pip install uvloop psutil
```

| Paket | Funktion |
|---|---|
| `uvloop` | Snabbare event loop (Linux/macOS) |
| `psutil` | Realtidsövervakning av minnesanvändning |

---

## 🖥️ Användning / Usage

```bash
python ultimate-web-crawler.py
```

### Mallar / Templates

Lägg en `sites.json` i samma mapp som `ultimate-web-crawler.py`. En **📋 Mall**-dropdown visas automatiskt i GUI:t. Välj en mall, välj mapp och klicka Starta – alla inställningar fylls i från filen.

Samma `sites.json` fungerar i serverläge med `--config`. Se "Serverläge" nedan.

### GUI-inställningar

**Grundinställningar / Basic Settings:**

| Inställning | Beskrivning |
|---|---|
| **Startadress / Start URL** | Komplett URL inklusive `https://` |
| **Fördröjning / Delay** | Sekunder mellan förfrågningar (standard: 0.5 s) |
| **Max sidor / Max pages** | `0` = crawla hela sajten |
| **Max djup / Max depth** | Länknivåer från startsidan (`0` = obegränsat) |
| **Filformat / File Format** | Se tabellen "Utdataformat" nedan |
| **Ladda ner dokument** | Sparar PDF, DOCX m.m. i undermappen `dokument/` |
| **Körläge / Run Mode** | Se tabellen "Körlägen" nedan |
| **Mapp / Folder** | Katalog för alla sparade filer |

**Utdataformat / File Formats:**

| Format | Beskrivning |
|---|---|
| **.json** | Strukturerad data med rubriker, chunks och metadata – rekommenderas för vektordatabaser. |
| **.md** | Markdown med metadata-huvud – bra för generell LLM-läsning. |
| **.txt** | Ren text. |
| **Ingen text / No text** | Crawlar och laddar enbart ned dokument, sparar ingen sidtext. |

**Körlägen / Run Modes:**

| Svenska | English | Beskrivning |
|---|---|---|
| **Snabb (dold)** | Fast (hidden) | Kör i bakgrunden utan synligt fönster. Snabbast. |
| **Logga in, sen dold** | Login, then hidden | Öppnar synlig webbläsare för manuell inloggning, kör sedan i bakgrunden. |
| **Synlig (felsökning)** | Visible (debugging) | Visar webbläsarfönstret. Bra för att förstå vad som händer. |

**Avancerat / Advanced:**

| Inställning | Beskrivning |
|---|---|
| **Hybrid-motor** | Väljer automatiskt aiohttp eller Playwright per sida |
| **Trafilatura** | Aktiverar AI-optimerad textextraktion |
| **Sitemap.xml** | Förladdas rekursivt, inklusive gzip-komprimerade sitemaps |
| **robots.txt** | Respekterar crawling-regler och Crawl-Delay |
| **Strikt Domän** | Tvingar crawlern att stanna på exakt angiven domän |
| **Uteslut ord i URL** | Kommaseparerad lista – URL:er som matchar hoppas över |
| **Kräv ord i URL (något av)** | Crawlern besöker bara sidor vars URL innehåller minst ett av dessa ord (ELLER-logik) |

**PII-Tvätt / PII Wash (GDPR):**

| Inställning | Vad som maskeras |
|---|---|
| **Radera E-post** | kontakt@myndighet.se → `[E-POST]` |
| **Radera Telefonnummer** | Svenska format inkl. landskod och parenteser → `[TELEFON]` |
| **Radera Personnummer** | Vanliga PNR och samordningsnummer → `[PERSONNUMMER]` |
| **Radera IP-adresser** | IPv4-adresser → `[IP-ADRESS]` |

> 💡 **Tips:** Dubbelklicka på valfri rad i *Live Data*-tabellen för att öppna URL:en i din webbläsare.

---

## 💻 Serverläge / Server Mode

Kör crawlern headless med en JSON-konfigurationsfil – perfekt för schemalagd körning med `cron` eller Task Scheduler:

```bash
python ultimate-web-crawler.py --config sites.json
python ultimate-web-crawler.py --config sites.json --webhook "https://hooks.slack.com/..."
```

> Webhook-URL kan även anges via miljövariabeln `WEBHOOK_URL`. Max 3 sajter körs parallellt. Varje sajt crawlas i sin egen undermapp under `server_data/`.

### Exempel på `sites.json`

Nedan visas två vanliga konfigurationer. Det första exemplet skrapar data anpassad för RAG/AI där personuppgifter tvättas bort. Det andra exemplet är inställt på att ignorera text helt och istället enbart ladda ner dokument.

```json
[
  {
    "name": "Skolverket (RAG & AI-text)",
    "start_url": "https://www.skolverket.se",
    "delay": 0.5,
    "max_pages": 500,
    "max_depth": 3,
    "save_format": ".json",
    "headless_mode": "headless",
    "find_sitemap": true,
    "respect_robots": true,
    "use_hybrid": true,
    "use_trafilatura": true,
    "download_docs": false,
    "strict_domain": true,
    "exclude_keywords": ["images", "login", "kalender"],
    "require_keywords": [],
    "remove_email": true,
    "remove_phone": true,
    "remove_pnr": true,
    "remove_ip": false
  },
  {
    "name": "SKR (Endast Dokument)",
    "start_url": "https://skr.se",
    "delay": 0.5,
    "max_pages": 500,
    "max_depth": 1,
    "save_format": "Ingen text",
    "headless_mode": "headless",
    "find_sitemap": true,
    "respect_robots": true,
    "use_hybrid": true,
    "use_trafilatura": false,
    "download_docs": true,
    "strict_domain": true,
    "exclude_keywords": [],
    "require_keywords": [],
    "remove_email": false,
    "remove_phone": false,
    "remove_pnr": false,
    "remove_ip": false
  }
]
```

---

## 📂 Output-struktur

```text
crawl_output/
├── texter/                          # En fil per skrapad sida
│   └── sidnamn_a1b2c3.json          # eller .md / .txt
├── dokument/                        # Nedladdade PDF, DOCX, XLSX m.m.
├── logs/
│   └── crawl_YYYYMMDD_HHMMSS.log
├── index.csv                        # Översikt: URL, titel, datum, filnamn
└── domännamn_cache.db               # SQLite-cache för incremental crawling
```

### JSON-format per sida (v6.0):

```json
{
  "title": "Kontakta oss - Skolverket",
  "url": "https://www.skolverket.se/kontakt",
  "crawled_at": "2026-04-03T12:00:00",
  "author": "",
  "published_date": "",
  "modified_date": "2026-03-15",
  "language": "sv",
  "og_type": "",
  "description": "Så når du Skolverket",
  "keywords": ["kontakt"],
  "plain_text": "## Ring oss\nNi når oss på [TELEFON]...",
  "chunks": [
    {
      "heading": "Ring oss",
      "content": "Ni når oss på [TELEFON]. Vår e-post är [E-POST]. Besök oss på [Fleminggatan 14](https://www.skolverket.se/om-oss/besok).",
      "chunk_index": 1,
      "total_chunks": 3
    },
    {
      "heading": "Öppettider",
      "content": "Telefontid vardagar 09:00–11:30 och 13:00–15:00.",
      "chunk_index": 2,
      "total_chunks": 3
    },
    {
      "heading": "Presskontor",
      "content": "Presskontakt nås via [E-POST] eller [TELEFON].",
      "chunk_index": 3,
      "total_chunks": 3
    }
  ]
}
```

---

## ⚖️ Etik och Ansvar

Detta verktyg är utvecklat för laglig och etisk datainsamling. Användaren ansvarar för att:

- Följa webbplatsens användarvillkor.
- Inte överbelasta servrar – använd den inbyggda fördröjningsfunktionen.
- Respektera de begränsningar som anges i `robots.txt`.
- Säkerställa att insamlad data hanteras i enlighet med GDPR och tillämplig lagstiftning.
