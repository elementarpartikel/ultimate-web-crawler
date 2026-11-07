# Webbdammsugare Pro

**Svenska** · [English](README.en.md)

Webbdammsugare Pro hämtar innehåll från webbplatser och sparar källmärkt text som Markdown, JSON eller klartext. Du anger en startadress och crawlern följer länkarna. Text från PDF- och Office-dokument kan också tas med.

## Installera på Windows

Du behöver Python 3.11 eller senare. Skrivbordsappen är testad på Windows 11 med Python 3.14 och Tk 9.0.

1. [Hämta projektet som ZIP](https://github.com/elementarpartikel/ultimate-web-crawler/archive/refs/heads/main.zip) och packa upp det.
2. Öppna mappen där `ultimate-web-crawler.py` ligger. Skriv `powershell` i Utforskarens adressfält och tryck Enter.
3. Kör kommandona nedan. De tre första behövs vid installationen. Det sista startar appen.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe ultimate-web-crawler.py
```

## Gör din första körning

1. Skriv in **Startadress** för en publik webbplats som du får hämta. Behåll körläget **Standard (ingen inloggning)**.
2. Sätt **Max sidor** till exempelvis `10` för en kort första körning. `0` betyder obegränsat.
3. Välj **Filformat**. `.md` ger en läsbar textfil med källhänvisning per sida och `.json` ger strukturerade sidfiler. Vill du ha text från PDF- och Office-filer, kryssa i både **Ladda ner dokument** och **Konvertera dokument till Markdown**.
4. Klicka **Välj mapp…** och välj var filerna ska sparas. Klicka sedan **Starta**.
5. När körningen är klar, klicka **Öppna mapp** och titta i `texter/`. Nedladdade original finns i `dokument/`. **Öppna rapport** visar fel och varningar från körningen.

## Läs vidare

- [Serverläge (CLI)](docs/anvandarguide.md#serverläge) för körning från terminal eller schema.
- [Avancerade inställningar](docs/anvandarguide.md#avancerade-inställningar) för mallar, inloggning och urval.
- [Utdata](docs/anvandarguide.md#utdata) för filformat, `chunks.jsonl` och inläsning via `current.json`. Se även [OCR för skannade PDF:er](docs/anvandarguide.md#ocr-för-skannade-pdfer).

## Tester

Testerna använder lokala testservrar. Installera `pytest` i projektmiljön och kör `.\.venv\Scripts\python.exe -m pytest tests -q`.
