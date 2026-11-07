# Användarguide

[Startguide](../README.md) · [English](user-guide.md)

Installera beroendena enligt startguiden. På Linux och macOS använder du
`./.venv/bin/python` i stället för `.\.venv\Scripts\python.exe` i kommandona.

## Serverläge

Skapa en `sites.json` i projektmappen. Använd en webbplats som du har rätt att hämta.
Med inställningarna nedan hämtar crawlern högst tio sidor.

```json
[
  {
    "name": "Första körningen",
    "start_url": "https://example.org/",
    "max_pages": 10,
    "save_format": ".md"
  }
]
```

Byt ut startadressen. Se [sites.example.json](../sites.example.json) för fler mallar.
Kör sedan följande i PowerShell:

```powershell
.\.venv\Scripts\python.exe ultimate-web-crawler.py --config sites.json --output crawl_output
```

Utan `--output` sparas körningarna i `server_data/`. Varje sajt får en egen undermapp.
Vid namnkrock används ett ledigt nummersuffix. Högst tre sajter körs samtidigt.
`--webhook` eller miljövariabeln `WEBHOOK_URL` kan ange en mottagare för slutnotisen.

Serverläget kan köras utan grafiskt gränssnitt. Exitkod **0** betyder att alla sajter
lyckades, **1** att minst en körning var partiell eller misslyckad eller hade ogiltig
konfiguration och **2** att konfigurationsfilen inte gick att läsa eller använda.
Okända nycklar ger en varning. En post med ogiltig startadress hoppas över.

### Inloggning i serverläge (`cookie_file`)

Interaktiv inloggning kräver GUI. För schemalagda inloggade intranätskörningar behövs `"headless_mode": "login_then_headless"`, `"cookie_file": "C:/secure/cookies.json"` och `"allow_private_hosts": true` när målet ligger på en privat adress. Kör först en inloggad crawl i GUI:t (samma `cookie_file` i mallen) – sessionscookies sparas då i filen. Serverläget läser filen och kontrollerar att sessionen fortfarande gäller; har den gått ut avbryts körningen med ett tydligt fel (och exit-kod 1). **Cookie-filen motsvarar ett lösenord** – lägg den i en skyddad mapp. Efter 5 sessionsfel i rad avbryts crawlen i stället för att fortsätta utan inloggning.

Endast cookies som gäller startvärden eller tillåtna dokument-/sidvärdar sparas och överförs till aiohttp. Domäncookies kan gälla en föräldradomän och behåller sitt omfång; värdbundna cookies breddas inte till underdomäner. Identitetsleverantörens övriga cookies får finnas i minnet under GUI-körningen men sparas inte på disk. Förnya inloggningen via GUI om sajtens egna cookies inte längre räcker. SSO-värdar kan användas vid interaktiv inloggning; vanlig crawl följer däremot inte otillåtna navigationsmål vid `strict_domain=true`.

Cookie-filen skrivs atomärt. På Windows skapas den med en skyddad ACL för filägaren och SYSTEM; på POSIX används `0600` från skapandet. Den är fortfarande okrypterad: skydda även lagringsplats och säkerhetskopior. Ett skrivfel lämnar föregående fil intakt.

GUI-mallar bevarar alla kända avancerade nycklar; synliga GUI-val kan ändras ovanpå mallen. Vald mall återställs från `sites.json` efter omstart. Byte av origin (schema, värd eller port) i startadressen kopplar bort mallens avancerade inställningar, så exempelvis cookies och domänundantag inte följer med till en annan sajt. Sökvägen kan ändras inom samma origin. Mallens `output_dir` respekteras om det anges, och valet Ingen text/No text sparas oberoende av språk.

## Avancerade inställningar

### Skrivbordsprogrammet

**Tillgänglighet.** Skrivbordsprogrammet är inte anpassat för skärmläsare. Tk-gränssnittet ger inga namn eller roller till Windows hjälpmedel. Serverläget kan köras i en terminal med skärmläsare. Skrivbordsprogrammet följer skärmens skalning och kan användas med tangentbordet.

| Tangent | Funktion |
|---|---|
| Tab / Skift+Tab | Flyttar mellan alla synliga reglage i läsordning. En fokusram visar var du är. |
| Mellanslag / Enter | Aktiverar knappar och kryssrutor och slår på eller av Mörkt läge. |
| Pil vänster/höger | Byter flik och språk. |
| Pil upp/ned | Stegar i menyerna. Alt+Pil ned öppnar menyn. |
| Enter i resultatlistan | Öppnar markerad sida. |
| Ctrl+C i resultatlistan | Kopierar sidans adress. |
| Ctrl+Plus / Ctrl+Minus / Ctrl+0 | Ökar, minskar eller återställer textstorleken. |
| Menytangenten / Skift+F10 | Öppnar snabbmenyn i resultatlistan och loggen, som också nås med högerklick. |
| Ctrl+A i loggen | Markerar hela loggen. |
| Esc | Stänger verktygstips och Hjälp. |

