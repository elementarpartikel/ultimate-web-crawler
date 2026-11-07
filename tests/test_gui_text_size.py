"""Regressioner för textstorlek och rullyta i GUI:t (DESIGNPLAN-WCAG.md, del 2)."""
import json

import pytest

from test_crawler import uwc
from test_gui_keyboard import press
from test_gui_typography import font_of, gui, style_font  # noqa: F401  (gui är en fixtur)

pytestmark = pytest.mark.skipif(
    not uwc.HAS_GUI or int(uwc.ctk.__version__.split('.')[0]) < 6,
    reason='Kräver CustomTkinter 6 enligt requirements.txt')


def settle(gui, rounds=4):
    for _ in range(rounds):
        gui.root.update()


def bars(gui):
    return bool(gui._vbar_box.winfo_ismapped()), bool(gui._hbar_box.winfo_ismapped())


def test_text_size_menu_and_shortcuts_scale_all_text(gui):
    base = gui._ui_scale
    gui._on_text_size_chosen('Text 150 %')
    settle(gui)
    assert gui.text_scale == 1.5
    assert gui.text_size_menu.get() == 'Text 150 %'
    label = font_of(gui.lbl_delay._label)[1]
    assert label == round(14 * base * 1.5)
    assert style_font(gui, 'Treeview.Heading')[1] == style_font(gui, 'Treeview')[1] == label

    press(gui, gui.url_entry._entry, '<Control-plus>')
    assert gui.text_scale == 1.75
    press(gui, gui.url_entry._entry, '<Control-minus>')
    assert gui.text_scale == 1.5
    press(gui, gui.url_entry._entry, '<Control-Key-0>')
    assert gui.text_scale == 1.0
    assert font_of(gui.lbl_delay._label)[1] == round(14 * base)

    # Menyn stegar också med piltangenterna.
    press(gui, gui.text_size_menu, '<Down>')
    assert gui.text_scale == 1.25
    gui.change_language_event('EN')
    assert gui.text_size_menu.get() == 'Text 125%'


def test_normal_text_fits_without_scrolling(gui):
    gui.root.geometry('1000x680')
    settle(gui)
    assert bars(gui) == (False, False)


@pytest.mark.parametrize("size, zoomed_bars, normal_bars", [
    ("1000x680", (True, False), (False, False)),
    ("720x450", (True, True), (True, False)),
])
def test_large_text_scrolls_instead_of_clipping_and_follows_focus(gui, size, zoomed_bars, normal_bars):
    gui.root.geometry(size)
    settle(gui)
    gui._apply_text_scale(2.0)
    settle(gui)
    assert bars(gui) == zoomed_bars
    viewport = gui._scroll_canvas
    # Starta ligger utanför bild vid 200 %. Fokus rullar fram den.
    press(gui, gui.start_btn, '<Shift_L>')
    settle(gui)
    top = gui.start_btn.winfo_rooty() - viewport.winfo_rooty()
    left = gui.start_btn.winfo_rootx() - viewport.winfo_rootx()
    assert 0 <= top and top + gui.start_btn.winfo_height() <= viewport.winfo_height()
    assert 0 <= left and left + gui.start_btn.winfo_width() <= viewport.winfo_width()

    gui._apply_text_scale(1.0)
    settle(gui)
    assert bars(gui) == normal_bars


def test_zoom_keeps_the_already_focused_control_visible(gui):
    target = gui.btn_folder
    press(gui, target, "<Shift_L>")
    for _ in range(4):
        target.event_generate("<Control-plus>")
        settle(gui)
    ready = uwc.tk.BooleanVar(gui.root, False)
    gui.root.after(100, ready.set, True)
    gui.root.wait_variable(ready)
    assert gui.text_scale == 2.0
    assert gui.root.focus_get() is target
    canvas = gui._scroll_canvas
    top = target.winfo_rooty() - canvas.winfo_rooty()
    left = target.winfo_rootx() - canvas.winfo_rootx()
    assert 0 <= top <= canvas.winfo_height() - target.winfo_height()
    assert 0 <= left <= canvas.winfo_width() - target.winfo_width()


def test_text_size_is_saved(gui, tmp_path, monkeypatch):
    gui._apply_text_scale(1.25)
    saved = gui._collect_settings()
    assert saved['text_scale'] == 1.25

    gui._apply_text_scale(1.0)
    # Fixturen har stängt av inläsningen; återställ den och läs en provfil.
    monkeypatch.undo()
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'text_scale': 1.75}), encoding='utf-8')
    monkeypatch.setattr(uwc.AppGUI, 'SETTINGS_PATH', str(path))
    gui._load_settings()
    assert gui.text_scale == 1.75

    # Ogiltiga värden ignoreras.
    path.write_text(json.dumps({'text_scale': 3}), encoding='utf-8')
    gui._load_settings()
    assert gui.text_scale == 1.75
