"""Regressioner för kompakt layout, loggföljning, snabbmenyer och fältfel i GUI:t (DESIGNPLAN-WCAG.md, del 2)."""
from unittest.mock import Mock

import pytest

from test_crawler import uwc
from test_gui_keyboard import BASIC_ORDER, press, show_tab, tab_order
from test_gui_typography import gui  # noqa: F401  (gui är en fixtur)

pytestmark = pytest.mark.skipif(
    not uwc.HAS_GUI or int(uwc.ctk.__version__.split('.')[0]) < 6,
    reason='Kräver CustomTkinter 6 enligt requirements.txt')


def settle(gui, wait_ms=450):
    """Låt layouten och den fördröjda kontrollen av läget bli klara."""
    for _ in range(4):
        gui.root.update()
    done = uwc.tk.BooleanVar(gui.root, False)
    gui.root.after(wait_ms, done.set, True)
    gui.root.wait_variable(done)
    for _ in range(4):
        gui.root.update()


def placement(widget):
    info = widget.grid_info()
    return int(info['row']), int(info['column'])


def test_large_text_switches_to_compact_layout_without_sideways_scrolling(gui):
    gui.root.geometry('1000x680')
    url_row_width = gui._url_frame.winfo_reqwidth()
    gui._apply_text_scale(1.5)
    settle(gui)
    assert gui._compact
    assert not gui._hbar_box.winfo_ismapped()
    # Hjälp, textstorlek, tema och språk på egen rad; inställningarna i en kolumn.
    assert str(gui._prefs_frame.pack_info()['in']) == str(gui._source_panel)
    assert str(gui._output_buttons.pack_info()['in']) == str(gui._main_frame)
    assert placement(gui.delay_entry) == (0, 1)
    assert placement(gui.max_pages_entry) == (1, 1)
    assert placement(gui.headless_menu) == (5, 1)
    assert placement(gui.cb_convert_docs) == (1, 0)
    assert placement(gui.cb_rm_pnr) == (1, 0)
    # Tabbordningen följer fortfarande läsordningen.
    assert tab_order(gui) == BASIC_ORDER

    gui._apply_text_scale(1.0)
    settle(gui)
    assert not gui._compact
    assert str(gui._prefs_frame.pack_info()['in']) == str(gui._url_frame)
    assert placement(gui.max_pages_entry) == (0, 3)
    assert placement(gui.cb_rm_pnr) == (0, 2)
    # pack() behöll tidigare marginaler från det kompakta läget, så adressraden blev bredare.
    assert gui._url_frame.winfo_reqwidth() == url_row_width


@pytest.mark.parametrize("tab", ["tab_basic", "tab_adv"])
def test_smallest_window_keeps_results_and_log_accessible(gui, tab):
    gui.root.geometry("720x450")
    show_tab(gui, tab)
    settle(gui)
    assert not gui._compact
    assert gui._vbar_box.winfo_ismapped()
    assert not gui._hbar_box.winfo_ismapped()

    gui.msg_queue.put(("table", ("https://kommun.example/", "OK", "Avgifter")))
    gui.process_queue()
    gui._log_to_gui("Provmeddelande")
    gui.root.update()
    text = gui.log_area._textbox
    assert gui.tree.winfo_viewable() and text.winfo_viewable()
    row = gui.tree.get_children()[0]
    assert gui.tree.bbox(row)
    assert text.bbox("end-2c")
    assert tab_order(gui)[-2:] == ["tree", "log_area"]

    press(gui, text, "<Shift_L>")
    viewport = gui._scroll_canvas
    top = text.winfo_rooty() - viewport.winfo_rooty()
    assert 0 <= top <= viewport.winfo_height() - text.winfo_height()


def test_sash_can_give_log_more_space(gui):
    pane = gui.results_pane
    initial = pane.sashpos(0)
    height = gui.log_area.winfo_height()
    pane.sashpos(0, initial - round(30 * gui._ui_scale))
    pane.event_generate("<ButtonRelease-1>")
    gui.root.update()
    assert gui.log_area.winfo_height() > height


def test_log_follows_new_lines_only_when_showing_the_end(gui):
    for number in range(80):
        gui._log_to_gui(f'Rad {number}')
    gui.root.update()
    text = gui.log_area._textbox
    # Den senaste textraden syns (raden efter den avslutande radbrytningen är tom).
    assert text.bbox('end-2c') is not None
    assert 'Rad 79' in text.get('end-2c linestart', 'end-1c')

    text.yview_moveto(0)
    gui.root.update()
    gui._log_to_gui('Ny rad medan användaren läser')
    gui.root.update()
    assert text.yview()[0] == 0

    text.yview_moveto(1)
    gui.root.update()
    gui._log_to_gui('Ny rad i slutet')
    gui.root.update()
    assert text.bbox('end-2c') is not None