**Textstorlek:** Välj **Text 100 %** till **Text 200 %** i översta raden. När fönstret blir för smalt för den vanliga layouten visas inställningarna i en kolumn. Om innehållet ändå inte får plats visas rullningslister, och kontrollen som har fokus rullas fram.

**Loggen** följer med nya rader så länge den senaste raden syns. Rulla upp för att läsa en äldre rad, så blir loggen kvar där.

**Felaktiga värden:** Om ett fält har ett ogiltigt värde när du väljer **Starta** säger meddelandet vilket fält det gäller och vad som är tillåtet. Sedan hamnar markören i fältet. Fördröjning kan skrivas med decimalkomma eller decimalpunkt.

Textstorleken sparas till nästa gång. Vid förstoring rullas den fokuserade kontrollen fram. I ett litet fönster kan hela innehållet rullas så att resultat och logg går att nå. Dra i delaren mellan resultat och logg för att ändra fördelningen. Loggen är skrivskyddad, men raderna kan markeras och kopieras. Målen och beslutet finns i [DESIGNPLAN-WCAG.md](../DESIGNPLAN-WCAG.md).

### Mallar / Templates

Lägg en `sites.json` i samma mapp som `ultimate-web-crawler.py` (eller `.exe`-filen). En **📋 Mall**-dropdown visas automatiskt i GUI:t. Välj en mall och klicka Starta – inställningarna fylls i och avancerade nycklar följer med. Sparmappen hämtas från `output_dir` eller sätts till en undermapp baserad på mallens namn (t.ex. `crawl_output/skolverket`).

