"""Regressioner för GUI:ts gemensamma typografi, skalning och fältkontrast (DESIGNPLAN-WCAG.md, del 1)."""
import gc

import pytest

from test_crawler import uwc


def contrast(first, second):
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.fixture
def gui(monkeypatch, request):
    if not uwc.HAS_GUI:
        pytest.skip('GUI-beroenden saknas')
    try:
        root = uwc.ctk.CTk()
    except uwc.tk.TclError:
        pytest.skip('Tk saknar en tillgänglig bildskärm')
    request.addfinalizer(gc.collect)
    monkeypatch.setattr(uwc.AppGUI, '_load_settings', lambda self: None)
    monkeypatch.setattr(uwc.AppGUI, '_find_templates', lambda self: {})
    root.attributes('-alpha', 0)
    app = uwc.AppGUI(root)
    root.geometry('1000x680+0+0')
    root.update()
    yield app
    # Skalning och tema är globala i CustomTkinter och får inte läcka till nästa test.
    uwc.ctk.set_widget_scaling(1.0)
    uwc.ctk.set_appearance_mode('Light')
    try:
        root.destroy()
    except uwc.tk.TclError:
        pass


def font_of(inner):
    """Familj och pixelstorlek för den inre Tk-kontrollen i en CustomTkinter-kontroll."""
    family, size = inner.tk.splitlist(inner.cget('font'))[:2]
    return family, -int(size)


def style_font(gui, style):
    name = uwc.ttk.Style(gui.root).lookup(style, 'font')
    font = uwc.tkfont.nametofont(name, root=gui.root)
    return font.cget('family'), -font.cget('size')


def table_state(gui):
    return {
        'panel': font_of(gui.output_labels['results_title']._label)[1],
        'label': font_of(gui.lbl_delay._label)[1],
        'heading': style_font(gui, 'Treeview.Heading')[1],
        'cell': style_font(gui, 'Treeview')[1],
        'rowheight': int(uwc.ttk.Style(gui.root).lookup('Treeview', 'rowheight')),
        'widths': {column: gui.tree.column(column, 'width') for column in gui.TREE_COLUMN_WIDTHS},
    }


def test_table_follows_text_size_with_the_same_factor_as_other_text(gui):
    """Tabellen räknade tidigare från fönsterskalningen och låg kvar när textstorleken ändrades."""
    gui.tree.insert('', 0, iid='rad', values=('https://example.se/a', 'OK (200)', 'Sida A'))
    gui.tree.selection_set('rad')
    gui.url_entry.delete(0, 'end')
    gui.url_entry.insert(0, 'https://example.se/start')
    # Ett fönster där det breda läget ryms även vid 150 %, så att bara textstorleken ändras och
    # inte layouten (kompakt läge med rullningslist gör tabellen smalare).
    gui.root.geometry('1400x900')
    gui.root.update()
    gui.tree.column('URL', width=500)          # användaren har breddat kolumnen
    gui.root.update()                          # Tk fördelar om kolumnerna efter tabellens bredd
    base = gui._ui_scale
    before = table_state(gui)
    assert before['heading'] == before['panel'] == before['label'] == before['cell'] == round(14 * base)

    uwc.ctk.set_widget_scaling(1.5)
    gui.root.update()
    after = table_state(gui)
    scaled = base * 1.5
    assert gui._ui_scale == pytest.approx(scaled)
    assert after['panel'] == round(14 * scaled)
    assert after['heading'] == after['cell'] == after['panel'] == after['label']
    assert after['rowheight'] > before['rowheight']
    # Statuskolumnen har fast bredd och växer med texten. Tk låter de utdragbara
    # kolumnerna (Sidtitel och URL) dela på resten av tabellens oförändrade bredd.
    assert after['widths']['Status'] == round(before['widths']['Status'] / base * scaled)
    assert sum(after['widths'].values()) == pytest.approx(gui.tree.winfo_width(), abs=2)
    assert gui.tree.selection() == ('rad',)
    assert gui.url_entry.get() == 'https://example.se/start'

    uwc.ctk.set_widget_scaling(1.0)
    gui.root.update()
    restored = table_state(gui)
    assert restored['heading'] == before['heading']
    assert restored['rowheight'] == before['rowheight']
    # Användarens egen kolumnbredd finns kvar efter att textstorleken återställts.
    for column, width in before['widths'].items():
        assert restored['widths'][column] == pytest.approx(width, abs=2), column


def test_table_follows_dpi_change_without_theme_switch(gui):
    """Ett DPI-byte (t.ex. flytt till en annan skärm) ska skala tabellen tillsammans med resten."""
    tracker = uwc.ctk.ScalingTracker
    root = gui.root
    dpi = tracker.window_dpi_scaling_dict[root]
    before = table_state(gui)
    other_dpi = 1.0 if dpi != 1.0 else 1.5
    try:
        # Samma anrop som CustomTkinter gör när fönstret hamnar på en skärm med annan skalning.
        tracker.window_dpi_scaling_dict[root] = other_dpi
        tracker.update_scaling_callbacks_for_window(root)
        moved = table_state(gui)
        assert moved['panel'] == round(14 * other_dpi)
        assert moved['heading'] == moved['cell'] == moved['panel']
        assert moved['widths']['Status'] == round(before['widths']['Status'] / dpi * other_dpi)
    finally:
        tracker.window_dpi_scaling_dict[root] = dpi
        tracker.update_scaling_callbacks_for_window(root)
    assert table_state(gui)['heading'] == before['heading']


