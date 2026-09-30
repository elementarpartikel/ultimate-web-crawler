# CLAUDE.md – Webbdammsugare Pro (Ultimate Web Crawler)

Asynkron webbcrawler med GUI (CustomTkinter) och serverläge (CLI). Byggd av Fredrik Eriksson (AI Sweden).
Huvudsyfte: ladda ner hela webbplatser/intranät som ren, källmärkt text (Markdown/JSON) att använda som
kunskapskällor i **Svea**, AI Swedens assistent (RAG). Allt som ändrar utdata ska bedömas utifrån frågan:
*blir det bättre eller sämre underlag för retrieval och källhänvisning i Svea?*

Språk: kod-kommentarer, loggar och GUI är på **svenska** (GUI även engelska). Skriv nya kommentarer,
loggmeddelanden och användartexter på svenska, och lägg till engelsk översättning i `AppGUI.texts`.

## Filer

| Fil | Roll |
|---|---|
| `ultimate-web-crawler.py` | Hela programmet (~4 800 rader, en fil). Filnamnet har bindestreck → kan inte importeras normalt; tester laddar det med `importlib`. |
| `tests/test_crawler.py` | pytest-tester mot en lokal aiohttp-testserver (ingen riktig sajt). |
| `sites.example.json` | Mallar. Kopieras till `sites.json` (läses av GUI:t och `--config`). `sites.json` är gitignorerad. |
| `requirements.txt` | Beroenden med nedre gränser. |
| `README.md` | Användardokumentation (sv). Uppdatera vid nya config-nycklar eller ändrad utdata. |

Ingen git-repo lokalt. Miljö: Windows 11, Python 3.11+ (använder `asyncio.TaskGroup`; testat på 3.14).

## Köra och testa

```bash
pip install -r requirements.txt && playwright install chromium
python ultimate-web-crawler.py                                   # GUI
python ultimate-web-crawler.py --config sites.json [--output DIR] [--webhook URL]   # serverläge, exit-kod 0/1/2
python -m pytest tests -q                                        # ska vara grönt före varje leverans
```

Crawla aldrig skarpa sajter för att testa. Använd testservern i `tests/` (klassen `Site`) eller
`python -m http.server`. Skriv ett test för varje buggfix – särskilt för inkrementell crawl, `max_pages`
och dokumenthantering, där felen tidigare var tysta.

## Arkitektur (sök på namn, radnummer flyttar sig)

- **Hjälpfunktioner** (överst): `normalize_url`, `get_clean_hash`, `stable_filename`, `semantic_chunk_text`
  (radvis, med `heading_path`/`context`), `strip_cms_boilerplate`, `downgrade_body_h1`, `csv_safe`, `markdown_to_plain`.
- **`AsyncCrawlDatabase`**: SQLite (aiosqlite, WAL). `page_cache` (hash, etag, last_modified, **links_json**, **filename**)
  och `doc_cache` (dokumentens filnamn/ETag/referer). Batchade commits. Äldre databaser migreras automatiskt.
- **`PerDomainRateLimiter`** (har `slow_down()` för 429/503), **`PriorityURLQueue`** (konfigurerbara boost/penalty-ord,
  årtalsregex, `mark_seen`).
- **`LoginDetector`**, **`DocumentManifest`** (in-memory, skrivs som `manifest_<domän>.json`).
- **`DocumentConverter`**: PDF/DOCX/XLSX/PPTX, äldre format via LibreOffice. `md_filename()` är den ENDA platsen som
  bestämmer namnet på `_doc.md` – använd `converter.md_path_for(url, filename)`, bygg aldrig namnet för hand.
- **Säkerhetshjälpare**: `SafeResolver` (blockerar privata IP:n), `is_non_public_host`, `RobotsRules` (Protego/stdlib),
  `safe_gunzip`, `magic_ok`, `atomic_write_text`, `detect_prompt_injection`, `validate_site_config`, `KNOWN_CONFIG_KEYS`.
- **`AsyncWebCrawler`**: `crawl()` (fångar alla fel, skickar alltid `summary` + `done`) → `_crawl_inner()` (DB, inloggning,
  session, robots/sitemap, dispatch-loop med backpressure och `max_pages`-räkning) → `process_page()` → `fetch()` /
  `_render_with_playwright()` → `extract_structured_data()` → `_write_page_file()` → `db.save_cache()` →
  `_enqueue_links()`. Dokument: `download_document()` → `_download_via_aiohttp()` (.part + atomärt byte) → `_convert_document()`.
  Slutfiler: `_generate_index/_manifest/_report` (index.csv, manifest, `changes.jsonl`, `crawl_report.json`).
