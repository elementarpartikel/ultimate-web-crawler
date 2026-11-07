"""Tekniskt prov av skärmläsarstöd i skrivbordsappen (DESIGNPLAN-WCAG.md, del 2, etapp 1).

Provet visar samma sex delar i två varianter:
  app  den riktiga appen (CustomTkinter), med ett syntetiskt körningsflöde
  ttk  samma delar byggda med vanliga ttk-kontroller, som jämförelse

De sex delarna: namngivet adressfält, knappen Starta, flikval, tabellrad med tre celler,
läsbar logg och ett slutmeddelande som ska annonseras. Protokollet finns i SKARMLASARPROV.md.

Kör från projektmappen med leveransmiljön:
  .venv\\Scripts\\python.exe tools\\skarmlasarprov.py app
  .venv\\Scripts\\python.exe tools\\skarmlasarprov.py ttk
  .venv\\Scripts\\python.exe tools\\skarmlasarprov.py app --tabbordning

F9 simulerar en körning: tre resultatrader, loggrader och ett slutmeddelande.
Ingen sida hämtas och användarens sparade inställningar läses eller skrivs inte.
"""
import argparse
import importlib.util
import os
import sys
import tkinter as tk
from tkinter import ttk

PROJEKTMAPP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Toppdomänen .example är reserverad (RFC 2606) och pekar aldrig på en riktig sajt.
PROVADRESS = "https://kommun.example/blanketter.html"
PROVSIDOR = [
    ("https://kommun.example/blanketter.html", "OK (200)", "Blanketter för miljö och hälsa"),
    ("https://kommun.example/avgifter.html", "OK (200)", "Avgifter och taxor"),
    ("https://kommun.example/kontakt.html", "OK (200)", "Kontakta miljöenheten"),
]
TITELTILLAGG = " – skärmläsarprov"


def ladda_crawlern():
    """Huvudskriptet har bindestreck i namnet och laddas därför som i testerna."""
    sys.path.insert(0, PROJEKTMAPP)
    sokvag = os.path.join(PROJEKTMAPP, "ultimate-web-crawler.py")
    spec = importlib.util.spec_from_file_location("ultimate_web_crawler", sokvag)
    modul = importlib.util.module_from_spec(spec)
    sys.modules["ultimate_web_crawler"] = modul
    spec.loader.exec_module(modul)
    return modul


# ─── Variant 1: den riktiga appen ─────────────────────────────
def bygg_app():
    uwc = ladda_crawlern()
    if not uwc.HAS_GUI:
        sys.exit("GUI-beroenden saknas i den här Python-miljön.")
    # Provet får inte ändra användarens sparade inställningar och får aldrig hämta något.
    uwc.AppGUI._load_settings = lambda self: None
    uwc.AppGUI._save_settings = lambda self: None
    uwc.AppGUI.start_crawl = lambda self: self._log_to_gui(
        "Starta aktiverades. Provet startar ingen hämtning.")
    root = uwc.ctk.CTk()
    gui = uwc.AppGUI(root)
    root.title(root.title() + TITELTILLAGG)
    gui.url_entry.delete(0, "end")
    gui.url_entry.insert(0, PROVADRESS)

    def simulera():
        meddelanden = gui.msg_queue
        meddelanden.put(("log", "Provkörning startad. Ingen sida hämtas."))
        for url, status, titel in PROVSIDOR:
            meddelanden.put(("table", (url, status, titel)))
            meddelanden.put(("log", f"Hämtade {url}"))
        meddelanden.put(("summary", uwc.CrawlSummary("success", (3, 3, 0, 0, 0, 0), (), None, None)))
        meddelanden.put(("done", "Klar"))

    viktiga = ["url_entry", "help_btn", "theme_switch", "lang_switch", "tabview",
               "delay_entry", "max_pages_entry", "max_depth_entry", "format_menu",
               "concurrency_entry", "headless_menu", "cb_docs", "cb_convert_docs",
               "dir_entry", "btn_folder", "start_btn", "open_btn", "tree", "log_area"]
    if getattr(gui, "template_menu", None) is not None:
        viktiga.insert(1, "template_menu")
    namn = {name: widget for name, widget in vars(gui).items() if isinstance(widget, tk.Misc)}
    return root, simulera, namn, viktiga


