"""Regressioner för batchexport, körstatistik och serverns intranätpolicy."""
import asyncio
import json
from queue import Queue
from unittest.mock import Mock

import pytest
from aiohttp import web

from test_crawler import Site, config, page, run, run_crawl, serve, uwc


@pytest.mark.parametrize('save_format', ['.md', '.txt'])
def test_replay_and_export_use_bulk_cache_and_skip_chunking_for_language(tmp_path, monkeypatch, save_format):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Kommunal information', [('/taxa', 'Taxa')]), 'start')
        site.pages['/taxa'] = (page('Taxa', 'Avgifter för kommunala tjänster'), 'taxa')
        runner, base = await serve(site)
        options = dict(find_sitemap=False, save_format=save_format, languages=['sv'])
        try:
            await run_crawl(base, tmp_path, **options)
            original = uwc.AsyncCrawlDatabase.get_cache
            calls = []

            async def count(db, url):
                calls.append(url)
                return await original(db, url)

            def unexpected(*args, **kwargs):
                pytest.fail('Språkfiltret ska läsa metadata utan att bygga chunks')

            monkeypatch.setattr(uwc.AsyncCrawlDatabase, 'get_cache', count)
            monkeypatch.setattr(uwc, 'markdown_file_to_chunks', unexpected)
            monkeypatch.setattr(uwc, 'plain_file_to_chunks', unexpected)
            result = await run_crawl(base, tmp_path, export_jsonl=False, **options)
            assert result.delivery_status() == 'success'
            assert sorted(calls) == [base + '/', base + '/taxa']
            assert len(list((tmp_path / 'texter').glob('*' + save_format))) == 2
        finally:
            await runner.cleanup()

    run(go())


def test_content_query_parameters_can_be_preserved_per_site(tmp_path):
    class QuerySite(Site):
        async def handle(self, request):
            if request.path == '/taxa':
                value = next(iter(request.query.values()))
                return web.Response(text=page('Taxa', f'Kommunal taxa {value}'), content_type='text/html')
            return await super().handle(request)

    async def go():
        site = QuerySite()
        parameters = ['ref', 'source', 'state']
        site.pages['/'] = (page('Start', 'Kommunal information', [
            (f'/taxa?{key}={i}', f'Taxa {i}') for i, key in enumerate(parameters, 1)
        ]), None)
        runner, base = await serve(site)
        try:
            result = await run_crawl(base, tmp_path, keep_query_params=parameters, find_sitemap=False)
            assert result.stats.pages_visited == 4
            urls = {json.loads(line)['url'] for line in (tmp_path / 'chunks.jsonl').read_text(encoding='utf-8').splitlines()}
            assert all(f'{base}/taxa?{key}={i}' in urls for i, key in enumerate(parameters, 1))
        finally:
            await runner.cleanup()

    run(go())


def test_gui_counts_fetched_and_cached_pages_once(tmp_path):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Kommunal information', [('/taxa', 'Taxa')]), 'start')
        site.pages['/taxa'] = (page('Taxa', 'Avgifter för kommunala tjänster'), 'taxa')
        runner, base = await serve(site)
        try:
            for expected in ((2, 2, 0, 0), (2, 0, 0, 2)):
                messages = Queue()
                crawler = uwc.AsyncWebCrawler(config(base, tmp_path, find_sitemap=False), msg_queue=messages)
                await asyncio.wait_for(crawler.crawl(), 60)
                assert crawler.delivery_status() == 'success'
                events = list(iter(messages.get_nowait, ('done', 'Klar')))
                for index, (kind, _) in enumerate(events):
                    if kind == 'table':
                        assert events[index + 1][1][0] >= 1
                stats = [data for kind, data in events if kind == 'stats_data']
                assert stats[-1][:4] == expected
                assert ('phase', 'phase_exporting') in events
        finally:
            await runner.cleanup()

    run(go())


def test_gui_close_waits_for_crawler_without_blocking_window(monkeypatch):
    gui = uwc.AppGUI.__new__(uwc.AppGUI)
    gui._closing = False
    gui.lang = 'sv'
    gui.root = Mock()
    gui.texts = {'window_title': {'sv': 'Crawler'}, 'confirm_close': {'sv': 'Stoppa?'}}
    gui.crawl_thread = Mock()
    gui.crawl_thread.is_alive.return_value = True
    gui._save_settings = Mock()
    gui.stop_crawl = Mock()
    confirm = Mock(side_effect=[False, True])
    monkeypatch.setattr(uwc.messagebox, 'askyesno', confirm)
    gui._on_closing()
    gui.stop_crawl.assert_not_called()
    gui._on_closing()
    gui.stop_crawl.assert_called_once()
    gui.crawl_thread.join.assert_not_called()
    gui.root.destroy.assert_not_called()
    gui.crawl_thread.is_alive.return_value = False
    gui.root.after.call_args.args[1]()
    gui.root.destroy.assert_called_once()


@pytest.mark.parametrize(('configured', 'code', 'reason'), [
    (None, 1, 'server_public_default'), (False, 1, 'explicit_deny'), (True, 0, 'explicit_allow'),
])
def test_cli_requires_explicit_private_host_permission(tmp_path, configured, code, reason):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Privat lokal sida'), None)
        runner, base = await serve(site)
        try:
            settings = {'name': 'test', 'start_url': base, 'find_sitemap': False, 'respect_robots': False}
            if configured is not None:
                settings['allow_private_hosts'] = configured
            path = tmp_path / 'config.json'
            path.write_text(json.dumps(settings), encoding='utf-8')
            assert await uwc.run_cli_mode(str(path), output_dir=str(tmp_path / 'out')) == code
            report = json.loads((tmp_path / 'out/test/crawl_report.json').read_text(encoding='utf-8'))
            assert report['network_policy'] == {'allow_private_hosts': configured is True, 'reason': reason}
            assert bool(site.hits) == (configured is True)
        finally:
            await runner.cleanup()

    run(go())
