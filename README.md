# 🕸️ Webbdammsugare Pro / Web Crawler Pro v5.0 (AI & GDPR Edition)

[![Ladda ner .exe för Windows](https://img.shields.io/badge/Ladda_ner-.exe-blue?style=for-the-badge&logo=windows)](https://github.com/elementarpartikel/ultimate-web-crawler/releases/latest)

![Skärmdump av GUI](screenshots/gui_preview.png)

**Webbdammsugare Pro** är ett professionellt verktyg för att skrapa, strukturera och lagra innehåll från webbplatser – särskilt framtaget för att generera högkvalitativ textdata för AI-modeller, RAG-pipelines och vektordatabaser.

---

## 🆕 Nyheter i v5.0

- **GDPR-PII-tvätt:** Personuppgifter rensas automatiskt ur all extraherad text innan sparning. Fyra valbara filter direkt i GUI:t: e-postadresser, telefonnummer, personnummer (inkl. samordningsnummer), och IP-adresser. Personnummer-regexet täcker både 10- och 12-sifferformat samt samordningsnummer (dag 61–91).
- **Overlapping Semantic Chunking:** Texten delas i AI-vänliga block om ~400 ord med 50 ords överlappning mellan varje chunk. Uppdelningen sker vid styckesgränser, inte mitt i meningar, vilket ger dramatiskt bättre retrieval-kvalitet i vektordatabaser.
- **HEAD-request för filidentifiering:** Programmet skickar ett lättviktigt HEAD-anrop innan varje sidnedladdning för att kontrollera Content-Type. Bilder, videor, fonter och andra binärfiler avvisas omedelbart utan att laddas ned – sparar bandbredd och tid. Omfattar även en graciös fallback för brandväggar (403/405).
- **"Ingen text"-läge:** Nytt utdataformat som gör att programmet enbart laddar ned dokument (PDF, DOCX m.fl.) utan att spara sidtext. Praktiskt för rena dokumentinsamlingskörningar.
- **Strukturerad JSON-export:** Utdataformat `.json` sparar varje sida som ett strukturerat objekt med `title`, `url`, `crawled_at`, `description`, `keywords`, `sections` (med rubrikhierarki), `chunks` (överlappande) – redo att läsas in direkt i en vektordatabas.
- **Sektionsextraktion & Trafilatura:** HTML-strukturen bevaras som en lista av `{"heading": "...", "text": "..."}` per avsnitt, baserat på h1–h4. Tabeller konverteras till pipe-separerat format och listor bevaras med bullet-punkter. Trafilatura används i bakgrunden för exceptionell brusreducering.
- **Förbättrad bildfiltrering:** URL:er som pekar mot `/images/`, `/media/` eller `/assets/` med bildändelser avvisas redan i URL-valideringen, innan ett enda nätverksanrop görs.
- **Grid-layout i GUI:t:** Grundinställningarna använder nu ett justerat grid-layout istället för pack, vilket ger bättre horisontell justering av etiketter och fält. Filsökvägen är nu också skrivskyddad för att undvika felskrivningar.
- **Race condition-fix i crawl-loopen:** `active_tasks`-räknaren läses nu inom ett asynkront lås, vilket eliminerar en teoretisk race condition vid bestämning av om körningen är klar.
- **Konsistenta filnamn (50 tecken):** Filnamns-prefixet är begränsat till 50 tecken i både `texter/`-mappen och `index.csv` för att undvika problem med för långa sökvägar i Windows.

---

## 🚀 Huvudfunktioner

- **Ren async-arkitektur:** Hela crawlern är byggd på `asyncio` och `aiohttp` med upp till 50 parallella HTTP-anslutningar. Separata skydds-semaforer (max 5) ser till att Playwright inte svämmar över RAM-minnet.
- **Playwright-rendering:** Faller automatiskt tillbaka på Playwright (`networkidle`-väntan) för JS-tunga sidor. En enda webbläsarinstans återanvänds under hela körningen.
- **Hybridmotor:** Hämtar sidor snabbt med aiohttp och använder Playwright bara när sidan kräver det.
- **Per-domän rate limiting:** Respekterar `Crawl-Delay` från `robots.txt` och håller en konfigurerbar fördröjning per domän.
- **Exponentiell backoff:** Återförsöker automatiskt vid 429/5xx med ökande väntetid.
- **Gzip-sitemaps:** Parsar komprimerade `.xml.gz`-sitemaps utan extra konfiguration och stöder binär nedladdning via fallback.
- **Sitemap-fallback:** Om robots.txt saknas eller misslyckas (t.ex. returnerar 401) provas `sitemap.xml` direkt utan att krascha flödet.
- **Rekursiva sitemap-index:** Undersitemaps hämtas parallellt med loop-skydd.
- **Prioriterad URL-kö:** URL:er från sitemap ges högre prioritet. Kön boostar URL:er med ord som "policy", "guide" och nedprioriterar arkiv och nyheter.
- **Canonical-hantering:** Kanoniska URL:er läggs i kön och originalsidan hoppas över.
- **URL-filter:** Uteslut eller kräv nyckelord i URL:er.
- **Strikt Domän:** Håller crawlern på exakt angiven domän.
- **Incremental crawling:** Whitespace-normaliserad SHA-256-hash förhindrar omskrivning av oförändrade sidor.
- **Automatisk index.csv:** Genereras vid körningens slut med URL, titel, datum och filnamn för alla cachade sidor. Bygger numera på hashen från databasen för perfekta matchningar.
- **Säkra dokumentnedladdningar:** PDF, DOCX, XLSX, PPTX m.fl. laddas ned med `await` för att förhindra "fire-and-forget"-korruption i slutet av körningen.
- **Manuell inloggning:** Playwright öppnar synlig webbläsare, väntar på OK-klick och överför sedan cookies till aiohttp-sessionen.
- **Pausa & Återuppta:** Omedelbar kortslutning på aktiv nätverkstrafik för blixtsnabb respons på knapptryck.
- **Tvåspråkigt gränssnitt (SV/EN):** Byt språk i realtid.
- **Mörkt/Ljust tema:** Switch (🌙 / ☀️) utan omstart.
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
| `customtkinter` | Modernt GUI med mörkt/ljust tema |

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
| **.json** | Strukturerad data med sektioner och chunks – rekommenderas för vektordatabaser. |
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
| **Uteslut ord i URL** | Kommaseparerad lista – matchande sidor hoppas över |
| **Kräv ord i URL** | Crawlern besöker bara sidor vars URL innehåller minst ett av dessa ord |

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

### Exempel på `sites.json` (två olika användningsområden)

Nedan visas två vanliga konfigurationer. Det första exemplet skrapar data anpassad för RAG/AI där personuppgifter tvättas bort. Det andra exemplet är inställt på att ignorera text helt och istället enbart leta efter och ladda ner dokument.

```json
[
  {
    "name": "Skolverket Betyg (RAG & AI-text)",
    "start_url": "https://www.skolverket.se",
    "require_keywords": ["betyg"],
    "exclude_keywords": [],
    "find_sitemap": true,
    "max_depth": 3,
    "delay": 1.0,
    "save_format": ".json", 
    "download_docs": true,
    "use_trafilatura": true,
    "remove_email": true,
    "remove_phone": true,
    "remove_pnr": true,
    "remove_ip": false
  },
  {
    "name": "Tyresö Kommun (Endast Dokument)",
    "start_url": "https://www.tyreso.se",
    "require_keywords": [],
    "exclude_keywords": ["kalender", "politik"],
    "find_sitemap": true,
    "max_depth": 1,
    "delay": 0.5,
    "save_format": "Ingen text",
    "download_docs": true,
    "use_trafilatura": false,
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

### JSON-format per sida (Exempel med PII-tvätt):

```json
{
  "title": "Sidonamn - Kontakta oss",
  "url": "https://exempel.se/kontakt",
  "crawled_at": "2026-01-01T12:00:00",
  "description": "Meta-beskrivning utan [PERSONNUMMER]",
  "keywords": ["nyckelord", "kontakt"],
  "sections": [
    {"heading": "Ring oss", "text": "Ni når oss på [TELEFON]. Vår e-post är [E-POST]."}
  ],
  "chunks": [
    "Överlappande textblock för RAG..."
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
