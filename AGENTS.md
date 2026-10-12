# Projektinstruktioner – Webbdammsugare Pro (Ultimate Web Crawler)

Asynkron webbcrawler med GUI (CustomTkinter) och serverläge (CLI). Byggd av Fredrik Eriksson (AI Sweden).
Huvudsyfte: ladda ner hela webbplatser/intranät som ren, källmärkt text (Markdown/JSON) att använda som
kunskapskällor i **Svea**, AI Swedens assistent (RAG). Allt som ändrar utdata ska bedömas utifrån frågan:
*blir det bättre eller sämre underlag för retrieval och källhänvisning i Svea?*

Språk: kod-kommentarer, loggar och GUI är på **svenska** (GUI även engelska). Skriv nya kommentarer,
loggmeddelanden och användartexter på svenska, och lägg till engelsk översättning i `AppGUI.texts`.

## Filer

| Fil | Roll |
|---|---|
| `ultimate-web-crawler.py` | Crawler, GUI och CLI. Filnamnet har bindestreck → kan inte importeras normalt; tester laddar det med `importlib`. |
| `crawler_runtime.py` | Begränsade workerprocesser, processträdsavslut, arbetsmappar, utmappslås och generationspublicering. |
| `conversion_worker.py` | Intern ingång för en enda PDF/Office/OCR-konvertering; ska ligga bredvid huvudskriptet. |
| `document_conversion.py` | PDF/Office/OCR med parsers som laddas efter filtyp, utan GUI/HTML/nätverksimport. |
| `crawler_text.py` | Gemensam PII-tvätt, texthjälpare och loggnivåer för crawler och worker. |
| `tests/test_crawler.py` | pytest-tester mot en lokal aiohttp-testserver (ingen riktig sajt). |
| `sites.example.json` | Mallar. Kopieras till `sites.json` (läses av GUI:t och `--config`). `sites.json` är gitignorerad. |
| `requirements.txt` | Beroenden med nedre gränser. |
| `README.md` | Användardokumentation (sv). Uppdatera vid nya config-nycklar eller ändrad utdata. |

Git-repo: https://github.com/elementarpartikel/ultimate-web-crawler. Miljö: Windows 11, Python 3.11+
(använder `asyncio.TaskGroup`; testat på 3.14).
Commits använder projektägarens konto. Lägg inte till automatiska AI-medförfattare eller AI-attributioner.

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
- **`AsyncCrawlDatabase`**: SQLite (aiosqlite, WAL). `page_cache` (hash, etag, last_modified, **links_json**, **filename**,
  **fetched_at**, **checked_at**) och `doc_cache` (filnamn/ETag/referer, **content_hash**, **converted_hash**,
  **md_filename**, **conversion_profile**). Batchade commits. Äldre databaser migreras automatiskt.
  Dokumentcachen har också `fetched_at` (den lagrade versionens hämtningstid) och `conversion_context`
  (käll-/länk-/serverdatummetadata som konverteringen bygger på). 304 får inte hitta på en hämtningstid.
  Sidcachen har också `fetch_url` (begärd adress), `source_url` (slutadress), `processing_profile`, `output_hash`
  och `rendered`. Bearbetningsprofilen omfattar textbehandling, inte URL-/språkurval eller hämtmotor.
  Profilbyte tvingar ny bearbetning och hindrar export av äldre texter, även vid begränsad körning.
  Urvalet prövas separat vid export. Rapporten sparar `page_processing_profile` och `document_processing_profile`
  för framtida migrationer. Begränsad migration från äldre obeskrivna profiler får inte tyst tömma korpusen.
- **`PerDomainRateLimiter`** (har `slow_down()` för 429/503), **`PriorityURLQueue`** (konfigurerbara boost/penalty-ord,
  årtalsregex, `mark_seen`).
- **`LoginDetector`**, **`DocumentManifest`** (in-memory, skrivs som `manifest_<domän>.json`).
- **`DocumentConverter`**: PDF/DOCX/XLSX/PPTX, äldre format via LibreOffice. `md_filename()` är den ENDA platsen som
  bestämmer namnet på `_doc.md` – använd `converter.md_path_for(url, filename)`, bygg aldrig namnet för hand.
  Produktionscrawlen använder `ConversionProcessRunner` i en begränsad kö med `conversion_workers`,
  `conversion_queue_size` och `conversion_timeout`. Ingen PDF/Office/OCR-parsning får flyttas tillbaka till crawltrådar.
- **Säkerhetshjälpare**: `SafeResolver` (blockerar privata IP:n), `is_non_public_host`, `RobotsRules` (Protego/stdlib),
  `BrowserEgressProxy` (SOCKS5 med DNS-pinning för publika Chromium-körningar), `safe_gunzip`, `magic_ok`,
  `atomic_write_text`, `detect_prompt_injection`, `validate_site_config`, `KNOWN_CONFIG_KEYS`.