- **`run_cli_mode`** validerar config, ger varje sajt egen mapp, returnerar exit-kod.
- **`AppGUI`**: CustomTkinter. Crawlern körs i egen tråd; kommunikation via `msg_queue`
  (`log`, `table`, `stats_data`, `summary`, `login_wait`, `done`). Inställningar sparas i `~/.ultimate_web_crawler_settings.json`.

## Konfiguration (config-dict = en post i `sites.json`)

Alla giltiga nycklar står i `KNOWN_CONFIG_KEYS` (okända nycklar ger varning i serverläget). Grundnycklar finns i GUI:t;
avancerade (`cookie_file`, `user_agent`, `allowed_domains`, `max_*`, `keep_*`, `ignore_https_errors` m.fl.) finns bara i
`sites.json` och README. En ny GUI-nyckel måste läggas till i: `AppGUI._build_ui`, `_apply_template`, `start_crawl`,
`_collect_settings`/`_load_settings`, `KNOWN_CONFIG_KEYS`, `sites.example.json` och README.

## Utdata (kontrakt mot Svea – ändra inte utan att ta hänsyn till nedströmsintag)

```
texter/<slug>_<md5:8>.md|json|txt     en fil per sida (stabilt namn från URL)
texter/<slug40>_<md5:8>_doc.md        konverterade dokument
dokument/…                            originalfiler
chunks.jsonl                          alla chunks, en per rad (byggs från disk → alltid komplett)
changes.jsonl / crawl_report.json / index.csv / manifest_*.json
```

Invarianter att bevara:
- **Käll-URL i brödtexten**, både överst och sist (`**Källa:** …`), inte bara i header. Datum/språk också i brödtexten.
- Varje JSON-chunk har `url`, `heading`, `heading_path`, `context`, `chunk_index`, `total_chunks`.
- Filnamn är stabila mellan körningar (`stable_filename`, `DocumentConverter.md_filename`).
- PII-tvätt körs *före* hashning och skrivning, och gäller ALL sparad text: innehåll, titlar, länktexter, dokumenttitlar.
- `H1` i brödtext nedgraderas till `H2`. CMS-boilerplate rensas i alla format.
- Filer skrivs atomärt (`atomic_write_text`, `.part` för dokument) – en avbruten körning får aldrig lämna halva filer.
- Inkrementell crawl måste kunna spela upp sparade länkar vid 304 och vid sitemap-`lastmod`-hopp (`_replay_cached`).
- Sidor som inte sparas (dubblett, canonical-kopia, fel språk) får `filename=None`; dubbletter/canonical-kopior sparas utan ETag
  så att de omprövas varje körning och läker om originalet försvinner.

## Konventioner

- All nätverks-IO är `async`; blockerande arbete (BS4/trafilatura, konvertering, filskrivning) via `asyncio.to_thread`.
- Kontrollera `self.state` (`STOPPED`/`PAUSED`) vid varje väntepunkt.
- Valfria bibliotek guardas med `HAS_*`. **GUI-beroenden är valfria**: serverläget ska fungera utan tkinter.
- Logga via `self._log(msg, LogLevel.X)`; DEBUG går bara till fil, inte till GUI:t.
- Håll crawlern artig och ärlig: respektera robots.txt och `delay`, identifiera dig med `user_agent`, aldrig utge dig för en webbläsare som standard.

## Säkerhet och dataskydd

Crawlern körs mot kommunala intranät (Playwright-inloggning) och publika sajter; utdata kan innehålla personuppgifter
och åtkomstbegränsat innehåll. Regler: inga credentials i kod/loggar; `cookie_file` är hemlig; ingen `ignore_https_errors`
utan uttryckligt val; privata IP-adresser blockeras när startadressen är publik (intranät = automatiskt tillåtet);
alla storlekar begränsas; XML med entiteter avvisas; allt crawlat innehåll är opålitlig indata (flagga prompt-injektion).
Namn maskeras inte av PII-tvätten – påstå aldrig att utdata är "GDPR-säker".

## Kända begränsningar (ej åtgärdade)

OCR och LibreOffice-konvertering är bara testade i kopplingen (attrapper), inte mot de riktiga programmen. Dubblettkontrollen
är exakt (hash av texten), inte fuzzy. `Playwright` kör utan DNS-skydd (bara IP-literaler blockeras). PDF/Office-parsning sker i
samma process. Filen är fortfarande monolitisk (bra nästa steg: dela i paket).
