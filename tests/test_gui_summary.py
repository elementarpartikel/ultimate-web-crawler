"""Regressioner för inställningar, slutstatus, språk och körningsrapport i GUI:t."""
import gc
import json
import os
from pathlib import Path
from queue import Queue
from unittest.mock import Mock

import pytest
from aiohttp import web

from test_crawler import Site, config, page, run, serve, uwc


def test_settings_fit_in_small_window_without_scrolling(monkeypatch, request):
    if not uwc.HAS_GUI:
        pytest.skip('GUI-beroenden saknas')
    try:
        root = uwc.ctk.CTk()
    except uwc.tk.TclError:
        pytest.skip('Tk saknar en tillgänglig bildskärm')

    # Frigör Tk-variabler på huvudtråden innan nästa test startar arbetstrådar.
    request.addfinalizer(gc.collect)
    monkeypatch.setattr(uwc.AppGUI, '_load_settings', lambda self: None)
    monkeypatch.setattr(uwc.AppGUI, '_find_templates', lambda self: {'Provplats': {}})
    try:
        root.attributes('-alpha', 0)
        gui = uwc.AppGUI(root)
        root.geometry('720x450+0+0')
        root.update()
        controls = {
            'tab_basic': (
                gui.delay_entry, gui.max_pages_entry, gui.max_depth_entry, gui.format_menu,
                gui.concurrency_entry, gui.headless_menu, gui.cb_docs, gui.cb_convert_docs,
                gui.dir_entry, gui.btn_folder,
            ),
            'tab_adv': (
                gui.cb_hybrid, gui.cb_traf, gui.cb_full, gui.cb_sitemap, gui.cb_robots,
                gui.cb_strict, gui.exclude_entry, gui.require_entry, gui.lang_filter_entry,
                gui.cb_rm_email, gui.cb_rm_phone, gui.cb_rm_pnr, gui.cb_rm_ip,
            ),
        }
        for language in ('SV', 'EN'):
            gui.change_language_event(language)
            for key, widgets in controls.items():
                name = gui.texts[key][gui.lang]
                gui.tabview.set(name)
                # Flikbytet lämnar den tidigare fliken synlig i 100 ms.
                ready = uwc.tk.BooleanVar(root, False)
                root.after(120, ready.set, True)
                root.wait_variable(ready)
                root.update_idletasks()
                tab = gui.tabview.tab(name)
                for widget in widgets:
                    assert widget.winfo_viewable(), widget
                    # Kontrollera även mellanliggande ramar och en eventuell rullyta.
                    container = widget.master
                    while container is not gui.tabview:
                        x = widget.winfo_rootx() - container.winfo_rootx()
                        y = widget.winfo_rooty() - container.winfo_rooty()
                        assert 0 <= x <= container.winfo_width() - widget.winfo_width(), widget
                        assert 0 <= y <= container.winfo_height() - widget.winfo_height(), widget
                        container = container.master
                    assert widget.winfo_width() >= widget.winfo_reqwidth(), widget
                    assert widget.winfo_height() >= widget.winfo_reqheight(), widget
                assert tab.winfo_rooty() + tab.winfo_height() <= gui.start_btn.winfo_rooty()
    finally:
        root.destroy()


def finished_summary(crawler):
    messages = list(iter(crawler.msg_queue.get_nowait, ('done', 'Klar')))
    summary, = [value for kind, value in messages if kind == 'summary']
    assert messages[-1] == ('summary', summary)
    assert not any(kind == 'log' and 'Besökta sidor' in str(value) for kind, value in messages)
    return summary


@pytest.mark.parametrize('status', ['success', 'partial', 'failed'])
def test_crawler_sends_final_status_and_written_report_to_gui(tmp_path, status):
    class ReportSite(Site):
        async def handle(self, request):
            if request.path == '/blocked' or (request.path == '/' and status == 'failed'):
                return web.Response(status=403)
            return await super().handle(request)

    async def go():
        site = ReportSite()
        links = [('/blocked', 'Nekad sida')] if status == 'partial' else []
        site.pages['/'] = (page('Start', 'Kommunal information.', links), '"start"')
        runner, base = await serve(site)
        try:
            crawler = uwc.AsyncWebCrawler(config(base, tmp_path), msg_queue=Queue())
            await crawler.crawl()
            summary = finished_summary(crawler)
            assert summary.status == status
            assert summary.counts[0] == int(status != 'failed')
            assert summary.counts[-1] == int(status != 'success')
            report_path = Path(summary.report_path)
            assert report_path == tmp_path / 'crawl_report.json'
            report = json.loads(report_path.read_bytes())
            assert report['status'] == status
            assert report['run'] == crawler.crawl_session_id
        finally:
            await runner.cleanup()

    run(go())


def test_locked_output_links_to_separate_attempt_report(tmp_path):
    old_report = tmp_path / 'crawl_report.json'
    old_report.write_bytes(b'{"run": "previous"}')
    crawler = uwc.AsyncWebCrawler(config('https://test.invalid', tmp_path), msg_queue=Queue())
    owner = uwc.PublicationWorkspace(str(tmp_path), os.path.basename(crawler.db.db_path), 'other-run')
    owner.begin()
    try:
        run(crawler.crawl())
        summary = finished_summary(crawler)
        assert summary.status == 'failed'
        path = tmp_path / 'reports' / (crawler.crawl_session_id + '.json')
        assert Path(summary.report_path) == path
        assert json.loads(path.read_bytes())['run'] == crawler.crawl_session_id
        assert old_report.read_bytes() == b'{"run": "previous"}'
    finally:
        owner.abort()


