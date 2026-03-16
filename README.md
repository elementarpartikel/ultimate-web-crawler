# 🕸️ Webbdammsugare Pro / Web Crawler Pro v1.7 (AI & RAG Edition)

[![Ladda ner .exe för Windows](https://img.shields.io/badge/Ladda_ner-.exe-blue?style=for-the-badge&logo=windows)](https://github.com/elementarpartikel/ultimate-web-crawler/releases/latest)

![Skärmdump av GUI](screenshots/gui_preview.png)

**Webbdammsugare Pro** är ett professionellt verktyg för att skrapa, strukturera och lagra innehåll från webbplatser – särskilt framtaget för att generera högkvalitativ textdata för AI-modeller, RAG-pipelines och vektordatabaser.

From v1.7 the interface is fully bilingual. Switch between 🇸🇪 Swedish and 🇬🇧 English directly in the app.

---

## 🆕 Nyheter i v1.7

- **Tvåspråkigt gränssnitt (SV/EN):** Byt språk i realtid direkt i appens huvud-meny via en flaggknapp – hela gränssnittet, alla etiketter, knappar och statistiktext uppdateras omedelbart.
- **Mörkt/Ljust tema:** En dedikerad switch (🌙 / ☀️) i fönstret låter dig växla tema utan att starta om. Treeview-tabellen anpassar sig automatiskt till valt tema.
- **Temakonsistent scrollbar:** Treeview använder nu `CTkScrollbar` som matchar det valda temat i stället för systemets standardscrollbar.

---

## 🚀 Huvudfunktioner

- **Hybridmotor:** Växlar intelligent mellan blixtsnabb hämtning via `requests` och dynamisk rendering via `Selenium` vid behov – ger maximal kompatibilitet med moderna webbplatser.
- **Content-Type Routing:** Kontrollerar serverns headers *innan* en fil laddas ned. Mediafiler ignoreras direkt och dokument skickas automatiskt till dokumenthanteraren.
- **Selenium PDF-skydd:** Konfigurerar Chrome att aldrig öppna PDF:er i webbläsarfliken, vilket annars kan få crawlern att fastna.
- **Smarta filnamn:** Läser `content-disposition`-headern för att hitta det verkliga filnamnet vid nedladdning. Unik hash garanterar att inga filer skrivs över.
- **AI/RAG-Optimerad:** Extraherar ren text rensad från menyer, footers och skräpkod med `Trafilatura`, redo för vektordatabaser.
- **Intelligent Caching:** Använder SQLite (WAL-läge) för att spåra innehållsändringar via SHA-256-hash och hoppa över oförändrade sidor (Incremental Crawling).
- **Sitemap-index stöd:** Parsar sitemap-index-filer rekursivt med inbyggt loop-skydd – fångar alla URL:er även på stora sajter.
- **Prioriterad URL-kö:** URL:er från `sitemap.xml` bearbetas med högre prioritet för ett strukturerat crawlflöde.
- **Canonical-hantering:** Lägger till kanoniska URL:er i kön med hög prioritet och hoppar över originalsidan för att undvika dubbletter i RAG-data.
- **URL-filter:** Uteslut sidor vars URL innehåller valda nyckelord, eller kräv att ett visst ord finns med – användbart för att låsa crawlern till en specifik del av en delad plattform.
- **Automatisk index-fil:** Genererar `index.csv` vid körningens slut med URL, titel, datum och filnamn för alla besökta sidor.
- **Strikt Domän:** Tvingar crawlern att stanna på exakt angiven domän. Kan stängas av för att tillåta underdomäner.
- **Flera Körlägen:** Osynligt (`headless`), synligt (`visible`) eller manuell inloggning med event-baserad väntan (`login_then_headless`).
- **Pausa & Återuppta:** Körningen kan pausas och återupptas utan att data går förlorad.
- **Utökad Dokumenthantering:** Laddar ned PDF, DOCX, XLSX, PPTX, CSV, ODT m.fl. Pågående nedladdningar slutförs alltid säkert innan programmet stängs.
- **Valbart utdataformat:** `.md` (Markdown, rekommenderas för AI/LLM) eller `.txt`.
- **Crawldjup:** Max antal länknivåer från startsidan (0 = obegränsat).
- **Automatisk retry:** Återförsöker HTTP-anrop vid nätverksfel med exponentiell backoff.
- **URL-deduplicering:** `index.html`/`index.php` normaliseras bort. Tracking-parametrar som `utm_source`, `fbclid` m.fl. rensas automatiskt.
- **Minnesövervakning:** Utlöser garbage collection automatiskt vid >1 500 MB RAM-användning (kräver `psutil`).
- **Roterande loggfiler:** Per session, max 5 MB × 2 backupfiler.
- **Trådsäker arkitektur:** `threading.Lock` skyddar SQLite-databasen och dokumentnedladdningarna. `RateLimiter` sover utanför låset.

---

## ✅ Krav

| Krav | Detalj |
|---|---|
| **Python** | 3.8 eller senare |
| **Google Chrome** | Måste vara installerat på datorn |
| **ChromeDriver** | Hanteras automatiskt av `webdriver-manager` |

> **Obs!** `webdriver-manager` laddar ned rätt ChromeDriver automatiskt. **Google Chrome** måste finnas installerat för att hybrid-motorn och headless-lägena ska fungera.
>
> Ladda ned Chrome här om det saknas: [google.com/chrome](https://www.google.com/chrome)

---

## 🛠️ Installation

**1. Klona repositoryt:**
```bash
git clone https://github.com/elementarpartikel/ultimate-web-crawler.git
cd ultimate-web-crawler
```

**2. Installera beroenden:**
```bash
pip install requests beautifulsoup4 lxml selenium webdriver-manager trafilatura customtkinter
```

| Paket | Funktion |
|---|---|
| `requests` | HTTP-hämtning |
| `beautifulsoup4` | HTML-parsning |
| `lxml` | XML-parsning för sitemap-stöd |
| `selenium` + `webdriver-manager` | JS-rendering via Chrome |
| `trafilatura` | AI-optimerad textextraktion (rekommenderas starkt) |
| `customtkinter` | Modernt GUI med mörkt/ljust tema |

**3. Installera valfria beroenden:**
```bash
pip install psutil
```

| Paket | Funktion |
|---|---|
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

| Läge | Beskrivning |
|---|---|
| `headless` | Osynlig Chrome-instans (standard, snabbast) |
| `visible` | Synlig webbläsare – bra för felsökning |
| `login_then_headless` | Öppnar synlig webbläsare för manuell inloggning. Klicka OK i dialogrutan när du är klar. |

**Avancerat / Advanced:**

| Inställning | Beskrivning |
|---|---|
| **Hybrid-motor** | Väljer automatiskt `requests` eller `Selenium` per sida |
| **Trafilatura** | Aktiverar AI-optimerad textextraktion |
| **Sitemap.xml** | Förladdas rekursivt för effektivare crawling |
| **robots.txt** | Respekterar webbplatsens crawling-regler |
| **Strikt Domän** | Tvingar crawlern att stanna på exakt angiven domän |
| **Uteslut ord i URL** | Kommaseparerad lista – matchande sidor hoppas över |
| **Kräv ord i URL** | Crawlern besöker bara sidor vars URL innehåller minst ett av dessa ord |

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
| HTTP-hämtning | Requests (Session + automatisk retry) |
| Textextraktion | BeautifulSoup4 + Trafilatura |
| JS-rendering | Selenium + ChromeDriverManager |
| Caching | SQLite3 (WAL-läge, trådsäker) |
| Loggning | RotatingFileHandler |
| Parallellism | ThreadPoolExecutor (dokumentnedladdning) |

---

## ⚖️ Etik och Ansvar

Detta verktyg är utvecklat för laglig och etisk datainsamling. Användaren ansvarar för att:

- Följa webbplatsens användarvillkor.
- Inte överbelasta servrar – använd den inbyggda fördröjningsfunktionen.
- Respektera de begränsningar som anges i `robots.txt`.
- Säkerställa att insamlad data hanteras i enlighet med GDPR och tillämplig lagstiftning.
