# 🕸️ Webbdammsugare Pro / Web Crawler Pro v2.0 (AI & RAG Edition)

[![Ladda ner .exe för Windows](https://img.shields.io/badge/Ladda_ner-.exe-blue?style=for-the-badge&logo=windows)](https://github.com/elementarpartikel/ultimate-web-crawler/releases/latest)

![Skärmdump av GUI](screenshots/gui_preview.png)

**Webbdammsugare Pro** är ett professionellt verktyg för att skrapa, strukturera och lagra innehåll från webbplatser – särskilt framtaget för att generera högkvalitativ textdata för AI-modeller, RAG-pipelines och vektordatabaser.

From v1.7 the interface is fully bilingual. Switch between 🇸🇪 Swedish and 🇬🇧 English directly in the app.

---

## 🆕 Nyheter i v2.0

- **Asynkron motor (Experimentell):** En ny motor byggd på `asyncio` och `aiohttp` som aktiveras direkt i GUI:t. Hanterar upp till 50 parallella anslutningar via en inbyggd semaphore, vilket ger dramatiskt högre hastighet på stora sajter. Stödjer manuell inloggning (`login_then_headless`), robots.txt och rekursiva sitemaps. Kräver Python 3.11+ och valfria beroenden `aiohttp` och `aiosqlite`.
- **`lxml` som HTML-parser:** Hela programmet använder nu `lxml` för HTML-parsning, inte bara sitemaps. Snabbare och mer tolerant mot trasig HTML.
- **Dokumenträknare i statusraden:** Statistiken visar nu nedladdade dokument separat, vilket ger bättre överblick vid körningar med dokumentnedladdning aktiverat.
- **Förloppsindikator:** En `CTkProgressBar` visas under körning. När max sidor är angett fylls den proportionellt – vid oändligt läge kör den en löpande animation.
- **Dubbelklick öppnar URL:** Dubbelklicka på valfri rad i Live Data-tabellen för att öppna sidan direkt i din webbläsare. Praktiskt vid felsökning av sidor med status "Fel".
- **Anti-stutter:** Loggfönstret trimmas automatiskt vid 500 rader och GUI-kön bearbetar upp till 20 meddelanden per tick.
- **Användarvänliga körlägen:** De tekniska termerna är ersatta med tydliga beskrivningar på båda språken: *Snabb (dold)*, *Logga in, sen dold* och *Synlig (felsökning)*.
- **Tvåspråkigt gränssnitt (SV/EN):** Byt språk i realtid via flaggknappen – hela gränssnittet, inklusive körlägesmenyn, uppdateras omedelbart.
- **Mörkt/Ljust tema:** Switch (🌙 / ☀️) för att växla tema utan omstart. Treeview och scrollbar anpassar sig automatiskt.

---

## 🚀 Huvudfunktioner

- **Dubbla motorer:** Välj mellan den klassiska trådade motorn (`requests` + `Selenium`) eller den asynkrona motorn (`aiohttp` + `asyncio`).
- **Hybridmotor:** Växlar automatiskt mellan snabb HTTP-hämtning och Selenium-rendering för JavaScript-tunga sidor.
- **Content-Type Routing:** Kontrollerar serverns headers *innan* en fil laddas ned. Mediafiler ignoreras direkt och dokument skickas till dokumenthanteraren.
- **Selenium PDF-skydd:** Konfigurerar Chrome att aldrig öppna PDF:er i webbläsarfliken.
- **Smarta filnamn:** Läser `content-disposition`-headern för att hitta det verkliga filnamnet. Unik hash garanterar att inga filer skrivs över.
- **AI/RAG-Optimerad:** Extraherar ren text rensad från menyer, footers och skräpkod med `Trafilatura`, redo för vektordatabaser.
- **Intelligent Caching:** SQLite (WAL-läge) med SHA-256-hash för att hoppa över oförändrade sidor. Async-motorn använder `aiosqlite`.
- **Sitemap-index stöd:** Parsar sitemap-index-filer rekursivt med inbyggt loop-skydd. Async-motorn hämtar undersitemaps parallellt.
- **Prioriterad URL-kö:** URL:er från `sitemap.xml` bearbetas med högre prioritet.
- **Canonical-hantering:** Lägger till kanoniska URL:er i kön med hög prioritet och hoppar över originalsidan för att undvika dubbletter.
- **URL-filter:** Uteslut eller kräv nyckelord i URL:er – användbart för att låsa crawlern till en specifik del av en delad plattform.
- **Automatisk index-fil:** Genererar `index.csv` vid körningens slut.
- **Strikt Domän:** Tvingar crawlern att stanna på exakt angiven domän. Kan stängas av för underdomäner.
- **Manuell inloggning:** Öppnar synlig webbläsare för inloggning, tar sedan över med sparade cookies. Fungerar i båda motorlägena.
- **Pausa & Återuppta:** Fungerar i båda motorlägena.
- **Utökad Dokumenthantering:** Laddar ned PDF, DOCX, XLSX, PPTX, CSV, ODT m.fl. Pågående nedladdningar slutförs alltid säkert.
- **Valbart utdataformat:** `.md` (rekommenderas för AI/LLM) eller `.txt`.
- **Crawldjup:** Max länknivåer från startsidan (0 = obegränsat).
- **Automatisk retry:** Exponentiell backoff vid nätverksfel (klassisk motor).
- **URL-deduplicering:** `index.html`/`index.php` normaliseras bort. Tracking-parametrar som `utm_source`, `fbclid` m.fl. rensas automatiskt.
- **Minnesövervakning:** Garbage collection vid >1 500 MB (kräver valfri `psutil`).
- **Roterande loggfiler:** Per session, max 5 MB × 2 backupfiler.
- **Trådsäker arkitektur:** `threading.Lock` skyddar databas och nedladdningar i klassisk motor. Async-motorn använder `asyncio.Lock` och `asyncio.Semaphore(50)`.

---

## ✅ Krav

| Krav | Detalj |
|---|---|
| **Python** | 3.8+ (klassisk motor) / **3.11+** (async-motor) |
| **Google Chrome** | Måste vara installerat på datorn |
| **ChromeDriver** | Hanteras automatiskt av `webdriver-manager` |

> **Obs!** `webdriver-manager` laddar ned rätt ChromeDriver automatiskt. **Google Chrome** måste finnas installerat för hybrid-motorn och headless-lägena.
>
> Ladda ned Chrome här om det saknas: [google.com/chrome](https://www.google.com/chrome)

---

## 🛠️ Installation

**1. Klona repositoryt:**
```bash
git clone https://github.com/elementarpartikel/ultimate-web-crawler.git
cd ultimate-web-crawler
```

**2. Installera obligatoriska beroenden:**
```bash
pip install -r requirements.txt
```

| Paket | Funktion |
|---|---|
| `requests` | HTTP-hämtning (klassisk motor) |
| `beautifulsoup4` | HTML-parsning |
| `lxml` | HTML- och XML-parsning |
| `selenium` + `webdriver-manager` | JS-rendering via Chrome |
| `trafilatura` | AI-optimerad textextraktion (rekommenderas starkt) |
| `customtkinter` | Modernt GUI med mörkt/ljust tema |

**3. Installera valfria beroenden** (aktiverar extrafunktioner):
```bash
pip install aiohttp aiosqlite uvloop psutil
```

| Paket | Funktion |
|---|---|
| `aiohttp` + `aiosqlite` | Aktiverar den asynkrona motorn (kräver Python 3.11+) |
| `uvloop` | Snabbare event loop för async-motorn (Linux/macOS) |
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
| **Filformat / File Format** | `.md` (rekommenderas för AI) eller `.txt` |
| **Ladda ner dokument** | Sparar PDF, DOCX m.m. i undermappen `dokument/` |
| **Körläge / Run Mode** | Se tabellen nedan |
| **Mapp / Folder** | Katalog för alla sparade filer |

**Körlägen / Run Modes:**

| Svenska | English | Beskrivning |
|---|---|---|
| Snabb (dold) | Fast (hidden) | Kör i bakgrunden utan synligt fönster. Snabbast. |
| Logga in, sen dold | Login, then hidden | Öppnar synlig webbläsare för manuell inloggning, kör sedan i bakgrunden. |
| Synlig (felsökning) | Visible (debugging) | Visar webbläsarfönstret. Bra för att förstå vad som händer. |

**Avancerat / Advanced:**

| Inställning | Beskrivning |
|---|---|
| **Async Mode** | Aktiverar den asynkrona motorn (experimentell, kräver Python 3.11+) |
| **Hybrid-motor** | Väljer automatiskt HTTP-hämtning eller Selenium per sida |
| **Trafilatura** | Aktiverar AI-optimerad textextraktion |
| **Sitemap.xml** | Förladdas rekursivt för effektivare crawling |
| **robots.txt** | Respekterar webbplatsens crawling-regler |
| **Strikt Domän** | Tvingar crawlern att stanna på exakt angiven domän |
| **Uteslut ord i URL** | Kommaseparerad lista – matchande sidor hoppas över |
| **Kräv ord i URL** | Crawlern besöker bara sidor vars URL innehåller minst ett av dessa ord |

> 💡 **Tips:** Dubbelklicka på valfri rad i Live Data-tabellen för att öppna URL:en i din webbläsare.

---

## 📂 Output-struktur

```
crawl_output/
├── texter/                          # En fil per skrapad sida (.md eller .txt)
│   └── sidnamn_a1b2c3.md
├── dokument/                        # Nedladdade PDF, DOCX, XLSX m.m.
├── logs/
│   └── crawl_YYYYMMDD_HHMMSS.log
├── index.csv                        # Översikt: URL, titel, datum, filnamn
└── domännamn_cache.db               # SQLite-cache för incremental crawling
```

Varje textfil inleds med ett metadata-huvud:
```
KÄLLA: https://exempel.se/sida
TITEL: Sidonamn
HÄMTAD: 2025-01-01 12:00:00
============================================================

[Ren sidtext här]
```

---

## 🏗️ Teknisk Stack

| Komponent | Teknik |
|---|---|
| GUI | CustomTkinter (mörkt/ljust tema, tvåspråkigt) |
| HTTP-hämtning | `requests` (klassisk), `aiohttp` (asynkron) |
| HTML-parsning | `lxml` + BeautifulSoup4, Trafilatura |
| JS-rendering | Selenium + ChromeDriverManager |
| Caching | SQLite3/WAL (klassisk), `aiosqlite` (asynkron) |
| Parallellism | `ThreadPoolExecutor` (klassisk), `asyncio` + `Semaphore(50)` (asynkron) |
| Loggning | RotatingFileHandler |
| Event loop | `asyncio` (standard), `uvloop` (valfri optimering) |

---

## ⚖️ Etik och Ansvar

Detta verktyg är utvecklat för laglig och etisk datainsamling. Användaren ansvarar för att:

- Följa webbplatsens användarvillkor.
- Inte överbelasta servrar – använd den inbyggda fördröjningsfunktionen.
- Respektera de begränsningar som anges i `robots.txt`.
- Säkerställa att insamlad data hanteras i enlighet med GDPR och tillämplig lagstiftning.
