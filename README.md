# 🕸️ Ultimat Webbdammsugare v3.5 (AI & RAG Edition)

**Ultimat Webbdammsugare** är ett professionellt verktyg utvecklat för att effektivt skrapa, strukturera och lagra innehåll från webbplatser. Den är särskilt framtagen för att generera högkvalitativ textdata för AI-modeller och RAG-pipelines.

## 🚀 Huvudfunktioner

* **Hybridmotor:** Växlar intelligent mellan blixtsnabb hämtning via `Requests` och dynamisk rendering via `Selenium` vid behov.
* **AI/RAG-Optimerad:** Extraherar ren text rensad från menyer, footers och skräpkod, redo att användas i vektordatabaser.
* **Intelligent Caching:** Använder SQLite för att hålla koll på ändringar och undvika att skrapa samma innehåll flera gånger (Incremental Crawling).
* **Avancerat Skydd:** Inbyggt skydd mot evighetsloopar, URL-skydd och hantering av `robots.txt` samt `sitemap.xml`.
* **Flera Körlägen:** Stöd för osynligt läge (headless), synligt läge eller manuell inloggning innan automatiserad skrapning påbörjas.
* **Dokumenthantering:** Kan automatiskt identifiera och ladda ner dokument som PDF, DOCX och XLSX.

## 🛠️ Installation

För att köra verktyget lokalt behöver du Python 3.8+ installerat.

1. Klona detta repository:
   ```bash
   git clone [https://github.com/DITT-ANVANDARNAMN/ultimate-web-crawler.git](https://github.com/DITT-ANVANDARNAMN/ultimate-web-crawler.git)
   cd ultimate-web-crawler
Installera nödvändiga bibliotek:

Bash
pip install requests beautifulsoup4 selenium webdriver-manager trafilatura python-dotenv
(Tips: trafilatura rekommenderas starkt för högkvalitativ textextraktion).

🖥️ Användning
Starta applikationen genom att köra huvudfilen:

Bash
python site_crawler4.py
Så här använder du GUI:t:
Startadress: Ange den kompletta URL:en (inklusive https://).

Motor: Välj "Hybrid" för att låta programmet själv avgöra om JavaScript krävs.

Körläge: Välj "login_then_headless" om du behöver logga in manuellt på en sida innan skrapningen börjar.

Output: Alla texter sparas som enskilda .txt-filer i mappen texter, och dokument hamnar i dokument.

🏗️ Teknisk Stack
GUI: Tkinter

Parsning: BeautifulSoup4 & Trafilatura

Browser Automation: Selenium med ChromeDriverManager

Databas: SQLite3

Loggning: RotatingFileHandler för systemloggar

⚖️ Etik och Ansvar
Detta verktyg är utvecklat för laglig och etisk datainsamling. Användaren ansvarar för att:

Följa webbplatsens användarvillkor.

Inte överbelasta servrar (använd den inbyggda fördröjningsfunktionen).

Respektera de begränsningar som anges i robots.txt.

Utvecklad med fokus på stabilitet och hastighet för moderna AI-projekt.