def test_log_keeps_position_within_a_wrapped_post(gui):
    gui._log_to_gui("En lång loggpost " * 300)
    gui.root.update()
    text = gui.log_area._textbox
    text.yview_moveto(0.2)
    gui.root.update()
    first = text.index("@0,0")
    assert text.dlineinfo("end-2c") is None

    gui._log_to_gui("En ny post")
    gui.root.update()
    assert text.index("@0,0") == first
    assert text.dlineinfo("end-2c") is None


def test_context_menus_offer_the_same_actions_as_the_keys(gui, monkeypatch):
    shown = []
    gui._popup = lambda menu, x, y: shown.append(menu) or 'break'
    opened = Mock()
    monkeypatch.setattr(uwc.webbrowser, 'open', opened)
    gui.tree.insert('', 'end', values=('https://kommun.example/a', 'OK (200)', 'Sida A'))
    gui.tree.insert('', 'end', values=('https://kommun.example/b', 'OK (200)', 'Sida B'))
    gui.root.update()

    # Högerklick väljer raden under pekaren.
    second = gui.tree.get_children()[1]
    x, y, _width, height = gui.tree.bbox(second)
    event = type('Event', (), {'y': y + height // 2, 'x_root': 0, 'y_root': 0})()
    gui._show_result_menu_at_pointer(event)
    assert shown[-1] is gui.result_menu
    assert gui.tree.selection() == (second,)
    assert gui.result_menu.entrycget(0, 'state') == 'normal'
    gui.result_menu.invoke(1)
    assert gui.root.clipboard_get() == 'https://kommun.example/b'
    gui.result_menu.invoke(0)
    opened.assert_called_once_with('https://kommun.example/b')

    # Menytangenten och Skift+F10 öppnar samma meny från tangentbordet.
    press(gui, gui.tree, '<Shift-F10>')
    assert shown[-1] is gui.result_menu

    gui._log_to_gui('Rad att kopiera')
    press(gui, gui.log_area._textbox, '<Control-a>')
    gui._show_log_menu(0, 0)
    assert shown[-1] is gui.log_menu
    assert gui.log_menu.entrycget(0, 'state') == 'normal'
    gui.log_menu.invoke(0)
    assert 'Rad att kopiera' in gui.root.clipboard_get()

    assert gui.result_menu.entrycget(1, 'label') == 'Kopiera adress'
    gui.change_language_event('EN')
    assert gui.result_menu.entrycget(1, 'label') == 'Copy address'
    assert gui.log_menu.entrycget(1, 'label') == 'Select all'


@pytest.mark.parametrize(('field', 'value', 'message'), [
    ('delay_entry', 'snabbt', 'err_delay'),
    ('delay_entry', '0,05', 'err_delay'),
    ('max_pages_entry', '-1', 'err_max_pages'),
    ('max_depth_entry', '2.5', 'err_max_depth'),
    ('concurrency_entry', '99', 'err_concurrency'),
])
def test_field_errors_name_the_field_and_move_focus_there(gui, monkeypatch, field, value, message):
    errors = []
    monkeypatch.setattr(uwc.messagebox, 'showerror', lambda title, text: errors.append(text))
    gui.url_entry.delete(0, 'end')
    gui.url_entry.insert(0, 'https://kommun.example/')
    entry = getattr(gui, field)
    entry.delete(0, 'end')
    entry.insert(0, value)
    # Felet ska synas även när användaren står på en annan flik.
    gui.tabview.set(gui.texts['tab_adv']['sv'])
    gui.start_crawl()
    assert errors == [gui.texts[message]['sv']]
    assert gui.tabview.get() == gui.texts['tab_basic']['sv']
    assert gui.root.focus_lastfor() is entry._entry
    assert entry._entry.selection_present()


@pytest.mark.parametrize("url", ["inte en adress", "https://[", "https://kommun.example:fel"])
def test_url_error_moves_focus_to_the_address(gui, monkeypatch, url):
    errors = []
    monkeypatch.setattr(uwc.messagebox, 'showerror', lambda title, text: errors.append(text))
    gui.url_entry.delete(0, 'end')
    gui.url_entry.insert(0, url)
    gui.start_crawl()
    assert errors == [gui.texts['err_url']['sv']]
    assert gui.root.focus_lastfor() is gui.url_entry._entry
    assert gui.url_entry._entry.selection_present()


def test_decimal_comma_is_accepted_for_the_delay(gui, monkeypatch):
    captured = []

    def capture(config, *args):
        captured.append(config)
        raise RuntimeError('Testet avslutas före trådstart')

    monkeypatch.setattr(uwc, 'AsyncWebCrawler', capture)
    monkeypatch.setattr(uwc.messagebox, 'showerror', lambda title, text: None)
    gui.url_entry.delete(0, 'end')
    gui.url_entry.insert(0, 'https://kommun.example/')
    gui.delay_entry.delete(0, 'end')
    gui.delay_entry.insert(0, '0,5')
    gui.start_crawl()
    assert captured[0]['delay'] == 0.5
