"""Regressioner för tangentbordsstöd, fokusram och valt läge i GUI:t (DESIGNPLAN-WCAG.md, del 2)."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_crawler import uwc
from test_gui_typography import contrast, gui  # noqa: F401  (gui är en fixtur)

# requirements.txt kräver CustomTkinter 6. Version 5.2 behåller t.ex. ett inaktuellt fliknamn
# efter rename(), vilket 6.0 rättar.
pytestmark = pytest.mark.skipif(
    not uwc.HAS_GUI or int(uwc.ctk.__version__.split('.')[0]) < 6,
    reason='Kräver CustomTkinter 6 enligt requirements.txt')

BASIC_ORDER = [
    'url_entry', 'help_btn', 'text_size_menu', 'theme_switch', 'lang_switch', 'tabview',
    'delay_entry', 'max_pages_entry', 'max_depth_entry', 'format_menu', 'concurrency_entry',
    'headless_menu', 'cb_docs', 'cb_convert_docs', 'dir_entry', 'btn_folder',
    'start_btn', 'open_btn', 'tree', 'log_area',
]


def tab_order(gui):
    """Namnen på kontrollerna i Tk:s vanliga Tab-ordning, som i tools/skarmlasarprov.py."""
    gui.root.update()
    named = {id(widget): name for name, widget in vars(gui).items() if isinstance(widget, uwc.tk.Misc)}
    order, widget = [], gui.root.tk_focusNext()
    while widget is not None and widget not in order and len(order) < 200:
        order.append(widget)
        widget = widget.tk_focusNext()
    names = []
    for widget in order:
        while widget is not None and id(widget) not in named:
            widget = widget.master
        names.append(named.get(id(widget)) if widget is not None else None)
    return names


def show_tab(gui, key):
    gui.tabview.set(gui.texts[key][gui.lang])
    # Flikbytet lämnar den tidigare fliken synlig i 100 ms.
    ready = uwc.tk.BooleanVar(gui.root, False)
    gui.root.after(150, ready.set, True)
    gui.root.wait_variable(ready)
    gui.root.update()


def press(gui, widget, key):
    """Ge kontrollen fokus som Tab gör och skicka en tangent till den."""
    gui.root.focus_force()
    uwc.tk.Misc.focus_set(widget)
    gui.root.update()
    widget.event_generate(key)
    gui.root.update()


def test_tab_order_follows_reading_order_and_skips_hidden_or_disabled(gui):
    assert tab_order(gui) == BASIC_ORDER

    gui.pause_btn.configure(state='normal')
    order = tab_order(gui)
    assert order.index('pause_btn') == order.index('start_btn') + 1
    gui.pause_btn.configure(state='disabled')

    show_tab(gui, 'tab_adv')
    order = tab_order(gui)
    for name in ('cb_hybrid', 'cb_sitemap', 'exclude_entry', 'cb_rm_ip'):
        assert name in order, name
    for name in ('delay_entry', 'cb_docs', 'format_menu'):
        assert name not in order, name


def test_keys_operate_buttons_checkboxes_menus_and_tabs(gui):
    start = Mock()
    gui.start_btn.configure(command=start)
    press(gui, gui.start_btn, '<space>')
    press(gui, gui.start_btn, '<Return>')
    assert start.call_count == 2

    docs = gui.docs_var.get()
    press(gui, gui.cb_docs, '<space>')
    assert gui.docs_var.get() is not docs

    before = gui.format_var.get()
    values = list(gui.format_menu.cget('values'))
    press(gui, gui.format_menu, '<Down>')
    assert gui.format_var.get() == values[values.index(before) + 1]
    press(gui, gui.format_menu, '<Up>')
    assert gui.format_var.get() == before

    press(gui, gui.tabview._segmented_button, '<Right>')
    assert gui.tabview.get() == gui.texts['tab_adv']['sv']
    press(gui, gui.tabview._segmented_button, '<Left>')
    assert gui.tabview.get() == gui.texts['tab_basic']['sv']

    press(gui, gui.lang_switch, '<Right>')
    assert gui.lang == 'en'
    press(gui, gui.lang_switch, '<Left>')
    assert gui.lang == 'sv'

    press(gui, gui.theme_switch, '<space>')
    assert uwc.ctk.get_appearance_mode() == 'Dark'


def selected_colors(segmented, value):
    button = segmented._buttons_dict[value]
    return button.cget('fg_color'), button.cget('text_color')


def test_selected_tab_and_language_stay_marked_after_language_change(gui):
    tabs, language = gui.tabview._segmented_button, gui.lang_switch
    selected = (gui.SEGMENT_SELECTED, gui.SEGMENT_SELECTED_TEXT)
    assert selected_colors(tabs, gui.tabview.get()) == selected
    assert selected_colors(language, 'SV') == selected
    assert selected_colors(tabs, gui.texts['tab_adv']['sv'])[1] == gui.STATUS_COLOR

    gui.tabview.set(gui.texts['tab_adv']['sv'])
    gui.change_language_event('EN')
    language.set('EN')
    # Tidigare tappade den omdöpta fliken sitt valda läge vid språkbyte.
    assert tabs.get() == gui.tabview.get() == 'Advanced'
    assert selected_colors(tabs, 'Advanced') == selected
    assert selected_colors(tabs, 'Basic settings')[1] == gui.STATUS_COLOR


@pytest.mark.parametrize('mode', [0, 1])
def test_selected_state_and_focus_ring_have_contrast(mode):
    if not uwc.HAS_GUI:
        pytest.skip('GUI-beroenden saknas')
    app = uwc.AppGUI
    assert contrast(app.SEGMENT_SELECTED[mode], app.FIELD_SURFACE[mode]) >= 3
    assert contrast(app.SEGMENT_SELECTED_TEXT[mode], app.SEGMENT_SELECTED[mode]) >= 4.5
    assert contrast(app.SEGMENT_SELECTED_TEXT[mode], app.SEGMENT_SELECTED_HOVER[mode]) >= 4.5
    for surface in (app.PANEL_SURFACE, app.WINDOW_SURFACE, app.FIELD_SURFACE):
        assert contrast(app.FOCUS_RING[mode], surface[mode]) >= 3


def test_focus_ring_follows_keyboard_focus(gui):
    press(gui, gui.start_btn, '<Shift_L>')
    ring = gui._focus_ring
    assert all(str(part.place_info().get('in')) == str(gui.start_btn) for part in ring)
    assert {part.cget('background') for part in ring} == {gui.FOCUS_RING[0]}

    gui.root.focus_force()
    gui.url_entry._entry.focus_set()
    gui.root.update()
    assert all(str(part.place_info().get('in')) == str(gui.url_entry) for part in ring)

    gui.theme_switch.select()
    gui.change_appearance_mode_event()
    # Temabytet döljer och visar fönstret kort i Windows, och fokus återställs efteråt.
    gui.root.update()
    assert {part.cget('background') for part in ring} == {gui.FOCUS_RING[1]}

    # Fokus lämnar fältet, till exempel när användaren byter fönster. Tk skickar inte
    # syntetiska fokushändelser vidare, så hanteraren anropas med samma händelsefält.
    gui._on_focus_out(SimpleNamespace(widget=gui.url_entry._entry))
    assert not any(part.winfo_ismapped() for part in ring)
    assert gui._focus_ring_target is None


def test_log_is_read_only_but_can_be_focused(gui):
    gui._log_to_gui('Första raden')
    text = gui.log_area.get('1.0', 'end')
    assert 'Första raden' in text
    assert gui.log_area.cget('state') == 'disabled'
    assert str(gui.log_area._textbox.cget('takefocus')) == '1'
    press(gui, gui.log_area._textbox, '<KeyPress-x>')
    assert gui.log_area.get('1.0', 'end') == text


def test_results_open_and_copy_with_keyboard(gui, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(uwc.webbrowser, 'open', opened)
    gui.tree.insert('', 'end', values=('https://kommun.example/a', 'OK (200)', 'Sida A'))
    gui.tree.insert('', 'end', values=('https://kommun.example/b', 'OK (200)', 'Sida B'))
    press(gui, gui.tree, '<Return>')
    # Tabellen får en aktuell rad när den får fokus, så Enter fungerar direkt.
    opened.assert_called_once_with('https://kommun.example/a')
    press(gui, gui.tree, '<Down>')
    press(gui, gui.tree, '<Control-c>')
    assert gui.root.clipboard_get() == 'https://kommun.example/b'


def test_results_arriving_after_focus_are_navigable_and_keep_selection(gui):
    tree = gui.tree
    press(gui, tree, "<Shift_L>")
    assert not tree.get_children()
    for index in range(3):
        gui.msg_queue.put(("table", (f"https://kommun.example/{index}", "OK", "Avgifter")))
    gui.process_queue()
    gui.root.update()
    assert gui.root.focus_get() is tree
    previous = tree.focus()
    tree.event_generate("<Up>")
    gui.root.update()
    assert tree.focus() and tree.focus() != previous
    selection = tree.selection()
    assert selection == (tree.focus(),)

    gui.msg_queue.put(("table", ("https://kommun.example/senaste", "OK", "Senaste")))
    gui.process_queue()
    gui.root.update()
    assert tree.selection() == selection


def test_tooltip_opens_on_focus_and_closes_with_escape(gui):
    show_tab(gui, 'tab_adv')
    press(gui, gui.cb_hybrid, '<Shift_L>')
    assert gui._tooltip_win is not None
    gui.cb_hybrid.event_generate('<Escape>')
    gui.root.update()
    assert gui._tooltip_win is None


def test_help_window_closes_with_escape_and_returns_focus(gui):
    gui.open_help_window()
    help_window, = [w for w in gui.root.winfo_children() if isinstance(w, uwc.ctk.CTkToplevel)]
    textbox, = [w for w in help_window.winfo_children() if isinstance(w, uwc.ctk.CTkTextbox)]
    # Hjälptexten tar fokus när fönstret öppnas, så att den kan rullas med tangentbordet. CustomTkinter
    # döljer och visar fönstret för titelradens färg, så fokus sätts asynkront; vänta högst tre sekunder.
    for _ in range(60):
        if help_window.focus_lastfor() is textbox._textbox:
            break
        ready = uwc.tk.BooleanVar(gui.root, False)
        gui.root.after(50, ready.set, True)
        gui.root.wait_variable(ready)
    assert help_window.focus_lastfor() is textbox._textbox
    textbox._textbox.focus_force()
    gui.root.update()
    textbox._textbox.event_generate('<Escape>')
    gui.root.update()
    assert not help_window.winfo_exists()
    # focus_get() är tomt när testfönstret inte är aktivt; fönstret minns fokus i focus_lastfor().
    assert gui.root.focus_lastfor() is gui.help_btn
