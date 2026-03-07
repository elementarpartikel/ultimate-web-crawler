# 🕸️ Ultimat Webbdammsugare v3.5 (AI & RAG Edition)

**Ultimat Webbdammsugare** är ett professionellt verktyg för att skrapa, strukturera och lagra innehåll från webbplatser. Det är särskilt framtaget för att generera högkvalitativ textdata för AI-modeller, RAG-pipelines och vektordatabaser.

---

## 🚀 Huvudfunktioner

- **Hybridmotor:** Växlar intelligent mellan blixtsnabb hämtning via `requests` och dynamisk rendering via `Selenium` vid behov – ger maximal kompatibilitet med moderna webbplatser.
- **AI/RAG-Optimerad:** Extraherar ren text rensad från menyer, footers och skräpkod med hjälp av `Trafilatura`, redo att användas direkt i vektordatabaser.
- **Intelligent Caching:** Använder SQLite för att spåra innehållsändringar via SHA-256-hash och undvika att skrapa oförändrade sidor om igen (Incremental Crawling).
- **Prioriterad URL-kö:** URLs från `sitemap.xml` bearbetas med högre prioritet för ett mer strukturerat och effektivt crawlflöde.
- **Avancerat Skydd:** Inbyggt skydd mot evighetsloopar, URL-längdsbegränsning, domänspärrning samt hantering av `robots.txt` och `sitemap.xml`.
- **Flera Körlägen:** Stöd för osynligt läge (`headless`), synligt läge (`visible`) eller manuell inloggning innan automatiserad skrapning (`login_then_headless`).
- **Pausa & Återuppta:** Crawlningen kan pausas och återupptas mitt i körningen utan att data går förlorad.
- **Dokumenthantering:** Identifierar och laddar automatiskt ned dokument som PDF, DOCX, XLSX, PPTX och CSV i en separat mapp.
- **Supabase-integration:** Valfritt stöd för att ladda upp skrapad data direkt till en Supabase-databas via `.env`-konfiguration.
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

Kräver Python 3.8+.

**1. Klona repositoryt:**
```bash
git clone https://github.com/DITT-ANVANDARNAMN/ultimate-web-crawler.git
cd ultimate-web-crawler
```

**2. Installera obligatoriska beroenden:**
```bash
pip install requests beautifulsoup4 selenium webdriver-manager trafilatura
```

**3. Installera valfria beroenden** (aktiverar extrafunktioner):
```bash
pip install supabase python-dotenv psutil pandas
```

| Paket | Funktion |
|---|---|
| `trafilatura` | Högkvalitativ textextraktion (rekommenderas starkt) |
| `supabase` + `python-dotenv` | Uppladdning av data till Supabase |
| `psutil` | Realtidsövervakning av minnesanvändning |
| `pandas` | Utökad datahantering |

**4. (Valfritt) Konfigurera Supabase:**

Skapa en `.env`-fil i projektmappen:
```
SUPABASE_URL=https://ditt-projekt.supabase.co
SUPABASE_KEY=din-anon-nyckel
```

---

## 🖥️ Användning

Starta applikationen:
```bash
python site_crawler4.py
```

### GUI-inställningar

**Grundinställningar:**
| Inställning | Beskrivning |
|---|---|
| **Startadress** | Ange komplett URL inklusive `https://` |
| **Fördröjning** | Sekunder mellan varje förfrågan (standard: 0.5 s) |
| **Max sidor** | Sätt till `0` för att crawla hela sajten |
| **Ladda ned dokument** | Aktivera för att spara PDF, DOCX m.m. |
| **Körläge** | Se tabellen nedan |
| **Mapp** | Output-katalog för sparade filer |

**Körlägen:**
| Läge | Beskrivning |
|---|---|
| `headless` | Osynlig Chrome-instans (standard, snabbast) |
| `visible` | Synlig webbläsare – bra för felsökning |
| `login_then_headless` | Öppnar synlig webbläsare i 60 sekunder för manuell inloggning, växlar sedan till osynligt läge med sparade cookies |

**Avancerat:**
- **Hybrid-motor** – låter programmet välja `requests` eller `Selenium` per sida
- **Trafilatura** – aktiverar AI-optimerad textextraktion
- **Sitemap.xml** – förladdas automatiskt för effektivare crawling
- **robots.txt** – respekterar webbplatsens crawling-regler
- **Uteslut ord i URL** – kommaseparerad lista med nyckelord; sidor vars URL innehåller dessa hoppar över

---

## 📂 Output-struktur

```
crawl_output/
├── texter/              # En .txt-fil per skrapad sida
│   └── sidnamn.txt      # Innehåller källa, titel, datum och ren text
├── dokument/            # Nedladdade PDF, DOCX, XLSX m.m.
├── logs/
│   └── crawl_YYYYMMDD_HHMMSS.log
└── domännamn_cache.db   # SQLite-cache för incremental crawling
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
| HTTP-hämtning | Requests (med Session) |
| Textextraktion | BeautifulSoup4 + Trafilatura |
| JS-rendering | Selenium + ChromeDriverManager |
| Caching | SQLite3 |
| Loggning | RotatingFileHandler |
| Parallellism | ThreadPoolExecutor (dokumentnedladdning) |
| Moln-integration | Supabase (valfritt) |

---

## ⚖️ Etik och Ansvar

Detta verktyg är utvecklat för laglig och etisk datainsamling. Användaren ansvarar för att:

- Följa webbplatsens användarvillkor.
- Inte överbelasta servrar – använd den inbyggda fördröjningsfunktionen.
- Respektera de begränsningar som anges i `robots.txt`.
- Säkerställa att insamlad data hanteras i enlighet med GDPR och tillämplig lagstiftning.
