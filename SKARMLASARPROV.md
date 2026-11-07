# Skärmläsarprov för skrivbordsappen

> **Vilande sedan 2026-11-06.** Enligt [beslutet om ambitionsnivå](DESIGNPLAN-WCAG.md#beslut-om-ambitionsnivå-2026-11-06) anpassas skrivbordsappen inte för skärmläsare. Förhandsresultaten nedan är belägg för den begränsningen. Hörprovet görs bara om beslutet omprövas, till exempel när Python levereras med en Tk-version som har stöd för skärmläsare.

Det här är det avgränsade teknikprovet i etapp 1 i [DESIGNPLAN-WCAG.md](DESIGNPLAN-WCAG.md), del 2. Provet avgör om appens nuvarande kontroller kan bära arbetsflödet med skärmläsare, eller om PySide6 ska få en liten prototyp innan något byggs om.

Provet består av sex delar och körs i två varianter:

| Variant | Vad den visar |
| --- | --- |
| `app` | Den riktiga appen med CustomTkinter-kontroller. |
| `ttk` | Samma sex delar byggda med vanliga ttk-kontroller, som jämförelse. |

Ingen sida hämtas. Provadresserna använder den reserverade toppdomänen `.example`, och Starta är frånkopplad från crawlern. F9 simulerar en körning med tre resultatrader, loggrader och ett slutmeddelande. Provet läser och skriver inte dina sparade inställningar.

## Miljö

| Del | Version |
| --- | --- |
| Windows | Windows 11 (10.0.26300) |
| Python-miljö | Projektets `.venv`: Python 3.14.10, CustomTkinter 6.0.0, Tk 9.0.4 |
| Windows Skärmläsaren | Inbyggd. Starta och stäng med Ctrl+Windows+Enter. |
| NVDA | Var inte installerad 2026-11-06. Installera från [nvaccess.org](https://www.nvaccess.org/download/) och anteckna versionen. |
| Accessibility Insights for Windows | Valfritt. Visar även MSAA-värden (`LegacyIAccessible`), som `tools/uia_dump.ps1` inte visar. [Hämta från Microsoft](https://accessibilityinsights.io/downloads/) |

## Förhandsresultat från automatiska prov (2026-11-06)

De här resultaten togs fram utan skärmläsare. De visar vad Tk lämnar till Windows, men inte exakt vad en skärmläsare säger. Hörprovet nedan avgör det.

| Prov | `app` (CustomTkinter) | `ttk` |
| --- | --- | --- |
| Tabbordning med Tab | 8 stopp: adressfältet, fyra sifferfält, mappfältet, tabellen och loggen. Nås inte: Hjälp, Mörkt läge, språkvalet, flikarna, Filformat, Körläge, båda kryssrutorna, Välj mapp, Starta och Öppna mapp. | 6 stopp. Alla sex delar nås: adressfält, flikar, kryssruta, Starta, tabell och logg. |
| UI Automation | 137 element: 136 `Pane` och fönstret. Bara fönstret har ett namn. Inget element är fokuserbart enligt UI Automation, och inget stöder mönster som `Value`, `Invoke`, `Toggle`, `SelectionItem`, `Table` eller `Text`. | 16 element: 15 `Pane` och fönstret. Samma brist som i `app`. |

Efter tangentbordsåtgärderna senare samma dag når Tab alla 19 viktiga kontroller i `app`, och `--tabbordning` avslutar med "Nås inte med Tab: inga". UI Automation-resultatet är oförändrat.

**Tolkning.** ttk löser tangentbordsdelen, men inte skärmläsardelen. I Tk 9.0.4 får Windows bara namnlösa fönsterrutor från båda varianterna. Skärmläsare får då varken namn, roll, värde eller tillstånd för adressfältet, knapparna, flikarna eller tabellen. Om hörprovet bekräftar detta är hindret genomgående enligt planens beslutsregel. Råfilerna finns i [tools/provresultat](tools/provresultat).

## Förberedelse

Kör kommandona från projektmappen.

```bash
.venv\Scripts\python.exe tools\skarmlasarprov.py app
```

```bash
.venv\Scripts\python.exe tools\skarmlasarprov.py ttk
```

Tabbordningen kan skrivas ut utan skärmläsare:

```bash
.venv\Scripts\python.exe tools\skarmlasarprov.py app --tabbordning
```

UI Automation-trädet för ett öppet provfönster sparas med:

```bash
powershell -ExecutionPolicy Bypass -File tools\uia_dump.ps1 -Titel "skärmläsarprov" -Utfil tools\provresultat\uia_app.txt
```

Inför varje omgång:

1. Starta skärmläsaren.
2. Öppna en provvariant.
3. Klicka en gång i fönstrets titelrad så att fönstret har fokus.
4. Börja provet med Tab.

Kör varje variant med båda skärmläsarna. Anteckna vad som faktiskt läses upp, ordagrant om det går.

## De sex proven

| Prov | Gör så här | Godkänt när |
| --- | --- | --- |
| P1. Adressfält | Tryck Tab tills fokus når adressfältet. Läs aktuellt objekt igen med skärmläsarens kommando, i NVDA med NVDA+Tab. | Skärmläsaren säger namnet **Startadress**, en roll som redigeringsfält och värdet, alltså adressen. |
| P2. Starta | Nå knappen **Starta** med Tab och aktivera den med Mellanslag eller Enter. Om Tab inte når den, prova skärmläsarens egen navigering, till exempel objektnavigering i NVDA. | Skärmläsaren säger **Starta** och rollen knapp, och knappen kan aktiveras med tangentbordet. Aktiveringen syns som raden "Starta aktiverades" i loggen. Provet hämtar aldrig något. |
| P3. Flikval | Nå flikarna och byt till **Avancerat** med piltangenter eller Ctrl+Tab. | Skärmläsaren säger flikens namn, att den är vald och gärna position, till exempel "2 av 2". Innehållet byts till fliken Avancerat. |
| P4. Tabellrad | Tryck F9 och vänta tills tre rader har kommit in. Nå tabellen med Tab och flytta mellan raderna med pil upp och pil ned. | Skärmläsaren läser radens tre celler, helst med kolumnnamnen **Sidtitel**, **Status** och **URL**, och anger vilken rad som är markerad. |
| P5. Logg | Nå loggen med Tab och flytta mellan raderna med piltangenterna. Prova också att skriva ett tecken. | Skärmläsaren läser den aktuella loggraden. Loggen går att markera och kopiera men inte ändra. Anteckna det om `app` låter dig skriva i loggen. |
| P6. Slutmeddelande | Ställ fokus i adressfältet och tryck F9. | Skärmläsaren läser upp slutstatusen, till exempel "Lyckad körning", utan att fokus flyttas från adressfältet. |

Anteckna i varje prov:
- **Godkänd:** det fungerar som beskrivet.
- **Underkänd:** något saknas.
- **Ej provad:** provet gjordes inte.

Skriv vad som saknades: namn, roll, värde, tillstånd, åtkomst med tangentbord eller annonsering.

## Resultat

Fyll i efter provet. Värdena är **Godkänd**, **Underkänd** eller **Ej provad**, med en kort kommentar.

| Prov | `app` + Skärmläsaren | `app` + NVDA | `ttk` + Skärmläsaren | `ttk` + NVDA |
| --- | --- | --- | --- | --- |
| P1. Adressfält | Ej provad | Ej provad | Ej provad | Ej provad |
| P2. Starta | Ej provad | Ej provad | Ej provad | Ej provad |
| P3. Flikval | Ej provad | Ej provad | Ej provad | Ej provad |
| P4. Tabellrad | Ej provad | Ej provad | Ej provad | Ej provad |
| P5. Logg | Ej provad | Ej provad | Ej provad | Ej provad |
| P6. Slutmeddelande | Ej provad | Ej provad | Ej provad | Ej provad |

- **Provad av:**
- **Datum:**
- **Versioner (Windows, NVDA):**
**Uppläsningar och iakttagelser:**

## Beslutsregel

Regeln kommer från etapp 1 i [DESIGNPLAN-WCAG.md](DESIGNPLAN-WCAG.md).

1. **Behåll CustomTkinter** om P1–P6 går igenom i `app` med båda skärmläsarna, eventuellt efter tangentbordsåtgärder.
2. **Byt berörda kontroller mot ttk** om `ttk` går igenom där `app` underkänns.
3. **Ta fram en PySide6-prototyp** om båda varianterna underkänns i namn, roll, värde eller annonsering. Prototypen ska klara samma sex prov innan beslut om ombyggnad.

Förhandsresultaten pekar mot punkt 3. För in utfallet och beslutet i statusraden i planens arbetsordning.