- **`AsyncWebCrawler`**: `crawl()` (fångar alla fel, skickar alltid `summary` + `done`) → `_crawl_inner()` (DB, inloggning,
  session, robots/sitemap, dispatch-loop med backpressure och `max_pages`-räkning) → `process_page()` → `fetch()` /
  `_render_with_playwright()` → `extract_structured_data()` → `_write_page_file()` → `db.save_cache()` →
  `_enqueue_links()`. Dokument: `download_document()` → `_download_via_aiohttp()` (.part + atomärt byte) → `_convert_document()`.
  Slutfiler: `_generate_index/_manifest/_report` (index.csv, manifest, `changes.jsonl`, `crawl_report.json`).
- **`PublicationWorkspace`**: kopierar/hårdlänkar föregående generation till `.working/<id>`, tar separat SQLite-backup,
  och publicerar till `.generations/<id>` med en atomisk `current.json`-commit. Konverteringar och DB måste vara
  avslutade före commit. Toppnivåns filer är kompatibilitetskopior; RAG-läsare pinnar generationen en gång.
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
chunks.jsonl                          chunks från registrerade filer och rätt dokumentversion
texter_utgangna/                      arkiv för oregistrerade äldre texter (ingår inte i korpusen)
changes.jsonl / crawl_report.json / index.csv / manifest_*.json
```

Samma struktur finns i den publicerade generationsmappen. `current.json` är den officiella ingången;
toppnivåfilerna finns kvar för kompatibilitet och byts inte gemensamt atomärt. Rapporten på toppnivå
beskriver senaste försök; rapporten i generationen hör till den leveransen. Cache återställs från generationen.

Invarianter att bevara:
- **Käll-URL i brödtexten**, både överst och sist (`**Källa:** …`), inte bara i header. Datum/språk också i brödtexten.
- Varje JSON-chunk har `url`, `heading`, `heading_path`, `context`, `chunk_index`, `total_chunks`.
- Filnamn är stabila mellan körningar (`stable_filename`, `DocumentConverter.md_filename`).
- PII-tvätt körs *före* hashning och skrivning, och gäller ALL sparad text: innehåll, titlar, länktexter, dokumenttitlar.
- `H1` i brödtext nedgraderas till `H2`. CMS-boilerplate rensas i alla format.
- Filer skrivs atomärt (`atomic_write_text`, `.part` för dokument) – en avbruten körning får aldrig lämna halva filer.
- Alla ändringar görs i arbetsmappen. Avbrott/fatala exportfel före commit får inte ändra den tidigare generationen.
  Publiceringen byter bara `current.json` efter att hela leveransen är klar. Läsare ska inte följa pointer på nytt mitt
  i samma inläsning. Cache/texter_utgangna är interna och ingår inte i `artifacts`/korpus.
- Publicera aldrig `failed`, också vid frånvaro av fatal_error. Ett stort oförklarat bortfall (>50 % av tidigare
  rena källfiler) stoppar publicering. Soft-404/korta 200-svar räknas inte som belagda massborttagningar.
  HTTP 404/410 är normala borttagningar; även en verkligt tömd sajt får publiceras utan driftfel.
- Generationsfiler får aldrig modifieras på plats. Återanvända hårdlänkar i arbetsmappen byts genom os.replace;
  kompatibilitetskopior på toppnivå får inte hårdlänkas till generationen. Tidigare generationer behålls för läsare.
  Valbar `generation_keep`/`generation_min_age_hours` städar bara äldre generationer efter respit från pensionering;
  aktuell generation och N senaste skyddas. Läsarens maximala inläsningstid måste rymmas inom respiten.
- En dokumentprocess får bara egna indatakopior och temporära filer. Registrera dess utdata först när hash och
  metadata fortfarande matchar cacheposten. Timeout/stop ska avsluta hela processträdet, också LibreOffice-barn.
  Avbrutna to_thread-skrivningar måste awaitas innan arbetsmappen städas (`blocking_complete`).
- Inkrementell crawl måste kunna spela upp sparade länkar vid 304 och vid sitemap-`lastmod`-hopp (`_replay_cached`).
- Normaliserade URL:er används som nycklar; faktisk URL används för hämtning. Länkar och canonical använder
  slutadressen och `<base href>` som bas. Metadatafält skrivs på en rad så att de inte kan skapa falska källfält.
- Sitemap-hopp får bara uppdatera `crawled_at`; `checked_at` ändras vid verkligt HTTP-svar (även 304), `fetched_at`
  när innehåll hämtas. Äldre poster utan kontrolltid ska kontrolleras igen.
- Dokumentens storlek bevisar aldrig oförändrat innehåll. `_doc.md` får exporteras bara när `converted_hash`
  matchar originalets faktiska SHA-256 och `conversion_profile` är aktuell. Misslyckad konvertering måste
  försöka igen vid nästa kontroll, även efter 304. Registrera ändringshändelser också för borttagen/återskapad text.
  Beräkna faktisk SHA-256 minst en gång varje körning; återanvänd bara inom körningen när filidentitet och
  storlek/mtime_ns/ctime_ns är oförändrade. Beständig stat-cache får inte ersätta denna integritetskontroll.
- `_prepare_export` tar fram filregistret före export, arkiverar oregistrerade texter och fyller dokumentmanifestet.
  Begränsade körningar får inte ta bort registrerade källor bara för att de inte hunnit besökas.
- Sidor som inte sparas (dubblett, canonical-kopia, fel språk) får `filename=None`; dubbletter/canonical-kopior sparas utan ETag
  så att de omprövas varje körning och läker om originalet försvinner.
- Canonical/dubbletter avgörs i `_finalize_page_duplicates` efter hämtningarna. Canonical-målet måste ha bekräftats
  i samma körning och vara exportbart. Ett otillgängligt eller obehandlat mål får inte ta bort källtexten.
- Renderade JS-sidor får inte hoppas över via sitemap-lastmod. Browser-hämtning bevarar HTTP-status.
  Publika 401/403 är hämtningsfel och får inte radera föregående sidtext. Sessionsprober delas i högst en minut.
- Robots-regler gäller per origin (scheme/värd/port) och kontrolleras före hämtning och varje redirect.
  4xx tillåter, medan 5xx/nätverksfel nekar och rapporteras. Robots-hämtningar undantas från egen regelkontroll.
  Gör upp till två omförsök. Otillgängliga robots-regler får stoppa hämtning men aldrig gallra sparad export.
  Alla riktiga HTTP-anrop, även sitemaps/retries/redirects, följer rate limit; sitemap-cacheträffar väntar inte.
  Adaptiv bromsning/återhämtning gäller svarsvärden; en annan värd får inte påverkas av dess 429/503.
- `crawl_report.json.status` är success/partial/failed. CLI ger 1 vid resurs-/konverterings-/exportfel eller stopp;
  avsiktliga urvalsgränser och normala borttagningar är inte fel. Rapporten skrivs också vid tidiga startfel.
- Browser-dokument tas över via CDP Fetch efter headers och läses i begränsade IO-block. Native downloads
  är avstängda; fallback används bara i inloggningsläge efter 401/403 eller HTML. Bevara proxy/DNS-pinning.
  SOCKS-proxyn använder råa asynkrona sockets med explicit close (även efter ConnectionResetError);
  återinför inte asyncio.Server/Proactor-transporter utan att testa återställda klientanslutningar på Windows.
- Dokumenttext och `.txt` följer samma flagg-/karantänpolicy som Markdown/JSON. `.txt` ingår i JSONL-exporten.
- Dokumentets `fetched_at`, `created_at`, `document_modified_at` och `server_modified_at` är separata.
  Tekniska datum får aldrig beskrivas som giltighetsdatum. Använd inte datum från LibreOffices temporärfil.
  Titel/källa/datum upprepas per sida/bild/långt avsnitt i Markdown, men upprepningen tas bort ur JSONL-brödtexten.
- PDF-tabeller och marginalrensning får inte tappa råtext vid parserfel. Flagga injektion före marginalrensning.
  Baslinjejusterade etikett-/värderader får inte läsas som självständiga textspalter.
  OCR-gränsen gäller antal försök, också misslyckade. PPTX-grupper/tabeller/talarnoteringar extraheras rekursivt.
- Små syskonsektioner får slås ihop, med `heading_paths` för de ursprungliga rubrikvägarna. Blanda inte föräldrar,
  PDF-sidor, PowerPoint-bilder eller Excel-blad. PDF får `page_number/page_url`, PowerPoint får `slide_number`.

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

Verkliga OCR-/LibreOffice-integrationstester finns men hoppas över här eftersom program/språkdata saknas; kopplingen
testas med attrapper. PDF/Word/Excel/PowerPoint och processkrasch/timeout/barnprocesser testas i riktiga processer.
Dubblettkontrollen är exakt (hash av normaliserad text), inte fuzzy. Processgränsen är inte en OS-sandbox.
Äldre toppnivåläsare behöver gå över till `current.json` för sammanhängande leveranser. Historiska generationer
behålls som standard; valbar städning kräver respit längre än pinnade läsares inläsningstid. Textarkiv/loggar städas
inte av generationsregeln. Browser-dokument kräver vanlig HTTP-navigation;
JS-genererade blob-downloads stöds inte.
Avancerade PDF-layouter och jämförelse mot pymupdf4llm återstår; layoutförbättringarna är verifierade med syntetiska referenser.
Huvudfilen är fortfarande stor; process-/publiceringsstödet finns i separata moduler. Övriga granskningsfynd
och nästa omgång finns i README:s åtgärdslista (2026-10-12).
