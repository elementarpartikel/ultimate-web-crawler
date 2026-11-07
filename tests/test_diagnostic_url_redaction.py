"""Regressioner för URL-maskering vid loggning och publicering."""
import json
from queue import Queue

import pytest
from aiohttp import web

from crawler_text import redact_diagnostic_urls
from test_crawler import Site, config, page, published, read_changes, run, run_crawl, serve, uwc


@pytest.mark.parametrize(('raw', 'expected'), [
    ('https://example.invalid/p?anything=secret&another=value', 'https://example.invalid/p'),
    ('https://example.invalid/p?secret_name_only', 'https://example.invalid/p'),
    ('https://example.invalid/p#access_token=secret', 'https://example.invalid/p'),
    ('HTTPS://login:secret@[::1]:444/path?SAMLResponse=secret', 'HTTPS://[::1]:444/path'),
    ("Fel för https://example.invalid/p?q=se'cret vid hämtning", 'Fel för https://example.invalid/p vid hämtning'),
    ('https://example.invalid/p?next=https://other.invalid/?signed=secret', 'https://example.invalid/p'),
    ('https://example.invalid/a%3Fb%23c;v=1', 'https://example.invalid/a%3Fb%23c;v=1'),
])
def test_removes_url_components_without_decoding_the_path(raw, expected):
    assert redact_diagnostic_urls(raw) == expected
    assert redact_diagnostic_urls(expected) == expected


def test_real_crawl_masks_diagnostics_but_fetches_and_identifies_query_variants(tmp_path, capsys):
    class QuerySite(Site):
        async def handle(self, request):
            if request.path == '/variant':
                value = request.query['which']
                self.variants.append(value)
                return web.Response(text=page('Variant', f'Kommunala avgifter för variant {value[-1]}'),
                                    content_type='text/html')
            if request.path == '/redirect':
                target = request.host.replace('127.0.0.1', 'localhost')
                raise web.HTTPFound(f'http://{target}/login?SAMLResponse=saml-secret#access_token=fragment-secret')
            if request.path == '/forbidden':
                raise web.HTTPForbidden()
            return await super().handle(request)

    async def go():
        site = QuerySite()
        site.variants = []
        site.pages['/'] = (page('Start', 'Kommunal testsida', [
            ('/variant?which=variant-secret-a', 'Första avgiften'),
            ('/variant?which=variant-secret-b', 'Andra avgiften'),
            ('/redirect?callback=redirect-secret', 'Länk med omdirigering'),
            ('/forbidden?request=log-secret', 'Spärrad sida'),
        ]), None)
        runner, base = await serve(site)
        try:
            crawler = await run_crawl(base, tmp_path, find_sitemap=False)
            assert crawler.delivery_status() == 'partial'
            assert sorted(site.variants) == ['variant-secret-a', 'variant-secret-b']
            generation = published(tmp_path)
            assert 'variant-secret-a' in (generation / 'chunks.jsonl').read_text(encoding='utf-8')
            assert 'variant-secret-b' in (generation / 'chunks.jsonl').read_text(encoding='utf-8')
            changes = [change for change in read_changes(generation) if change['url'] == base + '/variant']
            assert len(changes) == 2 and len({change['file'] for change in changes}) == 2
            for report_dir in (generation, tmp_path):
                report = json.loads((report_dir / 'crawl_report.json').read_text(encoding='utf-8'))
                assert report['off_domain_redirects'] == [{
                    'url': base + '/redirect', 'final_url': base.replace('127.0.0.1', 'localhost') + '/login',
                }]
            log = next((tmp_path / 'logs').glob('crawl_*.log')).read_text(encoding='utf-8')
            console = capsys.readouterr().out
            assert base + '/forbidden' in log and base + '/forbidden' in console
            saved_diagnostics = [log, console]
            for directory in (generation, tmp_path):
                for name in ('crawl_report.json', 'changes.jsonl'):
                    saved_diagnostics.append((directory / name).read_text(encoding='utf-8'))
            secrets = ('variant-secret-a', 'variant-secret-b', 'redirect-secret', 'saml-secret',
                       'fragment-secret', 'log-secret')
            assert all(secret not in diagnostic for diagnostic in saved_diagnostics for secret in secrets)
        finally:
            await runner.cleanup()

    run(go())


def test_start_failure_masks_exception_urls_in_attempt_report_and_gui_log(tmp_path, monkeypatch):
    async def fail(self):
        raise RuntimeError('Fel vid https://example.invalid/login?token=synthetic-secret')

    monkeypatch.setattr(uwc.AsyncWebCrawler, '_begin_publication', fail)
    history = tmp_path / 'changes.jsonl'
    legacy_line = b'{"url":"https://example.invalid/legacy?token=history-secret"}\n'
    history.write_bytes(legacy_line)
    messages = Queue()
    crawler = uwc.AsyncWebCrawler(config('http://127.0.0.1', tmp_path), msg_queue=messages)
    run(crawler.crawl())
    report = json.loads(next((tmp_path / 'reports').glob('*.json')).read_text(encoding='utf-8'))
    assert report['fatal_error'] == 'RuntimeError: Fel vid https://example.invalid/login'
    events = list(iter(messages.get_nowait, ('done', 'Klar')))
    assert any('https://example.invalid/login' in msg for kind, msg in events if kind == 'log')
    assert 'synthetic-secret' not in repr(events)
    assert history.read_bytes() == legacy_line


def test_publication_failure_masks_user_info_before_truncating_error(tmp_path, monkeypatch):
    def fail(self, status):
        password = 'long-secret-' * 20
        raise RuntimeError(f'Fel vid https://user:{password}@example.invalid/upload?key=upload-secret')

    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Kommunal testsida'), None)
        runner, base = await serve(site)
        try:
            monkeypatch.setattr(uwc.PublicationWorkspace, 'publish', fail)
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert result.delivery_status() == 'failed'
            report_text = (tmp_path / 'crawl_report.json').read_text(encoding='utf-8')
            report = json.loads(report_text)
            assert report['export_failures'][0]['reason'] == 'Fel vid https://example.invalid/upload'
            assert 'long-secret' not in report_text and 'upload-secret' not in report_text
            assert not (tmp_path / 'current.json').exists()
        finally:
            await runner.cleanup()

    run(go())


def test_old_change_history_is_masked_even_without_new_events(tmp_path, monkeypatch):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Kommunal testsida', [('/taxa?x=history-secret', 'Taxa')]), 'start')
        site.pages['/taxa'] = (page('Taxa', 'Kommunala avgifter'), 'taxa')
        runner, base = await serve(site)
        try:
            with monkeypatch.context() as legacy:
                legacy.setattr(uwc, 'redact_diagnostic_data', lambda data: data)
                legacy.setattr(uwc, 'DIAGNOSTIC_URL_VERSION', 0)
                await run_crawl(base, tmp_path, find_sitemap=False)
            old = published(tmp_path)
            old_bytes = (old / 'changes.jsonl').read_bytes()
            assert b'history-secret' in old_bytes

            crawler = await run_crawl(base, tmp_path, find_sitemap=False)
            assert not crawler.changes
            new = published(tmp_path)
            assert new != old
            assert (old / 'changes.jsonl').read_bytes() == old_bytes
            expected = [{**change, 'url': change['url'].split('?')[0]} for change in read_changes(old)]
            assert read_changes(new) == expected == read_changes(tmp_path)
            assert json.loads((new / 'crawl_report.json').read_text(encoding='utf-8'))['url_diagnostics_version'] == 1
        finally:
            await runner.cleanup()

    run(go())