# ─── Variant 2: vanliga ttk-kontroller ────────────────────────
def bygg_ttk():
    root = tk.Tk()
    root.title("Webbdammsugare – ttk" + TITELTILLAGG)
    root.geometry("900x600")
    ram = ttk.Frame(root, padding=12)
    ram.pack(fill="both", expand=True)

    adressrad = ttk.Frame(ram)
    adressrad.pack(fill="x")
    ttk.Label(adressrad, text="Startadress").pack(side="left", padx=(0, 8))
    adress = ttk.Entry(adressrad)
    adress.insert(0, PROVADRESS)
    adress.pack(side="left", fill="x", expand=True)

    flikar = ttk.Notebook(ram)
    flikar.pack(fill="x", pady=8)
    grund = ttk.Frame(flikar, padding=8)
    avancerat = ttk.Frame(flikar, padding=8)
    flikar.add(grund, text="Grundinställningar")
    flikar.add(avancerat, text="Avancerat")
    dokument = ttk.Checkbutton(grund, text="Ladda ner dokument (PDF m.m.)")
    dokument.state(["!alternate", "selected"])
    dokument.pack(anchor="w")
    robots = ttk.Checkbutton(avancerat, text="Följ robots.txt")
    robots.state(["!alternate", "selected"])
    robots.pack(anchor="w")

    knapprad = ttk.Frame(ram)
    knapprad.pack(fill="x")
    status = ttk.Label(ram, text="Redo att starta")

    def logga(text):
        logg.configure(state="normal")
        logg.insert("end", text + "\n")
        logg.configure(state="disabled")
        logg.see("end")

    starta = ttk.Button(knapprad, text="Starta", command=lambda: logga("Starta aktiverades."))
    starta.pack(side="left")
    status.pack(anchor="w", pady=6)

    ttk.Label(ram, text="Resultat").pack(anchor="w")
    tabell = ttk.Treeview(ram, columns=("Titel", "Status", "URL"), show="headings", height=5)
    for kolumn, rubrik in (("Titel", "Sidtitel"), ("Status", "Status"), ("URL", "URL")):
        tabell.heading(kolumn, text=rubrik, anchor="w")
    tabell.pack(fill="both", expand=True)

    ttk.Label(ram, text="Körningslogg").pack(anchor="w", pady=(8, 0))
    # Skrivskyddad men fokuserbar, så att raderna kan läsas och markeras med tangentbordet.
    logg = tk.Text(ram, height=6, wrap="word", takefocus=1, state="disabled")
    logg.pack(fill="both", expand=True)

    def simulera():
        logga("Provkörning startad. Ingen sida hämtas.")
        for url, sidstatus, titel in PROVSIDOR:
            tabell.insert("", 0, values=(titel, sidstatus, url))
            logga(f"Hämtade {url}")
        status.configure(text="Lyckad körning. Besökta sidor 3, nya 3, ändrade 0, borttagna 0.")

    namn = {"url_entry": adress, "tabview": flikar, "cb_docs": dokument, "start_btn": starta,
            "tree": tabell, "log_area": logg}
    return root, simulera, namn, list(namn)


# ─── Tabbordning ──────────────────────────────────────────────
def tabbordning(root, grans=300):
    """Följ Tk:s vanliga Tab-ordning från fönstret tills den börjar om."""
    ordning, widget = [], root.tk_focusNext()
    while widget is not None and widget not in ordning and len(ordning) < grans:
        ordning.append(widget)
        widget = widget.tk_focusNext()
    return ordning


def namn_for(widget, namn):
    """Närmaste namngivna kontroll som innehåller den fokuserade Tk-kontrollen."""
    omvant = {id(w): n for n, w in namn.items()}
    while widget is not None:
        if id(widget) in omvant:
            return omvant[id(widget)]
        widget = widget.master
    return None


def skriv_tabbordning(root, namn, viktiga, variant):
    root.update()
    ordning = tabbordning(root)
    print(f"Tabbordning ({variant}):")
    nadda = []
    for nummer, widget in enumerate(ordning, 1):
        kontroll = namn_for(widget, namn)
        nadda.append(kontroll)
        print(f"  {nummer:2}. {kontroll or '(namnlös)'}  [{widget.winfo_class()}]")
    saknas = [n for n in viktiga if n not in nadda]
    print("Nås inte med Tab: " + (", ".join(saknas) if saknas else "inga"))
    return saknas


def main():
    tolk = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    tolk.add_argument("variant", choices=("app", "ttk"))
    tolk.add_argument("--tabbordning", action="store_true", help="skriv ut tabbordningen och avsluta")
    tolk.add_argument("--simulera-efter", type=float, metavar="SEK", help="starta provkörningen automatiskt")
    tolk.add_argument("--stang-efter", type=float, metavar="SEK", help="stäng fönstret automatiskt")
    argument = tolk.parse_args()

    root, simulera, namn, viktiga = (bygg_app if argument.variant == "app" else bygg_ttk)()
    root.bind_all("<F9>", lambda _event: simulera())
    if argument.tabbordning:
        saknas = skriv_tabbordning(root, namn, viktiga, argument.variant)
        root.destroy()
        sys.exit(1 if saknas else 0)
    if argument.simulera_efter is not None:
        root.after(round(argument.simulera_efter * 1000), simulera)
    if argument.stang_efter is not None:
        root.after(round(argument.stang_efter * 1000), root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
