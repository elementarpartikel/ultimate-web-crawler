# Design- och tillgänglighetsplan för Webbdammsugare Pro

## 1. Vad som behöver lösas

Målet är ett sammanhållet Windowsprogram där startadress, inställningar, resultat och körningslogg har samma typografi och känns som delar av samma arbetsflöde. Hela flödet ska kunna genomföras med tangentbord. Skrivbordsappen anpassas inte för skärmläsare, se [beslutet om ambitionsnivå](#beslut-om-ambitionsnivå-2026-11-06). Den här planen gäller version 7.1 i arbetskopian och skärmbilden som visar huvudfönstret före start.

I skärmbilden är tabellens kolumnrubriker visuellt större och tyngre än panelrubriken **Resultat**. I den övre delen är fältetiketter och knapptexter mindre och tätare. Loggens tomtext ser ut som övrig gränssnittstext, men den faktiska loggen visas med ett annat typsnitt när körningen börjar. Det finns konkreta förklaringar till skillnaderna i den nuvarande implementationen.

| Del | Vad koden anger | Konsekvens och åtgärd |
| --- | --- | --- |
| Inställningar, knappar och flikar | Windowsvarianten av CustomTkinter-temat `blue` anger Roboto 13 och paketet laddar medföljande Roboto-filer. Tk rapporterade Roboto som den faktiska fonten i provet. | Byt appens standardfamilj före skapandet av kontrollerna och låt uttryckliga textroller använda samma familj. |
| Panelrubriker och tomvyer | Panelrubriker använder CustomTkinter med storlek 14 och fet stil. Tomvyer använder samma standardfamilj med storlek 12. | Behåll rubrikerna som överordnad nivå. Ge tomtext och annan viktig vägledning samma läsbarhet som fältetiketterna. |
| Resultatlista | `ttk.Treeview` anger Segoe UI separat. `AppGUI._update_treeview_style` räknar om font, radhöjd och rubrikmarginal när den anropas vid start och temabyte. `AppGUI._build_ui` sätter kolumnbredderna när tabellen skapas. | Åtgärda att tabellens mått ligger kvar när resten av fönstret skalar om och att de räknas från fel skalfaktor. Använd sedan samma familj och textroller i hela fönstret. |
| Körningslogg, Hjälp och verktygstips | Loggen anger Consolas 11. Hjälpen anger Arial 13. Verktygstips använder en vanlig Tk-etikett utan samma uttryckliga typografi. | Använd gränssnittets läsfont även här. Reservera eventuell monospace för en separat teknisk detaljvy där fasta teckenbredder fyller en funktion. |
| Windows titelrad | Operativsystemet ritar fönstrets titelrad. | Behåll Windows egen titelrad och fönsterfunktioner. Målet för programmets innehåll är att visuellt höra ihop med dem. |

**Bekräftade skalningsfel.** Tabellen har två separata fel som båda måste åtgärdas.

1. **Ingen uppdatering vid ändrad skalning.** I arbetskopians Windowsprov ändrades CustomTkinter-rubriken **Resultat** från 21 till 32 pixlar när CustomTkinters egen skalfaktor ökades från 1 till 1,5 ovanpå skärmens grundskala. Resultatlistans kolumnrubrik låg kvar på 18 pixlar. Tabellstilen räknas bara om vid start och temabyte, och kolumnbredderna bara när tabellen skapas.
2. **Fel skalfaktor.** `AppGUI._update_treeview_style` och kolumnbredderna i `AppGUI._build_ui` räknar från fönsterskalningen (`_get_window_scaling`). CustomTkinters text och delarens tjocklek räknar från kontrollernas skalning. Efter `ctk.set_widget_scaling(1.5)` på en skärm med 150 procent mättes fönsterskalningen till 1,5 och kontrollernas skalning till 2,25. Tabellen följer därför inte en ändrad textstorlek ens efter omstart eller temabyte. Vid ett DPI-byte ändras båda faktorerna, så det felet syns först när textstorleken ändras utan DPI-byte.

Bildens uppskattade bredder är förenliga med en tabell kvar på 150 procent när omgivningen visas vid 100 procent. Proven bekräftar mekanismerna, men fastställer inte vilket DPI-byte eller vilken startväg som föregick just skärmbilden. Proven kördes i den lokala `.venv` med Python 3.14.10, CustomTkinter 6.0.0 och Tk 9.0.4. Systemets Python på samma dator har CustomTkinter 5.2.2 och Tk 8.6.15, och fontvalet och skalningsfaktorerna var desamma där. [GUI-koden, sök efter `AppGUI._update_treeview_style` och `AppGUI._build_ui`](ultimate-web-crawler.py)

## Beslut om ambitionsnivå (2026-11-06)

**Beslut.** Skrivbordsappen ska kunna användas helt med tangentbord och uppfylla de visuella kraven i den här planen: enhetlig typografi, skalning, textstorlek, kontrast och synligt fokus. Den anpassas inte för skärmläsare och beskrivs inte som förenlig med WCAG 2.2 AA. Serverläget körs i en terminal där skärmläsare kan användas.

**Skäl.**
- **Användare:** Verktyget är gratis, byggs internt och används av ett fåtal vana användare, främst på kommunala IT-avdelningar.
- **Lagen om tillgänglighet till digital offentlig service:** Den gäller offentliga aktörers webbplatser och mobilappar, även intranät. Den gäller inte ett internt skrivbordsprogram.
- **Upphandling:** Kraven på tillgänglighet gäller när kommunen köper något.
- **Diskrimineringslagen:** Behovet av skäliga åtgärder för en anställd kan mötas med serverläget i en terminal.
- **Kostnad:** Skärmläsarstöd skulle kräva ett nytt GUI-ramverk. Förprovet i [SKARMLASARPROV.md](SKARMLASARPROV.md) visade att Tk 9.0.4 bara ger namnlösa fönsterrutor till UI Automation, både med CustomTkinter och med ttk.

Bedömningen är inte juridisk rådgivning. Stäm av med kommunens tillgänglighetsansvariga vid ett formellt införande.

**Ompröva beslutet** i något av följande fall:
- verktyget sprids till andra organisationer som en produkt
- verktyget upphandlas
- någon som använder skärmläsare behöver skrivbordsappen
- Python levereras med en Tk-version som har stöd för skärmläsare (TIP 733, planerat för Tk 9.1).

I det sista fallet räcker det att köra om provet med `tools/skarmlasarprov.py`.

## Arbetsordning

1. **Lås leveransmiljön.** `requirements.txt` kräver i dag bara `customtkinter>=5.2`, och Tk-versionen följer Python-installationen. Lås CustomTkinter till en testad huvudversion och skriv in vilken Python- och Tk-version som är leveransmiljön. Alla prov i planen gäller den miljön.
2. **Åtgärda skalningsfelen och samla textrollerna på ett ställe** enligt del 1, punkt 1–3. Det är litet arbete och behövs så länge appen använder Tk.
3. **Ramverksprovet är vilande.** Efter beslutet om ambitionsnivå behövs inget ramverksbyte. Förprovet räcker som belägg för begränsningen.
4. **Bygg tangentbordsstöd och visuella krav** i nuvarande ramverk enligt del 2. Prova ttk-kontroller innan egna tangentbordsbindningar läggs på CustomTkinter-reglagen.

**Status 2026-11-06.** Steg 1 och 2 är genomförda. Del 1 är genomförd så när som på manuella prov på riktiga Windows-skärmar:
- **Leveransmiljö:** `customtkinter>=6.0,<7`. README och AGENTS.md anger Python 3.14, CustomTkinter 6.0 och Tk 9.0.
- **Gemensam typografi:** Segoe UI, med Roboto som reserv där Segoe UI saknas. Textrollerna finns i `AppGUI.TEXT_ROLES` och används av CustomTkinter, ttk-tabellen, loggen, Hjälp och verktygstipsen.
- **Skalning:** Tabellen följer samma faktor som övrig text via `AppGUI._on_ui_scaling`.
- **Färger:** Fältkanterna har den nya färgen. Verktygstipsen följer temat.
- **Tester:** `tests/test_gui_typography.py`. De går igenom med CustomTkinter 6.0/Tk 9.0 och 5.2/Tk 8.6.

Kvar i del 1: prova verkliga DPI-lägen 100, 125, 150 och 200 procent och flytt mellan skärmar. Testerna simulerar DPI-bytet med samma anrop som CustomTkinter gör. Valt läge för flikar och språk (1,16:1) åtgärdas i del 2.

**Steg 3 avslutades med förprovet.** Protokoll, provprogram och resultat finns i [SKARMLASARPROV.md](SKARMLASARPROV.md). Utan skärmläsare visade det automatiska provet följande:
- **Tangentbord:** ttk-varianten nådde alla sex delar med Tab. I appen nåddes inte bland annat Starta och flikarna.
- **UI Automation:** I Tk 9.0.4 fick Windows bara namnlösa fönsterrutor från båda varianterna.

Hörprovet med Skärmläsaren och NVDA görs inte efter beslutet om ambitionsnivå. Protokollet finns kvar om beslutet omprövas.

**Steg 4, tangentbordsdelen, är genomförd (2026-11-06).** CustomTkinter-reglagen behölls och fick egna tangentbindningar i `AppGUI._setup_keyboard`. Ett byte till ttk skulle ha brutit den enhetliga designen från del 1.
- **Tab** når alla synliga och aktiva kontroller i läsordning. Dolda flikar och inaktiva knappar hoppas över.
- **Mellanslag och Enter** aktiverar knappar, kryssrutor och Mörkt läge.
- **Piltangenter** byter flik och språk och stegar i menyerna. Alt+Pil ned öppnar en meny.
- **Fokusram:** 2 logiska pixlar, mörk i ljust läge och ljus i mörkt läge, med luft mot kontrollen. Den flyttar inte layouten.
- **Valt läge för flikar och språk** har en starkare fyllning och egen textfärg. Felet där fliken tappade sitt valda läge vid språkbyte är rättat.
- **Loggen** är skrivskyddad men kan nås, markeras och kopieras.
- **Tabellen:** Enter öppnar markerad sida och Ctrl+C kopierar dess adress.
- **Verktygstips** visas också vid fokus och stängs med Esc.
- **Hjälp:** texten får fokus, Esc stänger fönstret och fokus går tillbaka till Hjälp-knappen.
- **Tester:** `tests/test_gui_keyboard.py`, som kräver CustomTkinter 6.

**Textstorleken är genomförd (2026-11-06).**
- **Textstorlek:** valet **Text 100 %** till **Text 200 %** finns i översta raden. Ctrl+Plus, Ctrl+Minus och Ctrl+0 gör samma sak. Valet skalar CustomTkinter, tabellen och verktygstipsen med samma faktor och sparas mellan körningar.
- **Rullyta:** Hela innehållet ligger i en rullyta. Resultat och logg får plats för rubriker och innehåll i båda panelerna. I 720 × 450 går det att rulla till resultat och logg även vid 100 procent. I 1000 × 680 ryms båda vid 100 procent. När en kontroll får fokus eller textstorleken ändras rullas kontrollen fram.
- **Delaren:** Ett reglage med etiketten **Loggens storlek** och knappen **Återställ** provades som alternativ till dragning och togs bort samma dag. Det var ovant och svårbegripligt, och användare förväntar sig att dra i delaren som i andra program. 2.5.7 (dragning) eftersträvas därför inte för skrivbordsappen.
- **Tester:** `tests/test_gui_text_size.py`.

**Kompakt layout, loggföljning, snabbmenyer och fältfel är genomförda (2026-11-07).**
- **Kompakt layout:** När det breda läget inte ryms byter fönstret till en kolumn för inställningarna. Hjälp, textstorlek, tema och språk hamnar på en egen rad, och utdataknapparna på raden under körknapparna. Beslutet räknas varje gång från reglagens egna bredder (`AppGUI._wide_layout_width`) och prövas igen 300 ms efter ett byte. I 1000 × 680 är layouten bred vid 100–125 procent och kompakt vid 150 procent, utan vågrät rullning. I 1000 × 680 ryms det kompakta läget även vid 200 procent utan vågrät rullning i provmiljön. I 720 × 450 behövs vågrät rullning vid större textstorlek.
- **Loggen följer med** nya rader bara när den senaste raden syns, som i ett terminalfönster. Den som rullar upp för att läsa en äldre rad blir kvar där. Ingen extra knapp behövs.
- **Snabbmenyer:** Resultatlistan har **Öppna sidan i webbläsaren** och **Kopiera adress**. Loggen har **Kopiera** och **Markera allt**. Menyerna öppnas med högerklick, Menytangenten eller Skift+F10 och visar samma åtgärder som tangenterna. Planens förslag om en synlig knapp **Öppna markerad sida** ersattes av snabbmenyn, som är det vanliga mönstret i Windows.
- **Fältfel:** Varje sifferfält har ett eget felmeddelande med tillåtet intervall och exempel. Efter meddelandet visas fältets flik, fältet får fokus och texten markeras. Decimalkomma accepteras i Fördröjning.
- **Tester:** `tests/test_gui_layout_and_feedback.py`.

**Resultat, logg och fokus i små fönster.** Vid 720 × 450 kan hela innehållet rullas. Delaren lämnar utrymme för båda panelerna. Kontrollen som har fokus rullas fram efter ändrad textstorlek. Piltangenterna fungerar även när den tomma resultatlistan får fokus innan första raden kommer in. Loggen följer nya poster först när slutet på den sista visuella raden syns.

Kvar i del 2: vågrät rullning vid 200 procent i smala fönster, där etiketterna kan behöva stå ovanför fälten.

## Del 1. Typografi och skalning i dagens skrivbordsapp

1. **Lös skalningsfelen först.** Varje ändring av DPI eller textstorlek ska uppdatera CustomTkinter och tabellens font, radhöjd, rubrikmarginal, kolumnbredder och delare. Räkna alla tabellmått från kontrollernas skalning, samma faktor som CustomTkinters text, och inte från fönsterskalningen. Tabellens mått ska räknas från sina logiska grundvärden, eller från sparad logisk bredd där användaren har justerat en kolumn. CustomTkinter 6 anropar registrerade funktioner via `ScalingTracker.add_widget` när skalningen ändras. Det är ett internt gränssnitt, så versionen ska vara låst enligt arbetsordningen och kopplingen ska ha ett eget test.
   - Prova 150 → 100 → 150 procent DPI i samma fönster och kontrollera att temabyte inte krävs för att måtten ska rättas.
   - Prova ändrad textstorlek med `ctk.set_widget_scaling`, till exempel 100 → 150 → 100 procent, med oförändrad DPI. Kolumnrubriken ska växa i samma proportion som panelrubriken.
   - Kontrollera i båda proven att tabellens markering och de övre fältens värden finns kvar.
2. Använd vanlig **Segoe UI** som gemensam Windowsfont. Välj Tk-vikterna **normal** och **fet**. I det lokala provet identifierade Tk `Segoe UI Variable Text` men ersatte namnet `Segoe UI Variable` med Arial. Vanlig Segoe UI ger en förutsägbar startpunkt. Använd en gemensam reservfont på plattformar där Segoe UI saknas. [Tk:s fontalternativ](https://www.tcl-lang.org/man/tcl8.6/TkCmd/font.htm) · [Microsofts Windows-typografi](https://learn.microsoft.com/en-us/windows/apps/design/signature-experiences/typography)
3. Använd följande textroller i logiska pixlar vid 100 procent. Skalan utgår från Windows nivåer 12, 14 och 20, med Tk:s feta vikt för de starkare rollerna. De är produktval för den här appen. Samla rollerna på ett ställe i koden så att CustomTkinter, ttk och vanliga Tk-etiketter använder samma definitioner.

   | Roll | Storlek och vikt | Användning |
   | --- | --- | --- |
   | Panelrubrik | 14, fet. Vald i fönsterprovet nedan. | Resultat, Körningslogg, motsvarande rubriker i Hjälp och rapport |
   | Brödtext | 14, normal | Startadress, värden, fältetiketter, resultatceller, logg och instruktioner |
   | Reglagetext | 14, normal | Knappar, flikar, menyer och kryssrutor. Primär åtgärd kan ha fet vikt. |
   | Kolumnrubrik | 14, fet | Sidtitel, Status och URL. |
   | Metadata | 12, normal | Tidsstämplar och sekundära detaljer. Vägledning, fel och viktig status använder 14. |

   **Fönsterprov för panelrubriken.** I Windows typskala är 20 halvfet. Tk har bara normal och fet, och fet 20 blir tyngre och tar mer höjd. Jämför 20 fet och 14 fet i det riktiga fönstret, vid minsta fönsterstorlek 720 × 450 och vid 200 procents textstorlek. Välj 20 om rubrikerna fortfarande lämnar plats åt tabell och logg. Välj annars 14 fet och skilj panelrubriken från kolumnrubrikerna med placering, yta eller avstånd.

   **Resultat:** Vid 720 × 450 lämnade 20 fet ingen plats åt tabellen, och rubriken **Körningslogg** klipptes nederst. Därför används 14 fet. Panelrubriken står i mörk text på panelytan, medan kolumnrubrikerna har dämpad text på ett grått rubrikband.

4. Använd samma font och tema även i Hjälp, logg och verktygstips. `AppGUI._add_tooltip` sätter i dag gul bakgrund, svart text och en fast radbrytningsbredd. Ge tipsen temafärger, skalade mått och läsbar brödtext. Funktionerna för att nå tips med tangentbord och stänga dem hör till del 2. [GUI-koden, sök efter `AppGUI._add_tooltip`](ultimate-web-crawler.py)
5. Godkänn del 1 när rubrikerna är konsekventa i ljust och mörkt läge, texten och tabellmåtten följer samma DPI och textstorlek under användning, och fält, tabell och logg går att läsa i både normalt och kompakt fönster. Mät vid 100, 125, 150 och 200 procents DPI och prova svensk och engelsk text. Det här är en självständig leverans med begränsad omfattning. Uppskatta tiden efter provet med DPI-byte.

## Del 2. Tangentbord och visuell tillgänglighet

### Mål, nivå och omfattning

- **Skrivbordsappen på Windows** ska kunna användas helt med tangentbord och enkel pekare. Den ska klara kontrast, synligt fokus, textstorlek upp till 200 procent och kompakt fönster enligt den här delen. Motsvarande WCAG-kriterier är produktmål. Appen beskrivs inte som WCAG-förenlig, och skärmläsarstöd ingår inte enligt [beslutet om ambitionsnivå](#beslut-om-ambitionsnivå-2026-11-06). Målen omfattar huvudfönstret, båda inställningsflikarna, Hjälp, mappval, inloggnings- och felmeddelanden, resultat och logg, samt vägen till körningsrapporten.
- **Serverläget** ska fungera i en terminal med skärmläsare. Utdata, fel och exitkoder ska finnas som vanlig text, och ingen information får bara visas med färg.
- **Rapporten** ska gå att nå och förstå från appen. Om `AppGUI.open_report` öppnar rå JSON i en extern standardapp ska de viktigaste uppgifterna också kunna läsas i crawlerns sammanfattning. Exporterade källdokument bedöms separat. [GUI-koden, sök efter `AppGUI.open_report`](ultimate-web-crawler.py)

### Textförstoring och kompakt layout

1. Lägg till ett synligt textstorleksval från 100 till 200 procent. Prova även Windows inställning **Textstorlek** separat och ta hänsyn till den där ramverket stöder det. Samordna textförstoringen med den aktuella skärmens DPI. `ctk.set_widget_scaling()` påverkar också reglagens mått och får inte vara den enda åtgärden, eftersom ttk-tabellen måste följa samma faktor enligt del 1, punkt 1, och layouten behöver kunna anpassas. Testa textförstoring oberoende av DPI och kontrollera att båda kan användas tillsammans. [Microsoft om textskalning](https://learn.microsoft.com/en-us/windows/apps/develop/input/text-scaling)
2. Behåll två kolumner för inställningar när de får plats och växla till en kolumn när fälten annars trängs ihop. Lägg språk, tema och Hjälp på en egen rad vid behov. Bryt åtgärdsraden i grupper. Resultat och logg kan visas som tillgängliga flikar på mycket liten yta. Använd en sammanhängande rullyta vid behov så att fokuserade reglage kommer fram. Vid normal storlek ska alla inställningar fortfarande rymmas utan separat skrollning.
3. Prova omflöde för vertikalt innehåll i ett smalt skrivbordsfönster enligt WCAG2ICT. Dokumentera uppmätt innehållsyta, skalfaktor och navigering. Om plattformen begränsar storleken, prova närmaste möjliga storlek och redovisa det. Ompröva appens eget minimimått på 720. Om själva tabellen behöver två dimensioner, behåll horisontell rullning där och ge en detaljvy för fullständig sidtitel, status och URL. [Omflöde enligt WCAG2ICT](https://www.w3.org/TR/wcag2ict/#reflow)

### Layout och visuella tillstånd

| Yta | Utformning och godkänt resultat |
| --- | --- |
| Startadress och mall | Gemensam vänsterlinje för etiketter och fält. Hjälp, tema och språk får en konsekvent placering. Full URL ska kunna läsas och kopieras även när den visuellt är längre än fältet. Samma mall, adress och val finns kvar vid byte av flik, språk eller tema. |
| Inställningar | Fält som hör ihop placeras ihop och ligger i samma ordning visuellt och för tangentbordet. Skriv gränser som `0 betyder obegränsat` i läsbar hjälpinformation som kan kopplas till fältet. Visa obligatorisk vägledning även utan mus. Lägg samma inre marginal i panelerna för inställningar, resultat och logg. |
| Körning och genvägar | **Starta** är den primära åtgärden. **Pausa**, **Fortsätt** och **Stoppa** ligger i samma grupp. **Öppna mapp** och **Öppna rapport** ligger i utdata-gruppen. Aktiv, inaktiv, fokuserad, nedtryckt och vald kontroll får skilda och begripliga tillstånd. Stoppa får en tydlig textetikett även när färg eller ikoner inte kan urskiljas. |
| Resultat | Behåll **Sidtitel**, **Status** och **URL** med vänsterställd text. Användaren ska kunna läsa hela raden, markera den, kopiera URL:en och öppna sidan via en tydligt namngiven åtgärd. Resultat som kommer in ska inte flytta fokus eller byta användarens markerade rad. Tomvyn får en kort förklaring som försvinner när första raden visas. |
| Körningslogg | Löpande text visas med samma läsfont som resten av appen. Loggen ska vara skrivskyddad och gå att navigera, markera och kopiera. Automatisk följning av loggen ska kunna stängas av utan att pausa hämtningen. När användaren läser en äldre rad ska nya rader inte ta över visningspositionen. |
| Delare | Behåll draghandtaget, som fungerar som i andra program. Det ska vara lätt att se och träffa. Inget extra reglage för delningen, enligt beslutet 2026-11-06. |
| Hjälp och dialoger | Samma läsfont, ordning och fokusmarkering. Hjälptexter finns i anslutning till det de förklarar. Fel pekar ut fältet och ger ett förslag när en rättning går att föreslå. När en dialog stängs går fokus tillbaka till en meningsfull plats, normalt den kontroll som öppnade den. |

### Färger som kan behållas och färger som behöver ändras

Beräknade sRGB-värden från den nuvarande skrivbordsstilen. Det här är exempel från källkodens färgpar. Det behövs en mätning av de faktiska tillstånden på skärmen och av de färger Windows eller ramverket tillämpar.

| Prov | Ljust | Mörkt | Beslut |
| --- | ---: | ---: | --- |
| Normal text mot panelytan | 15,64:1 | 12,68:1 | Behåll som bas. |
| Dämpad text mot tabellrubrikens yta | 5,61:1 | 5,71:1 | Behåll om det renderade tillståndet stämmer med färgparet. |
| Vit text mot startknappen | 7,39:1 | 5,89:1 | Grundläget klarar textmålet. Verifiera också hover, fokus och nedtryckt läge. |
| Fältets kant mot fältets fyllning | 1,41:1 | 1,34:1 | Öka kontrasten på den visuella ledtråd som krävs för att identifiera aktiva fält. |
| Fältets kant mot panelytan runt fältet | 1,48:1 | 1,59:1 | Samma åtgärd. Kanten ska klara 3:1 mot båda angränsande ytorna. |
| Vald flik och valt språk (SV/EN) mot ovalt läge | 1,16:1 | 1,36:1 | Valt läge visas bara med fyllning. Lägg till en ledtråd som inte bygger på den svaga fyllningen, till exempel en understrykning eller kant med minst 3:1 mot omgivningen, eller fet text för valt läge. Gäller inställningsflikarna och språkvalet. |
| Deklarerad färg för delaren mot dess yta | 2,57:1 | 3,21:1 | Verifiera den faktiska greppindikatorn i båda lägena och höj kontrasten där den behövs. |

**Ny färg för fältkanter.** Inför en egen färg för fältkanter i stället för att ändra `AppGUI.PANEL_BORDER`, som också ritar de dekorativa panelkanterna. Följande startvärden har marginal till 3:1 mot både fältets fyllning och panelytan:

| Tema | Fältkant | Mot fältets fyllning | Mot panelytan |
| --- | --- | ---: | ---: |
| Ljust | `#7b8794` | 3,47:1 | 3,66:1 |
| Mörkt | `#7f8b98` | 3,47:1 | 4,11:1 |

Använd minst 4,5:1 för normal text och minst 3:1 för stor text enligt WCAG:s definition. Använd minst 3:1 för visuella delar som behövs för att identifiera kontroller, fokus eller valda tillstånd mot relevanta angränsande färger, enligt kriteriets omfattning. Dekorativa panelkanter kan fortsätta vara diskreta. Alla obligatoriska gränser, markeringar och felmeddelanden granskas i ljust, mörkt och Windows kontrasttema. Använd en synlig fokusram, gärna med en dubbelfärgad markering där samma färg annars försvinner mot någon yta. En fokusram på minst två logiska pixlar är här ett föreslaget produktmål. Den faktiska WCAG-bedömningen använder de tillämpliga kriterierna. [WCAG om textkontrast](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html) · [WCAG om kontrast för kontroller och fokus](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html)

### Tillgänglig användning av hela arbetsflödet

#### Tangentbord och pekare

**Bekräftad brist.** I den lokala Tk-provkörningen hoppade den normala tabbordningen över bland annat Starta, dokumentkryssrutan, Filformat, Körläge och tema- och språkreglagen. Adressfältet, flera fält på den aktiva fliken, resultatlistan och loggen ingick i tabbordningen. Appen saknar egna bindningar som gör de överhoppade kontrollerna nåbara och användbara med tangentbord. Detta blockerar en första körning med tangentbord och prioriteras tidigt i del 2.

- Definiera tabbordningen och kontrollera den i Windows. Föreslagen huvudslinga är startadress, mall när den finns, aktiv inställningsflik, flikens fält i läsordning, körknappar, utdatagenvägar, resultat och logg. Hjälp, språk och tema ska kunna nås konsekvent, även i kompakt vy. Tab och Skift+Tab fungerar åt båda hållen. Pilar byter flik eller val där plattformens standardbeteende anger det. Inga osynliga eller inaktiva kontroller får ta fokus.
- Prova att aktivera alla huvudåtgärder med Enter eller Mellanslag enligt kontrolltypen. Lägg en namngiven åtgärd **Öppna markerad sida** i resultatdelen, med tangentbordsaktivering. Behåll dubbelklick som snabbväg. Lägg till en tillgänglig metod för att kopiera en URL. Prova radnavigering, längre listor och scroll när nya resultat kommer in.
- Ge varje kontroll en synlig fokusindikator. Kontrollera att fokuserade kontroller inte döljs helt av verktygstips, panelkanter eller annan yta vid förstoring. Hjälp som visas vid hovring ska kunna nås från fokus, stängas med Esc där tillämpligt, kunna läsas när pekaren förs över den och vara kvar till dess att användaren hinner läsa den. Kärninstruktioner och fel finns även som vanlig, beständig text.
- Delaren mellan resultat och logg flyttas genom att dra i den. Ett alternativ utan dragning (2.5.7) togs bort efter beslutet 2026-11-06. Prova verkliga träffytor i båda teman. Sikta på minst 32 × 32 logiska pixlar för vanliga reglage. Dokumentera 2.5.8:s miniminivå på 24 × 24 CSS-pixlar eller relevant motsvarighet och varje faktiskt tillämpligt undantag, som avstånd eller en likvärdig kontroll. Huvudåtgärder ska aktiveras på släpp eller erbjuda motsvarande avbrottsmöjlighet. [Dragning](https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements.html) · [Träffyta](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)

#### Status och återkoppling

Skärmläsarstöd i skrivbordsappen ingår inte efter [beslutet om ambitionsnivå](#beslut-om-ambitionsnivå-2026-11-06). Kraven på namn och roller i UI Automation och på uppläsning av status är vilande. Underlaget finns i [SKARMLASARPROV.md](SKARMLASARPROV.md).

- Visa redo, startar, hämtar, pausad, återupptagen, stoppar, klar, delvis klar och fel som synlig text. Visa förloppet med ett korrekt värde när nämnaren är känd och som obestämt annars. Ge ett blockerande fel en tydlig väg tillbaka till uppgiften.
- Prova svensk och engelsk apptext, även språkbyte medan appen är öppen. Främmande sidtitlar och URL:er från källorna ska gå att läsa och kopiera.

I dag öppnas resultat bara med dubbelklick, och verktygstips visas bara när pekaren hålls över en kontroll. Delaren skapas som `ttk.Panedwindow`. Förprovet visade att Tk 9.0.4 bara ger namnlösa fönsterrutor till UI Automation. [GUI-koden, sök efter `AppGUI._add_tooltip` och `AppGUI._build_ui`](ultimate-web-crawler.py)

### Genomförandeordning och beslut om ramverk

| Etapp | Arbete | Klart när |
| --- | --- | --- |
| 1. Avgränsat teknikprov | Vilande. Förprovet i [SKARMLASARPROV.md](SKARMLASARPROV.md) räcker som belägg för att skrivbordsappen inte stöder skärmläsare. Kör hela provet med Skärmläsaren och NVDA om beslutet omprövas. | Avslutat 2026-11-06 med förprovet. |
| 2. Åtgärda arbetsflöden | Bygg tangentbordsnavigation, synlig fokusmarkering, tydligt valt läge för flikar och språk, fel i text, loggnavigering och status. Lägg till textförstoring och kompakt layout enligt del 2. | Alla viktiga uppgifter kan slutföras med tangentbord och enkel pekare, vid 200 procents textstorlek och i båda teman. |

Prova vanliga ttk-kontroller för tangentbordsnavigation innan egna bindningar läggs på många CustomTkinter-reglage. Förprovet visade att ttk-kontrollerna nås med Tab. Temat `sv-ttk` kan ge Windowsutseende med ljust och mörkt läge. [Sun Valley-temat för ttk](https://github.com/rdbende/Sun-Valley-ttk-theme)

**Om beslutet omprövas.** Tk:s antagna TIP 733 ger standardkontroller och ttk-kontroller stöd för skärmläsare i Tk 9.1. På Windows går stödet via MSAA, med en tunn brygga till UI Automation. Om Python levereras med den versionen, kör om provet och redovisa både UI Automation och MSAA (`LegacyIAccessible`). Annars är PySide6 med Qt Widgets förstahandskandidat. Det kan använda Python-motorn, och Qt dokumenterar tillgänglighetsgränssnitt för sina standardkontroller. Godkänn namn, roller, fokus, tabellnavigering, statusannonsering och systemets text- och kontrastinställningar i de sex proven innan något byggs om. [Tk TIP 733](https://core.tcl-lang.org/tips/doc/main/tip/733.md) · [Qt om tillgänglighet för QWidget](https://doc.qt.io/qt-6/accessible-qwidget.html)

Om UI-lagret behöver bytas ska crawlerns motor, konfigurering, rapporter och arbetskö kunna köras som i dag. Utgå från befintlig gräns där `AppGUI` tar emot meddelanden via `msg_queue`. Prova bara en tunn UI-del med ett syntetiskt körningsflöde först. Behåll regressionsprover för mallar, sparade inställningar, start, paus, stopp, summering och rapport. Ändringar av GUI får inte göra att CLI måste ladda skrivbordsramverket.

### Protokoll för verifiering

#### Praktiska acceptansprov

| Prov | Utförande och förväntat resultat |
| --- | --- |
| V01. Första körningen | Med bara tangentbord, ange en lokal testadress, ändra grundinställningar, gå till och från Avancerat, välj mapp och starta. Samtliga etiketter och värden är begripliga, alla åtgärder kan genomföras och fokus är synligt hela vägen. |
| V02. Fel, paus och stopp | Testa tom eller ogiltig adress, ogiltigt tal, otillgänglig målmapp, paus, återupptagning, stopp och lyckat avslut. Fel identifierar fältet och rättningsmöjligheten. Användarens värden finns kvar och statustexten ändras en gång per relevant händelse. |
| V03. Resultat och delare | Kör mot en lokal testsida med många resultat och en lång URL. Navigera mellan rader med tangentbordet, kopiera och öppna en vald URL. Ge loggen mer plats genom att dra i delaren och prova båda ytterlägena. |
| V04. Logg och rapport | Läs äldre loggrader medan nya tillkommer. Markering och visningsposition får inte hoppa när följning är avstängd. Läs slutsammanfattning och öppna rätt rapport. Prova ett felutfall och en körning som bara lyckas delvis. |
| V05. Storlek och färg | Prova normalt och minsta användbara fönster, 100–200 procent textförstoring, flera DPI-lägen och flytt mellan skärmar. Prova ljust, mörkt och Windows kontrasttema. Fokus och viktig text ska gå att se. Samma arbetsuppgifter ska gå att utföra även när layouten går över till en kolumn eller en flikvy. |
| V06. Skärmläsare | Vilande för skrivbordsappen enligt beslutet om ambitionsnivå. Serverläget kan provas med skärmläsare i en terminal. |

#### Kriterier som används som produktmål

Målen för skrivbordsappen omfattar följande delar av WCAG:
- kontrast: 1.4.3 och 1.4.11
- textstorlek och omflöde: 1.4.4 och 1.4.10
- innehåll vid hovring eller fokus: 1.4.13
- tangentbord: 2.1.1 och 2.1.2
- fokusordning och synligt fokus: 2.4.3, 2.4.7 och 2.4.11
- träffyta: 2.5.8. Dragning (2.5.7) eftersträvas inte, se delaren.

#### Leveransgräns

1. Kör GUI- och regressionstester. Utöka den automatiska täckningen där ett känt problem behöver skydd, till exempel klippta fält vid förstoring, tappat fokus, resultat och logg som försvinner i små fönster och felaktig status efter en slutförd körning.
2. Kör hela projektets testsvit enligt `AGENTS.md`. Använd lokala källor och dokumentera överhoppade testfall. Manuella prov på Windows krävs för sådant som automatiska tester inte kan avgöra.
3. Redovisa produktmålen och de manuella prov som återstår. Viktiga flöden ska kunna slutföras med tangentbord och vid förstoring.