def test_failed_report_write_still_emits_failed_summary(tmp_path, monkeypatch):
    original_write = uwc.atomic_write_text

    def write(path, content):
        if Path(path) == tmp_path / 'crawl_report.json':
            raise PermissionError('Testfel vid skrivning av körningsrapport')
        return original_write(path, content)

    monkeypatch.setattr(uwc, 'atomic_write_text', write)

    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Kommunal information.'), '"start"')
        runner, base = await serve(site)
        try:
            crawler = uwc.AsyncWebCrawler(config(base, tmp_path), msg_queue=Queue())
            await crawler.crawl()
            summary = finished_summary(crawler)
            assert summary.status == 'failed'
            assert summary.report_path is None
        finally:
            await runner.cleanup()

    run(go())


def stub_gui(monkeypatch):
    def build(gui):
        for name in (
                'tabview', 'headless_var', 'headless_menu', 'format_var', 'format_menu', 'tree', 'log_area',
                'lbl_url', 'help_btn', 'theme_switch', 'lbl_delay', 'lbl_max_pages', 'lbl_max_depth', 'lbl_format',
                'lbl_concurrency', 'cb_docs', 'cb_convert_docs', 'lbl_mode', 'lbl_folder', 'btn_folder',
                'cb_hybrid', 'cb_traf', 'cb_sitemap', 'cb_robots', 'cb_strict', 'lbl_exclude', 'lbl_require',
                'cb_rm_email', 'cb_rm_phone', 'cb_rm_pnr', 'cb_rm_ip', 'cb_full', 'lbl_lang_filter',
                'open_btn', 'report_btn', 'start_btn', 'pause_btn', 'stop_btn', 'stats_label', 'progress_bar',
                'text_size_menu', 'result_menu', 'log_menu'):
            setattr(gui, name, Mock())
        gui.output_labels = {}
        gui.headless_var.get.return_value = gui.texts['run_modes']['headless']['sv']
        gui.format_var.get.return_value = '.md'

    monkeypatch.setattr(uwc.AppGUI, '_build_ui', build)
    monkeypatch.setattr(uwc.AppGUI, '_load_settings', lambda self: None)
    monkeypatch.setattr(uwc.AppGUI, '_update_treeview_style', lambda self, mode: None)
    root = Mock()
    root.winfo_screenwidth.return_value = 1366
    root.winfo_screenheight.return_value = 768
    root._get_window_scaling.return_value = 1
    gui = uwc.AppGUI(root)
    gui._log_to_gui = Mock()
    return gui


@pytest.mark.parametrize(('status', 'swedish', 'english', 'colors'), [
    ('success', 'Lyckad körning', 'Successful run', ('#176536', '#75d59b')),
    ('partial', 'Partiell körning', 'Partial run', ('#835100', '#ffc46b')),
    ('failed', 'Misslyckad körning', 'Failed run', ('#a51f2b', '#ff8d94')),
])
def test_gui_summary_uses_status_colors_and_translates_again_on_language_change(
        tmp_path, monkeypatch, status, swedish, english, colors):
    gui = stub_gui(monkeypatch)
    path = tmp_path / 'crawl_report.json'
    path.write_text('{}', encoding='utf-8')
    warnings = (
        ('download_failures', 1), ('robots_unreachable', 2), ('conversion_failures', 3),
        ('short_pages', 4), ('possible_prompt_injection', 5),
    )
    fatal_error = 'Tekniskt fel från crawlern' if status == 'failed' else None
    gui.msg_queue.put(('summary', uwc.CrawlSummary(status, (11, 2, 3, 4, 5, 6), warnings, fatal_error, str(path))))
    gui.msg_queue.put(('done', 'Klar'))
    gui.process_queue()
    sv_text = gui.stats_label.configure.call_args.kwargs['text']
    assert sv_text.startswith(f'{swedish}. Besökta sidor 11, nya 2, ändrade 3, borttagna 4, dokument 5, sidfel 6.')
    assert gui.stats_label.configure.call_args.kwargs['text_color'] == colors
    gui._log_to_gui.assert_called_once_with(sv_text)
    gui.report_btn.configure.assert_called_with(state='normal')

    gui.change_language_event('🇬🇧 EN')
    en_text = gui.stats_label.configure.call_args.kwargs['text']
    assert en_text.startswith(f'{english}. Pages visited 11, new 2, updated 3, removed 4, documents 5, page errors 6.')
    assert 'Document download failures 1 · Hosts with unavailable robots.txt 2' in en_text
    assert 'Document conversion failures 3 · Pages with too little text 4' in en_text
    assert 'Sources with possible prompt injection 5' in en_text
    assert en_text.endswith('See the run report.')
    if fatal_error:
        assert 'The run ended with an error' in en_text
        assert fatal_error not in en_text
    assert gui.stats_label.configure.call_args.kwargs['text_color'] == colors
    assert any(call.kwargs == {'text': 'Open report'} for call in gui.report_btn.configure.call_args_list)


def test_gui_opens_saved_report_even_if_output_setting_changes(tmp_path, monkeypatch):
    gui = stub_gui(monkeypatch)
    path = tmp_path / 'reports' / 'attempt.json'
    path.parent.mkdir()
    path.write_text('{}', encoding='utf-8')
    gui._open_local_path = Mock()
    gui.dir_var = Mock()
    gui.dir_var.get.return_value = str(tmp_path / 'other-folder')
    gui._last_summary = uwc.CrawlSummary('failed', (0, 0, 0, 0, 0, 0), (), 'Testfel', str(path))
    gui.open_report()
    gui._open_local_path.assert_called_once_with(str(path))
    path.unlink()
    gui.open_report()
    gui._open_local_path.assert_called_once()
    gui.report_btn.configure.assert_called_with(state='disabled')
    gui._log_to_gui.assert_called_once_with('Körningsrapporten finns inte längre.')
