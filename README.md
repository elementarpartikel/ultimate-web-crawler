# 🕸️ Webbdammsugare Pro v1.4 (AI & RAG Edition)

[![Ladda ner .exe för Windows](https://img.shields.io/badge/Ladda_ner-.exe-blue?style=for-the-badge&logo=windows)](https://github.com/elementarpartikel/ultimate-web-crawler/releases/latest)

![Skärmdump av GUI](screenshots/gui_preview.png)

**Webbdammsugare Pro** är ett professionellt verktyg för att skrapa, strukturera och lagra innehåll från webbplatser. Det är särskilt framtaget för att generera högkvalitativ textdata för AI-modeller, RAG-pipelines och vektordatabaser.

---

## 🚀 Huvudfunktioner

- **Hybridmotor:** Växlar intelligent mellan blixtsnabb hämtning via `requests` och dynamisk rendering via `Selenium` vid behov – ger maximal kompatibilitet med moderna webbplatser.
- **Content-Type Routing:** Kontrollerar serverns headers *innan* en fil laddas ned. Mediafiler ignoreras direkt, och dokument utan filändelse i URL:en skickas automatiskt till dokumenthanteraren – sparar minne och bandbredd.
- **Selenium PDF-skydd:** Konfigurerar Chrome att aldrig öppna PDF:er i webbläsarfliken, vilket annars får crawlern att fastna. Nedladdade filer hamnar automatiskt i rätt mapp.
- **Smarta filnamn:** Läser `content-disposition`-headern för att hitta det verkliga filnamnet vid nedladdning (t.ex. `Årsredovisning_2024.pdf`) även om URL:en ser ut som `download?id=9942`. Unik hash i filnamnet garanterar att inga filer skrivs över.
- **AI/RAG-Optimerad:** Extraherar ren text rensad från menyer, footers och skräpkod med hjälp av `Trafilatura`, redo att användas direkt i vektordatabaser.
- **Intelligent Caching:** Använder SQLite för att spåra innehållsändringar via SHA-256-hash och undvika att skrapa oförändrade sidor om igen (Incremental Crawling).
- **Sitemap-index stöd:** Parsar sitemap-index-filer rekursivt och följer länkade undersitemaps automatiskt – fångar alla URL:er även på stora sajter.
- **Prioriterad URL-kö:** URL:er från `sitemap.xml` bearbetas med högre prioritet för ett mer strukturerat och effektivt crawlflöde.
- **Canonical-hantering:** Känner igen canonical-taggar och lägger automatiskt till den kanoniska URL:en i kön med hög prioritet. Originalsidan hoppas över för att undvika dubbletter i RAG-data.
- **URL-filter:** Avancerade regler för att styra vilka sidor som besöks. Uteslut sidor vars URL innehåller valda nyckelord, eller kräv att URL:en innehåller ett visst ord – användbart för att låsa crawlern till en specifik del av en sajt eller plattform.
- **Automatisk index-fil:** Genererar en `index.csv` vid körningens slut med en fullständig översikt av alla besökta sidor (URL, titel, datum och filnamn).
- **Avancerat Skydd:** Inbyggt skydd mot evighetsloopar i sitemap-kedjor, URL-längdsbegränsning, domänspärrning samt hantering av `robots.txt`.
- **Flera Körlägen:** Stöd för osynligt läge (`headless`), synligt läge (`visible`) eller manuell inloggning innan automatiserad skrapning (`login_then_headless`).
- **Pausa & Återuppta:** Crawlningen kan pausas och återupptas mitt i körningen utan att data går förlorad.
- **Utökad Dokumenthantering:** Identifierar och laddar automatiskt ned PDF, DOCX, XLSX, PPTX, CSV, ODT, Markdown m.fl. i en separat mapp. Pågående nedladdningar slutförs alltid säkert innan programmet stängs.
- **Valbart utdataformat:** Sparar extraherad text som `.md` (Markdown, rekommenderas för AI/LLM) eller `.txt` (klassiskt format) – väljs direkt i GUI:t.
- **Crawldjup:** Inställning för max länkdjup från startsidan (0 = obegränsat). Förhindrar att crawlern fastnar i djupa, irrelevanta delar av en sajt.
- **Automatisk retry:** HTTP-anrop återförsöks automatiskt vid tillfälliga nätverksfel med exponentiell backoff – mer stabilt på ostadiga anslutningar.
- **URL-deduplicering:** `index.html` och `index.php` normaliseras bort automatiskt så att startsidan inte crawlas dubbelt. Tracking-parametrar som `utm_source`, `fbclid` m.fl. rensas från URL:er.
- **Minnesövervakning:** Övervakar RAM-användning via `psutil` och utlöser automatisk garbage collection vid >1 500 MB.
- **Roterande loggfiler:** Körloggar sparas per session med `RotatingFileHandler` (max 5 MB × 2 backupfiler).

---

## ✅ Krav

| Krav | Detalj |
|---|---|
| **Python** | 3.8 eller senare |
| **Google Chrome** | Måste vara installerat på datorn |
| **ChromeDriver** | Hanteras automatiskt av `webdriver-manager` |

> **Obs!** `webdriver-manager` laddar automatiskt ned rätt version av ChromeDriver för din Chrome-installation – du behöver inte göra något manuellt. Däremot måste **Google Chrome** finnas installerat, annars kan varken hybrid-motorn, headless-läget eller `login_then_headless` användas.
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
pip install requests beautifulsoup4 lxml selenium webdriver-manager trafilatura
```

| Paket | Funktion |
|---|---|
| `requests` | HTTP-hämtning |
| `beautifulsoup4` | HTML-parsning |
| `lxml` | XML-parsning för sitemap-stöd |
| `selenium` + `webdriver-manager` | JS-rendering via Chrome |
| `trafilatura` | AI-optimerad textextraktion (rekommenderas starkt) |

**3. Installera valfria beroenden** (aktiverar extrafunktioner):
```bash
pip install psutil
```

| Paket | Funktion |
|---|---|
| `psutil` | Realtidsövervakning av minnesanvändning |

---

## 🖥️ Användning

Starta applikationen:
```bash
python ultimate-web-crawler.py
```

### GUI-inställningar

**Grundinställningar:**

| Inställning | Beskrivning |
|---|---|
| **Startadress** | Ange komplett URL inklusive `https://` |
| **Fördröjning** | Sekunder mellan varje förfrågan (standard: 0.5 s) |
| **Max sidor** | Sätt till `0` för att crawla hela sajten |
| **Max djup** | Antal länknivåer från startsidan (0 = obegränsat) |
| **Utdataformat** | `.md` (rekommenderas för AI) eller `.txt` |
| **Ladda ned dokument** | Aktivera för att spara PDF, DOCX m.m. |
| **Körläge** | Se tabellen nedan |
| **Mapp** | Output-katalog för sparade filer |

**Körlägen:**

| Läge | Beskrivning |
|---|---|
| `headless` | Osynlig Chrome-instans (standard, snabbast) |
| `visible` | Synlig webbläsare – bra för felsökning |
| `login_then_headless` | En synlig webbläsare öppnas så du kan logga in. Klicka OK i programmet när du är klar, så fortsätter dammsugningen osynligt. |

**Avancerat:**

| Inställning | Beskrivning |
|---|---|
| **Hybrid-motor** | Låter programmet välja `requests` eller `Selenium` per sida |
| **Trafilatura** | Aktiverar AI-optimerad textextraktion |
| **Sitemap.xml** | Förladdas automatiskt för effektivare crawling, inklusive sitemap-index |
| **robots.txt** | Respekterar webbplatsens crawling-regler |
| **Strikt Domän** | Tvingar crawlern att stanna på exakt angiven domän. Bocka ur för att tillåta underdomäner. |
| **Uteslut ord i URL** | Kommaseparerad lista – sidor vars URL innehåller dessa ord hoppas över |
| **Kräv ord i URL** | Kommaseparerad lista – crawlern besöker bara sidor vars URL innehåller minst ett av dessa ord. Användbart för att hålla sig till en specifik del av en delad plattform (t.ex. `utb.tyreso.se`) |

---

## 📂 Output-struktur

```
crawl_output/
├── texter/                          # En fil per skrapad sida (.md eller .txt)
│   └── sidnamn_a1b2c3.md
├── dokument/                        # Nedladdade PDF, DOCX, XLSX m.m.
├── logs/
│   └── crawl_YYYYMMDD_HHMMSS.log
├── index.csv                        # Automatisk översikt: URL, titel, datum, filnamn
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
| GUI | Tkinter + ttk |
| HTTP-hämtning | Requests (med Session + automatisk retry) |
| Textextraktion | BeautifulSoup4 + Trafilatura |
| JS-rendering | Selenium + ChromeDriverManager |
| Caching | SQLite3 (WAL-läge) |
| Loggning | RotatingFileHandler |
| Parallellism | ThreadPoolExecutor (dokumentnedladdning) |

---

## ⚖️ Etik och Ansvar

Detta verktyg är utvecklat för laglig och etisk datainsamling. Användaren ansvarar för att:

- Följa webbplatsens användarvillkor.
- Inte överbelasta servrar – använd den inbyggda fördröjningsfunktionen.
- Respektera de begränsningar som anges i `robots.txt`.
- Säkerställa att insamlad data hanteras i enlighet med GDPR och tillämplig lagstiftning.