def test_one_font_family_in_all_parts_of_the_window(gui):
    families = set(uwc.tkfont.families(gui.root))
    expected = 'Segoe UI' if 'Segoe UI' in families else uwc.CTK_THEME_FONT['family']
    assert gui.ui_font_family == expected
    assert uwc.ctk.ThemeManager.theme['CTkFont']['family'] == expected
    inner_widgets = {
        'fältetikett': gui.lbl_delay._label,
        'startadress': gui.lbl_url._label,
        'knapp': gui.start_btn._text_label,
        'kryssruta': gui.cb_docs._text_label,
        'panelrubrik': gui.output_labels['log_title']._label,
        'tomvy': gui.output_labels['log_empty']._label,
        'körstatus': gui.stats_label._label,
        'logg': gui.log_area._textbox,
    }
    for part, inner in inner_widgets.items():
        assert font_of(inner)[0] == expected, part
    assert style_font(gui, 'Treeview')[0] == expected
    assert style_font(gui, 'Treeview.Heading')[0] == expected
    # Loggen och tomvyerna använder brödtextens storlek, inte Consolas 11 eller storlek 12.
    assert font_of(gui.log_area._textbox)[1] == font_of(gui.lbl_delay._label)[1]
    assert font_of(gui.output_labels['results_empty']._label)[1] == font_of(gui.lbl_delay._label)[1]

    gui.open_help_window()
    help_window, = [w for w in gui.root.winfo_children() if isinstance(w, uwc.ctk.CTkToplevel)]
    textbox, = [w for w in help_window.winfo_children() if isinstance(w, uwc.ctk.CTkTextbox)]
    assert font_of(textbox._textbox)[0] == expected
    help_window.destroy()


def test_choose_ui_font_family_falls_back_to_bundled_font():
    choose = uwc.AppGUI.choose_ui_font_family
    assert choose({'Segoe UI', 'Arial'}, 'Segoe UI', 'Roboto') == 'Segoe UI'
    # Namnet "Segoe UI Variable" finns inte i Tk och får inte räknas som Segoe UI.
    assert choose({'Segoe UI Variable Text', 'Arial'}, 'Segoe UI', 'Roboto') == 'Roboto'


def test_tooltip_uses_theme_colors_body_font_and_scaled_width(gui):
    gui._show_tooltip(gui.exclude_entry, 'exclude')
    frame, = gui._tooltip_win.winfo_children()
    label, = frame.winfo_children()
    assert str(label.cget('font')) == gui.tk_fonts['body'].name
    assert label.cget('background') == gui.TOOLTIP_SURFACE[0]
    assert label.cget('foreground') == gui.STATUS_COLOR[0]
    assert frame.cget('background') == gui.FIELD_BORDER[0]
    assert int(label.cget('wraplength')) == round(380 * gui._ui_scale)

    gui.theme_switch.select()
    gui.change_appearance_mode_event()
    gui._show_tooltip(gui.exclude_entry, 'exclude')
    frame, = gui._tooltip_win.winfo_children()
    label, = frame.winfo_children()
    assert label.cget('background') == gui.TOOLTIP_SURFACE[1]
    assert label.cget('foreground') == gui.STATUS_COLOR[1]
    assert frame.cget('background') == gui.FIELD_BORDER[1]
    gui._hide_tooltip()


@pytest.mark.parametrize('mode', [0, 1])
def test_field_borders_meet_non_text_contrast(mode):
    if not uwc.HAS_GUI:
        pytest.skip('GUI-beroenden saknas')
    gui = uwc.AppGUI
    assert contrast(gui.FIELD_BORDER[mode], gui.FIELD_SURFACE[mode]) >= 3
    assert contrast(gui.FIELD_BORDER[mode], gui.PANEL_SURFACE[mode]) >= 3
    # Verktygstipsets kant och text ska också vara tydliga mot tipsets yta.
    assert contrast(gui.FIELD_BORDER[mode], gui.TOOLTIP_SURFACE[mode]) >= 3
    assert contrast(gui.STATUS_COLOR[mode], gui.TOOLTIP_SURFACE[mode]) >= 4.5


def test_entries_use_the_field_border(gui):
    for entry in (gui.url_entry, gui.delay_entry, gui.exclude_entry, gui.dir_entry):
        assert entry.cget('border_color') == gui.FIELD_BORDER


def test_closed_window_ignores_later_scaling_changes(gui):
    gui.root.destroy()
    # Ett annat fönster eller test kan ändra skalningen efteråt.
    uwc.ctk.set_widget_scaling(1.25)
    uwc.ctk.set_widget_scaling(1.0)