Samma `sites.json` fungerar i [serverläge](#serverläge) med `--config`.

### GUI-inställningar

`Besökta` omfattar både `Sidor` och `Cachade`. Konverteringskön visas separat medan arbetet pågår. Om du stänger fönstret under en körning får du välja om körningen ska stoppas och fönstret stängas när rapporten är klar.

Slutstatusen visar **Lyckad körning** i grönt, **Partiell körning** i bärnstensfärg eller **Misslyckad körning** i rött. Räknare och varningar i sammanfattningen följer valt språk. **Öppna rapport** blir tillgänglig när rapporten har skrivits och öppnar filen i systemets standardprogram. Normalt är det `crawl_report.json` i körningens utmapp. Om ett tidigt fel rapporteras separat öppnas i stället filen i `reports/`. Tekniska felmeddelanden finns i rapporten och loggen.

**Grundinställningar / Basic Settings:**

| Inställning | Beskrivning |
|---|---|
| **Startadress / Start URL** | Komplett URL inklusive `https://` |
| **Fördröjning / Delay** | Sekunder mellan förfrågningar per domän (standard: 0.5 s) |
| **Max sidor / Max pages** | `0` = crawla hela sajten |
| **Max djup / Max depth** | Länknivåer från startsidan (`0` = obegränsat) |
| **Samtidighet / Concurrency** | Antal parallella sidor (standard: 10) |
| **Filformat / File Format** | Se tabellen "Utdataformat" nedan |
| **Ladda ner dokument** | Sparar PDF, DOCX m.m. i undermappen `dokument/` |
| **Konvertera dokument till Markdown** | Extraherar text ur dokument och sparar som `.md` med käll-URL |
| **Körläge / Run Mode** | Se tabellen "Körlägen" nedan |
| **Full omcrawl** (Avancerat) | Ignorerar cachen och hämtar allt på nytt |
| **Mapp / Folder** | Katalog för alla sparade filer |

**Utdataformat / File Formats:**

| Format | Beskrivning |
|---|---|
| **.json** | Strukturerad data med rubriker, chunks och metadata – rekommenderas för vektordatabaser. |
| **.md** | Markdown med käll-URL i brödtexten – bra för RAG-system och LLM-läsning. Standardval. |
| **.txt** | Ren text. |
| **Ingen text / No text** | Crawlar och laddar enbart ned dokument, sparar ingen sidtext. |

**Körlägen / Run Modes:**

| Svenska | English | Beskrivning |
|---|---|---|
| **Standard (ingen inloggning)** | Standard (no login) | Kör i bakgrunden utan synligt fönster. För sajter utan inloggning. |
| **Logga in först, sen automatiskt** | Log in first, then automatic | Öppnar synlig webbläsare för manuell inloggning, kör sedan i bakgrunden. |
| **Synlig webbläsare (felsökning)** | Visible browser (debugging) | Visar webbläsarfönstret. Bra för att förstå vad som händer. |

**Avancerat / Advanced:**

| Inställning | Beskrivning |
|---|---|
| **Hybrid-motor** | Väljer automatiskt aiohttp eller Playwright per sida |
| **Trafilatura** | Aktiverar AI-optimerad textextraktion |
| **Sitemap.xml** | Förladdas rekursivt, inklusive gzip-komprimerade sitemaps |
| **robots.txt** | Respekterar crawling-regler och Crawl-Delay |
| **Strikt Domän** | Tvingar crawlern att stanna på exakt angiven domän |
| **Uteslut ord i URL** | Kommaseparerad lista – URL:er som matchar hoppas över |
| **Kräv ord i URL (något av)** | Crawlern besöker bara sidor vars URL innehåller minst ett av dessa ord (ELLER-logik) |

**PII-Tvätt / PII Wash (GDPR):**

| Inställning | Vad som maskeras |
|---|---|
| **Radera E-post** | kontakt@myndighet.se → `[E-POST]` |
| **Radera Telefonnummer** | Svenska format inkl. landskod och parenteser → `[TELEFON]` |
| **Radera Personnummer** | Vanliga PNR och samordningsnummer → `[PERSONNUMMER]` |
| **Radera IP-adresser** | IPv4-adresser → `[IP-ADRESS]` |

Personnummer utan tydlig etikett kontrolleras mot datum och Luhn-kontrollsiffra; även samordningsnummer stöds. Tydlig telefonkontext (`tel:`, `Telefon:`, `Mobil:` m.fl.) bevaras när `remove_phone=false`. En sifferföljd kan vara både ett giltigt personnummer och ett telefonnummer: utan sammanhang prioriteras personnummermaskering. IPv4-tvätten kontrollerar oktetterna och bevarar tydligt märkta avsnitts-/versionsnummer, t.ex. `Avsnitt 1.2.3.4`. Tvätten är heuristisk och garanterar inte att alla personuppgifter identifieras.

> 💡 **Tips:** Dubbelklicka på valfri rad i *Live Data*-tabellen för att öppna URL:en i din webbläsare.

### Avancerade nycklar i `sites.json`

| Nyckel | Standard | Beskrivning |
|---|---|---|
| `cookie_file` | – | Sökväg där sessionscookies sparas/läses (se ovan) |
| `user_agent` | `UltimateWebCrawler/7.1 (+repo-URL)` | Crawlern identifierar sig ärligt; sätt egen vid behov |
| `allowed_domains` | `[]` | Extra domäner som räknas som "samma sajt" vid `strict_domain` |
| `include_subdomains` | `false` | Tillåt underdomäner till startdomänen |
| `max_page_mb` / `max_download_mb` | `10` / `100` | Storleksgränser i MiB för sidor respektive dokument. Vid automatisk browser-rendering begränsas både sidans samlade avkodade svarskroppar (HTML/JS/fetch m.m.) och dess serialiserade DOM; dokument strömmas separat med dokumentgränsen |
| `spa_wait_seconds` | `3` | Extra väntetid efter `DOMContentLoaded`, 0–60 sekunder. `0` stänger av väntan |
| `spa_ready_selector` | tom | Valfri selektor för ett synligt element som visar att en sida är klar. Aktiverar rendering för sajtens HTML-sidor även i hybridläge |
| `max_path_segments` / `max_query_params` | `15` / `5` | Gränser mot crawl-fällor |
| `keep_query_params` / `ignore_query_params` | `[]` | Styr vilka exakta parameternamn som undantas från respektive läggs till i standardlistan för deduplicering. `ref`, `source` och `state` behålls som standard |
| `boost_words` / `penalty_words` | se kod | Ord i URL som höjer/sänker prioritet |
| `keep_role_emails` | `false` | Behåll `kontakt@`-liknande funktionsbrevlådor vid e-posttvätt |
| `ignore_https_errors` | `false` | Ignorera certifikatfel i webbläsaren (intranät med egen CA) |
| `allow_private_hosts` | `false` i serverläge, automatiskt i GUI | `true` tillåter privata nätverksmål. Sätt det uttryckligen för schemalagda intranätkörningar. `false` kräver publika mål |
| `incremental` | `true` | `false` = full omcrawl |
| `session_check_url` | startsidan | Åtkomlig skyddad resurs för kontroll av inloggning; ange denna om startsidan är publik |
| `languages` | `[]` (alla) | Bara dessa språk, t.ex. `["sv"]` |
| `respect_canonical` | `true` | Spara inte sidor som pekar ut en annan URL som original |
| `dedupe_content` | `true` | Spara bara en kopia av sidor med identiskt innehåll |
| `use_sitemap_lastmod` | `true` | Hoppa över sidor som sitemapen säger är oförändrade |
| `sitemap_lastmod_max_age_days` | `14` | Kontrollera poster äldre än så ändå |
| `export_jsonl` | `true` | Skriv `chunks.jsonl` (en chunk per rad) |
| `quarantine_flagged_chunks` | `true` | Isolera flaggade chunks (t.ex. prompt-injektion) i `chunks_quarantine.jsonl` i stället för `chunks.jsonl` |
| `ocr` / `ocr_languages` / `ocr_max_pages` | `true` / `swe+eng` / `50` | OCR av skannade PDF:er (kräver Tesseract) |
| `conversion_workers` | `2` | Samtidiga dokumentprocesser per sajt; oberoende av hämtningarnas `concurrency` |
| `conversion_queue_size` | `8` | Högsta antal väntande konverteringsjobb; körande processer räknas separat |
| `conversion_timeout` | `180` | Sekunder per workerprocess, inklusive import/start; öka för stora skannade dokument. Timeout och avbrott avslutar också barnprocesser |
| `conversion_memory_mb` | `1024` | Minnesgräns i MiB per konverteringsjobb, minst `64`. Windows begränsar processens och hela processträdets sammanlagda committed-minne via Job Object. POSIX använder `RLIMIT_AS` per process; barn ärver gränsen, men den är ingen gemensam budget för trädet. Samtidiga jobb har egna budgetar |
| `allow_libreoffice` | `false` | Tillåt nya LibreOffice-konverteringar för betrodda dokument. Original hämtas även när valet är avstängt |
| `generation_keep` | `0` | Valbar historikstädning: `0` behåller alla; t.ex. `10` behåller aktuell generation och de nio senaste äldre, plus generationer inom respiten |
| `changes_history_max_entries` | `0` | Valfri gräns för antalet händelser i `changes.jsonl`. `0` behåller hela historiken. Den senaste körningens samtliga nya händelser sparas även om de överskrider gränsen |
| `generation_min_age_hours` | `24` | Respit från när en generation först noterats som inaktuell. Måste vara längre än längsta RAG-inläsning som pinnar en generation |

Behåll `ultimate-web-crawler.py`, `crawler_runtime.py`, `conversion_worker.py`, `document_conversion.py`, `pdf_scan_tables.py`, `pdf_dense_currency.py`, `pdf_source_corrections.py` och `crawler_text.py` i samma mapp när du kör källkoden. Workerstart stöder också `pythonw` och en intern ingång för paketerade program. Avancerade nycklar fungerar i CLI och följer med när en GUI-mall väljs. PDF-workers laddar bara PDF-biblioteket, utan GUI, HTML-/nätverksbibliotek eller Office-parsers.

Vid rendering utan selektor väntar crawlern minst 0,75 sekunder inom tidsbudgeten. Den fortsätter när huvudtexten varit stabil i 0,25 sekunder och pågående fetch/XHR-anrop har avslutats. När budgeten löper ut används den senaste tillgängliga sidtexten. Om texten saknas, eller om en angiven selektor fortfarande inte är synlig, rapporteras sidan under `spa_timeouts` och tidigare källtext behålls. En angiven selektor förhindrar även att ett gammalt HTML-skal återanvänds via sitemap-lastmod eller villkorlig GET.

Saknade `delay`, `max_pages`, `max_depth` och samtidighetsvärden får gemensamma standardvärden (`0.5`, `0`, `0`, `10`/`2`). Numeriska strängar normaliseras; negativa värden, NaN/oändlighet, ogiltiga heltal och felaktiga list-/booltyper avvisas före crawl. Både `exclude_keywords` och `require_keywords` matchas utan hänsyn till versaler i GUI och CLI.

### Källor med innehållsstyrande URL-parametrar

Olika värden för `ref`, `source` och `state` ger som standard separata cacheposter och filnamn för både sidor och dokument. Om en parameter bara används för spårning på en viss sajt kan den anges i sajtens `ignore_query_params` för att behålla den tidigare dedupliceringen.

Vid uppgradering används sidcachens sparade begärda adress för att flytta kända varianter till sina egna nycklar och filnamn. Sidans källhänvisning och befintliga chunk-ID:n följer med när samma innehåll återanvänds. Ett filnamnsbyte anges med `previous_file` i `changes.jsonl`. Migreringen sker i nästa generation och omfattar även kända sidor som en begränsad körning inte hinner besöka.

Äldre dokumentposter saknar den begärda adressen. När den registrerade dokumentadressen kontrolleras igen hämtas den utan tidigare HTTP-validatorer. En bekräftad 404/410 tar bort den gamla posten och nya dokumentvarianter får egna källhänvisningar och chunk-ID:n när de hämtas. Läs den nya generationens filregister och `chunks.jsonl` tillsammans vid inläsning. Ändringsloggen fortsätter att maskera frågesträngar.

## Utdata

```text
crawl_output/
├── current.json                     # Atomisk ingång till den publicerade generationen
├── .generations/<id>/               # Komplett generation: texter, original, index, manifest, chunks och rapport
├── .working/<id>/                   # Pågående körning; aldrig en källa för inläsning
├── texter/                          # En fil per skrapad godkänd sida
│   ├── sidnamn_a1b2c3d4.md          # eller .json / .txt
│   └── dokumentnamn_a1b2c3d4_doc.md # konverterade dokument
├── texter_karantan/                 # Isolerade textfiler från flaggade sidor (t.ex. prompt-injektion)
├── dokument/                        # Nedladdade PDF, DOCX, XLSX m.m.
├── logs/
│   └── crawl_YYYYMMDD_HHMMSS.log
├── index.csv                        # Översikt: URL, titel, datum, filnamn (endast befintliga filer)
├── chunks.jsonl                     # Rena chunks, en per rad – färdig att läsa in i en vektordatabas
├── chunks_quarantine.jsonl          # Isolerade chunks från flaggade sidor (t.ex. prompt-injektion)
├── changes.jsonl                    # Logg över nytt/ändrat/borttaget (läggs till vid varje körning)
├── crawl_report.json                # Luckor, fel, varningar och sidor som inte nåtts sedan sist
├── manifest_domännamn.json          # Kopplar dokument till ursprungssida
└── domännamn_cache.db               # SQLite-cache för inkrementell crawling
```

### Sammanhängande inläsning via `current.json`

Crawlern förbereder hela leveransen i en arbetsmapp och byter `current.json` först när konverteringar, index, manifest, korpus och rapport är klara. En körning med `failed`, ett exportfel eller avbrott före commit lämnar den tidigare generationen och dess cache intakta. En färdig körning med enstaka hämtnings-/konverteringsfel kan publicera en ren `partial`-generation med felinformationen i rapporten; CLI ger fortfarande exit-kod 1. En påbörjad commit slutförs även om väntan avbryts.

En otillgänglig robots.txt stoppar nya hämtningar på den origin som berörs, men tar inte bort sparat innehåll. Robots-hämtningen gör upp till två omförsök. Publika 401/403-svar behandlas som hämtningsfel och behåller tidigare källor. Om mer än hälften av föregående generations rena källfiler försvinner utan belägg stoppas publiceringen och `publication_blocked` förklarar bortfallet. Verkliga HTTP 404/410, uttryckliga urvals-/bearbetningsändringar, karantän och bekräftade dubblettbeslut kan förklara borttagningarna. En soft-404 eller kort 200-sida räcker inte som belägg för massborttagning. En bekräftat tömd sajt kan alltså publicera en tom korpus.

Bearbetningsprofilen omfattar PII-tvätt, format, karantänpolicy och extraktor. URL-/språkurval och hämtmotor ligger utanför profilen; urvalet kontrolleras separat vid export, även för obesökta källor. Vid uppgradering från äldre sid-/dokumentprofiler utan profilbeskrivning kan en begränsad första körning stoppas av bortfallsspärren. Kör då en gång med `max_pages=0` och `max_depth=0` så att källorna kan omprövas. Den tidigare leveransen finns kvar under tiden.

Vid inläsning i en RAG-tjänst: **läs `current.json` en gång och använd samma generationsmapp för hela inläsningen**. Exempel:

```python
from pathlib import Path
import json

root = Path("crawl_output")
pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
generation = root / pointer["generation"]
if pointer["corpus"] is not None:
    corpus_path = generation / pointer["corpus"]
    # Läs chunks.jsonl, index.csv och manifest från denna generationsmapp.
```

`generation.json` i mappen beskriver samma leverans. `artifacts` listar leveransfiler, utan cache och utgångna textarkiv. `corpus` pekar på rena chunks; `quarantine` är separat och ska inte matas in i den vanliga korpusen. Vid `export_jsonl=false` är `corpus` tomt och gamla JSONL-filer tas bort från den nya leveransen.

De tidigare toppnivåvägarna (`texter/`, `chunks.jsonl`, `index.csv` och manifest) finns kvar som kompatibilitetskopior. De byts **inte** gemensamt atomärt; en integration som använder dem måste flyttas till generationsmappen för att få den nya garantin. Kopiorna delar inte hårdlänkar med generationen och kan inte ändra dess innehåll. Ett misslyckat kompatibilitetsbyte ger felstatus, medan den kompletta publicerade generationen fortfarande finns kvar.

Toppnivåns `crawl_report.json` beskriver senaste försöket, med `publication_committed` och `published_generation`. Rapporten inne i generationen hör till den leveransen. Cache återställs från den publicerade generationen vid nästa körning. Utmappen låses mot samtidiga skrivare, och övergivna arbetsmappar städas vid återstart. Tidigare generationer behålls som standard för pinnade läsare. För schemalagd serverdrift kan t.ex. `generation_keep=10` och `generation_min_age_hours=24` aktivera städning efter lyckad commit; välj en respit längre än läsarnas maximala inläsningstid. De N senaste och aktuell generation skyddas alltid, och respiten kan tillfälligt ge fler än N mappar. Städningen rapporteras i `generation_cleanup`; fel där visas separat i `generation_cleanup_failures` och rullar inte tillbaka en komplett leverans. Textarkiv och loggar omfattas inte av denna städning. Utgångna textarkiv ska inte ingå i RAG-inläsningen.

`crawl_report.json` innehåller `status`: `success` när körningen genomförts utan körfel, `partial` när minst en resurs hämtats/kontrollerats men andra misslyckats eller körningen stoppats, och `failed` vid ett fatalt fel eller när inget lyckats och körningen har fel. Exportfel är alltid fatala. Avsiktliga gränser som `max_pages`, vanliga borttagningar och robots-regler som utesluter innehåll är inte körfel. `completed_naturally` visar separat om kön tömdes. Rapporten skrivs även när inloggningen misslyckas innan crawlen startat.

Robots-regler hämtas separat per scheme, värd och port, och kontrolleras före startsidan och varje redirect. En robots.txt med 4xx tillåter hämtning; 5xx eller nätverksfel stoppar hämtningar på berörd origin och registreras under `robots_unreachable`, enligt [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3.1). `robots_denied` visar adresser som stoppats vid kontrollen. Sitemap-inläsning fungerar även när `respect_robots=false`.

Browser-fallback för dokument används bara i `login_then_headless` efter 401/403 eller HTML-svar. Chromium strömmar bilagor och inline-dokument under `max_download_mb`; för stora överföringar avbryts och temporära filer tas bort. Vanliga browser-downloads är avstängda så att de inte kan kringgå gränsen.

### JSON-format per sida:

```json
{
  "title": "Kontakta oss - Skolverket",
  "url": "https://www.skolverket.se/kontakt",
  "crawled_at": "2026-04-03T12:00:00",
  "author": "",
  "published_date": "",
  "modified_date": "2026-03-15",
  "language": "sv",
  "og_type": "",
  "description": "Så når du Skolverket",
  "keywords": ["kontakt"],
  "flags": [],
  "plain_text": "## Ring oss\nNi når oss på [TELEFON]...",
  "chunks": [
    {
      "heading": "Ring oss",
      "heading_path": "Ring oss",
      "context": "Kontakta oss > Ring oss",
      "content": "Ni når oss på [TELEFON]. Vår e-post är [E-POST].",
      "chunk_index": 1,
      "total_chunks": 3,
      "url": "https://www.skolverket.se/kontakt"
    }
  ]
}
```

### Markdown-format per sida:

```markdown
# Kontakta oss - Skolverket

**Källa:** https://www.skolverket.se/kontakt
**Senast ändrad:** 2026-03-15
**Hämtad:** 2026-04-03
**Språk:** sv

---

## Ring oss
Ni når oss på [TELEFON]. Vår e-post är [E-POST].

---

**Källa:** https://www.skolverket.se/kontakt
```

### Konverterat dokument (Markdown):

```markdown
# Budget 2025

**Källa:** https://intranat.kommun.se/ekonomi
**Dokument-URL:** https://intranat.kommun.se/download/budget-2025.pdf
**Filnamn:** budget-2025.pdf
**Filtyp:** PDF

---

## Sida 1

[dokumenttext...]

---

**Källa:** https://intranat.kommun.se/ekonomi
**Dokument-URL:** https://intranat.kommun.se/download/budget-2025.pdf
```

### chunks.jsonl för RAG-tjänster och vektordatabaser

`chunks.jsonl` byggs efter varje körning av *alla* godkända sidor och dokument (även oförändrade), så filen är alltid komplett och ren för inläsning. Både råfiler och chunks från sidor med misstänkt prompt-injektion eller andra säkerhetsflaggor isoleras i mappen `texter_karantan/` respektive filen `chunks_quarantine.jsonl`. Varje rad:

```json
{"id": "a1b2c3d4e5-1", "url": "https://…/avgifter", "title": "Avgifter", "source_type": "page",
 "referer_url": "", "language": "sv", "modified_date": "2026-03-15", "crawled_at": "2026-04-03",
 "heading": "Förskola", "heading_path": "Avgifter > Förskola", "context": "Avgifter > Förskola",
 "content": "…", "chunk_index": 1, "total_chunks": 4, "flags": []}
```

För dokument är `source_type` `document`, `url` dokumentets adress och `referer_url` sidan som länkade till det. För PDF-sidor inkluderas även `page_number` (t.ex. `14`) och `page_url` (med djuplänk t.ex. `https://…/policy.pdf#page=14`); PowerPoint-bilder får `slide_number`. Sammanslagna sektioner får `heading_paths` med de ursprungliga rubrikvägarna. `id` består av källans hash och chunkens ordningsnummer: källans del är stabil, men ändrad chunkning kan flytta innehåll mellan ID:n. Ersätt därför källans samtliga chunks vid uppdatering och rensa gamla ID:n via `changes.jsonl`/filregistret.

I PDF-filens Markdown-export står det fysiska sidnumret och en klickbar sidlänk i ett eget stycke i brödtexten. Stycket upprepas om en PDF-sida delas i flera avsnitt. Det gör siduppgiften tillgänglig när en RAG-tjänst läser brödtexten och filtrerar bort rubriken. I `chunks.jsonl` ligger uppgifterna i `page_number` och `page_url`. Oförändrade PDF-original kan konverteras om från cachen vid nästa kontroll efter uppgradering.

Extraherade tabeller från webbsidor och konverterade dokument följs av ett vanligt textstycke per datarad i källfilerna och i `chunks.jsonl`. Kolumnrubriken upprepas för varje ifylld cell, exempelvis `Tabellrad. Biljettyp: Morgonsim senior. Pris i kronor: 85.`. En kolumn utan rubrik anges med sitt nummer, till exempel `Kolumn 2`. Den ursprungliga tabellen finns också kvar. Textstyckena gör radkopplingen läsbar även när en RAG-tjänst tappar tabellens celler vid inläsningen. Efter uppgradering behöver webbsidor hämtas på nytt. Oförändrade dokument kan konverteras om från det sparade originalet.

Chunkexporten återanvänder rader från oförändrade, registrerade källfiler i föregående generation. Om hela urvalet är oförändrat kan generationerna dela samma JSONL-fil via hårdlänk. När en källa ändras skrivs en ny komplett JSONL-fil. Om exportindexet saknas eller är ogiltigt byggs exporten från källfilerna. Rapportens `chunk_export` visar antalet källfiler, antalet återanvända, om hela JSONL-exporten återanvändes och hur många byte den rena exporten och karantänexporten omfattar tillsammans.

När `changes_history_max_entries` är större än noll sparas de senaste historiska händelserna som ryms efter att de nya händelserna lagts till. En äldre körning kan därför finnas kvar delvis. Om den senast inlästa körningen saknas eller bara finns kvar delvis, synka om mot den pinnade generationens fullständiga korpus och filregister. `changes_history` i rapporten visar gränsen, filstorleken och om historiken kortades vid körningen. Tidigare generationer behåller sina loggar enligt den separata generationsstädningen.

### OCR för skannade PDF:er

Många kommunala PDF:er är inskannade bilder utan text. För att läsa dem:

1. Installera **Tesseract** (Windows: installationsprogram från UB Mannheim; se till att *Swedish* väljs under språkdata) och lägg det i `PATH`.
2. Crawlern OCR-läser sidor utan textlager och kan komplettera sidor där större bildytor saknar täckande text. De delar gränsen `ocr_max_pages` (standard 50) med omläsning av täta tabeller. Ett försök räknas även om det misslyckas.

Saknas Tesseract syns skannade dokument under `conversion_failures` i `crawl_report.json` med orsaken. OCR-text kan innehålla fel och märks därför i dokumentet.

---

## 🔒 Säkerhet och dataskydd

- **Åtkomststyrning:** Crawlar du ett intranät med en användares inloggning hamnar *allt* den användaren ser i utdata – och därmed potentiellt i RAG-tjänsten för alla som använder den. Använd ett dedikerat konto med minimala rättigheter och granska vad som matas in.
- **Nätverk:** aiohttp kontrollerar IP-literaler och DNS-resultat även vid redirects. I publikt läge kontrollerar Chromiums SOCKS5-proxy varje TCP-mål och ansluter till den granskade IP-adressen. Skyddet omfattar sidrendering, redirects, popup-fönster, interaktiv inloggning och webbläsarens dokumenthämtningar. Implicit loopback-bypass, QUIC och WebRTC-trafik utanför proxyn stängs av. Service Workers blockeras och browser-dokument strömmas via CDP med en storleksgräns. Proxyfel stoppar anslutningen.
- **Publika IP-mål:** Samma kontroll gäller för literaler, DNS-svar och proxyanslutningar. Den spärrar NAT64-prefixen `64:ff9b::/96` och `64:ff9b:1::/48`, 6to4 och Teredo. IPv4-mappade adresser kontrolleras som IPv4. Ett nät med ett eget översättningsprefix behöver hantera det prefixet i sin nätverkspolicy. [RFC 6052](https://www.rfc-editor.org/rfc/rfc6052.html) · [RFC 8215](https://www.rfc-editor.org/rfc/rfc8215.html)
- **Intranätläge:** Serverläget kräver `allow_private_hosts=true` för att nå privata adresser. I GUI:t kan `localhost`, loopback, privata IPv4-adresser enligt RFC 1918 och IPv6 ULA aktivera läget automatiskt. För ett DNS-namn måste alla uppslagna adresser tillhöra dessa grupper. `allow_private_hosts=false` låser även GUI:t till publik policy. Tillstånd loggas som varning och beslutet sparas i `crawl_report.json` under `network_policy`.
- **Begränsningar:** Gränser för sidstorlek, nedladdningsstorlek och dekomprimerade sitemaps. XML med entiteter avvisas.
- **HTTPS:** Certifikatfel ignoreras bara om `ignore_https_errors` uttryckligen sätts.
- **Cookie-filen** (`cookie_file`) motsvarar ett lösenord. Den läggs aldrig i loggar och bör ligga i en skyddad mapp.
- **URL:er i diagnostik** behåller sökvägen men utelämnar hela frågesträngen, fragmentet och användaruppgifter i HTTP(S)-adresser i crawlerns loggar, `crawl_report.json` och `changes.jsonl`. Maskeringen gäller även felmeddelanden. Använd `file` i ändringsloggen för att skilja källor som bara skiljer sig i frågesträngen. Hämtning, cache, filregister och källhänvisningarna i texterna kan fortfarande innehålla de fullständiga adresserna. Nästa publicerade generation maskerar även äldre ändringsposter, oavsett om körningen har nya ändringar. Tidigare generationer och loggfiler kan fortfarande innehålla äldre adresser.
- **Utdata är opålitlig indata** till en LLM. Injektionskontrollen är heuristisk och kan både missa instruktioner och flagga legitim text. Matchande källor rapporteras under `possible_prompt_injection` i `crawl_report.json` och exporteras till karantänen när den är aktiverad. En RAG-tjänst bör behandla även oflaggade källor som opålitlig indata.
- **Dokumentparsning** (PDF/Office/OCR) körs i separata processer med tidsgräns och egna indatakopior. Workern och LibreOffice får en tillåten lista med system-, språk- och sökvägsvariabler, inklusive `TESSDATA_PREFIX`. Hemkatalog, appdata, temporär katalog och arbetskatalog styrs till jobbet. Workern spärrar Python-anrop för nätverk. Native bibliotek och LibreOffice kan fortfarande nå nätverk och filer enligt processkontots OS-behörigheter. När LibreOffice väljs till behöver betrodda dokument, en aktuell installation och driftsmiljöns åtkomstregler ingå i skyddet. [LibreOffices säkerhetsinformation](https://www.libreoffice.org/security/)

Vid strikt domänkontroll stoppas otillåtna crawlredirects före anslutning, även före hämtning av målets robots.txt. Ett externt 404/410-svar får inte användas som bevis för att radera en lokal källa. Överskriden browser- eller konverteringsgräns rapporteras som ett körfel; tidigare publicerad källtext med oförändrad bearbetningsprofil behålls. Browsergränserna begränsar överföring och DOM-export, men är ingen fullständig minnes-/CPU-sandbox för JavaScript eller den interaktiva inloggningen.

## ⚖️ Etik och Ansvar

Detta verktyg är utvecklat för laglig och etisk datainsamling. Användaren ansvarar för att:

- Följa webbplatsens användarvillkor.
- Inte överbelasta servrar – använd den inbyggda fördröjningsfunktionen.
- Respektera de begränsningar som anges i `robots.txt`. Crawlern identifierar sig som `UltimateWebCrawler` och följer `*`/`$`-regler när `protego` är installerat.
- Säkerställa att insamlad data hanteras i enlighet med GDPR och tillämplig lagstiftning.
