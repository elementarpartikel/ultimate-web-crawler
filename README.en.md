# Web Crawler Pro

[Svenska](README.md) · **English**

Web Crawler Pro fetches website content and saves source-linked text as Markdown, JSON or plain text. Enter one start URL and the crawler follows the links. It can also extract text from PDF and Office documents.

## Install on Windows

You need Python 3.11 or later. The desktop app has been tested on Windows 11 with Python 3.14 and Tk 9.0.

1. [Download the project as a ZIP file](https://github.com/elementarpartikel/ultimate-web-crawler/archive/refs/heads/main.zip) and extract it.
2. Open the folder that contains `ultimate-web-crawler.py`. Type `powershell` in File Explorer's address bar and press Enter.
3. Run the commands below. The first three set up the project. The last one opens the app.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe ultimate-web-crawler.py
```

## Run your first crawl

1. Select **EN** if the app opens in Swedish. Enter the **Start URL** for a public website you have permission to crawl. Keep **Standard (no login)** as the run mode.
2. Set **Max pages** to a small number such as `10` for the first run. `0` means unlimited.
3. Choose a **File Format**. `.md` gives you a readable, source-linked text file per page, and `.json` gives you structured page files. To extract text from PDF and Office files, select both **Download documents** and **Convert documents to Markdown**.
4. Click **Browse…** and choose an output folder. Then click **Start**.
5. When the run finishes, click **Open folder** and look in `texter/`. Downloaded originals go to `dokument/`. **Open report** shows errors and warnings from the run.

## Learn more

- [Server mode (CLI)](docs/user-guide.md#server-mode) for terminal and scheduled runs.
- [Advanced settings](docs/user-guide.md#advanced-settings) for templates, login and filtering.
- [Output](docs/user-guide.md#output) for file formats, `chunks.jsonl` and imports through `current.json`. See also [OCR for scanned PDFs](docs/user-guide.md#ocr-for-scanned-pdfs).

## Tests

The tests use local test servers. Install `pytest` in the project's virtual environment and run `.\.venv\Scripts\python.exe -m pytest tests -q`.
