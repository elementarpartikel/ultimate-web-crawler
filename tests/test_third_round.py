"""Driftstatus, robots per origin, artighet och strömmande browser-dokument."""
import asyncio
import json
import queue
import socket
import struct
import sys
import time

import pytest
from aiohttp import web

from test_crawler import Site, config, make_pdf, page, run, run_crawl, serve, texts, uwc
from test_advanced import needs_chromium


@pytest.mark.parametrize('status', [200, 401, 403, 404, 418, 429, 500, 503])
def test_robots_response_policy(tmp_path, status):
    class RobotsSite(Site):
        async def handle(self, request):
            if request.path == '/robots.txt':
                self.hits.append((request.path, False, request.method))
                return web.Response(status=status, text='')
            return await super().handle(request)
    async def go():
        site = RobotsSite()
        site.pages['/'] = (page('Start', 'ROBOTS TESTTEXT'), None)
        runner, base = await serve(site)
        try:
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            if status >= 500:
                assert not any(hit[0] == '/' for hit in site.hits)
                assert result.delivery_status() == 'failed'
                assert result.report['robots_unreachable']
            else:
                assert result.stats.pages_visited == 1
                assert result.delivery_status() == 'success'
            assert sum(hit[0] == '/robots.txt' for hit in site.hits) == (3 if status >= 500 or status == 429 else 1)
        finally:
            await runner.cleanup()
    run(go())


def test_robots_network_failure_denies_origin_and_is_reported(tmp_path, monkeypatch):
    async def go():
        c = uwc.AsyncWebCrawler(config('https://test.se', tmp_path))
        calls = []
        async def failed(url, **kw):
            calls.append(url)
            return None
        monkeypatch.setattr(c, 'fetch', failed)
        try:
            assert not await c._robots_allowed('https://test.se/a')
            assert not await c._robots_allowed('https://test.se/b')
            assert calls == ['https://test.se/robots.txt']
            assert c.delivery_status() == 'failed'
        finally:
            c._close_log_handlers()
    run(go())


def test_robots_rules_and_crawl_delay_are_separate_per_origin(tmp_path, monkeypatch):
    async def go():
        c = uwc.AsyncWebCrawler(config('https://test.se', tmp_path, include_subdomains=True))
        calls = []
        async def robots(url, **kw):
            calls.append(url)
            text = ('User-agent: *\nDisallow: /secret\nCrawl-delay: 3\n'
                    if 'sub.test.se' in url else 'User-agent: *\nAllow: /\n')
            return uwc.FetchResult(status=200, text=text)
        monkeypatch.setattr(c, 'fetch', robots)
        try:
            assert await c._robots_allowed('https://test.se/secret')
            result = await asyncio.gather(*[
                c._robots_allowed('https://sub.test.se/secret') for _ in range(5)])
            assert result == [False] * 5
            assert not c.is_valid_url('https://sub.test.se/secret')
            assert c.is_valid_url('https://test.se/secret')
            assert calls.count('https://sub.test.se/robots.txt') == 1
            assert c.rate_limiter.domain_minimums['sub.test.se'] == 3
            assert 'test.se' not in c.rate_limiter.domain_minimums
            assert await c._robots_allowed('http://test.se/secret')
            assert calls[-1] == 'http://test.se/robots.txt'
        finally:
            c._close_log_handlers()
    run(go())


def test_start_page_is_checked_against_robots(tmp_path):
    async def go():
        site = Site()
        site.robots = 'User-agent: *\nDisallow: /\n'
        site.pages['/'] = (page('Start', 'SPÄRRAD'), None)
        runner, base = await serve(site)
        try:
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert not any(hit[0] == '/' for hit in site.hits)
            assert not texts(tmp_path)
            assert result.report['robots_denied']
            assert result.delivery_status() == 'success'  # Avsiktlig regel, inget nätverksfel.
        finally:
            await runner.cleanup()
    run(go())


@pytest.mark.parametrize('mode', ['http', 'browser', 'document'])
def test_redirect_is_checked_before_requesting_robots_denied_target(tmp_path, mode):
    if mode == 'browser':
        # Samma tillgänglighetskontroll som övriga riktiga Chromium-tester.
        if needs_chromium.args[0]:
            pytest.skip(needs_chromium.kwargs['reason'])
    class RedirectSite(Site):
        async def handle(self, request):
            if request.path in ('/redirect', '/document.pdf'):
                self.hits.append((request.path, False, request.method))
                raise web.HTTPFound('/secret')
            return await super().handle(request)
    async def go():
        site = RedirectSite()
        site.robots = 'User-agent: *\nDisallow: /secret\n'
        site.pages['/'] = (page('Start', 'Starttext'), None)
        site.pages['/secret'] = (page('Hemligt', 'OTILLÅTEN HÄMTNING'), None)
        runner, base = await serve(site)
        c = uwc.AsyncWebCrawler(config(base, tmp_path), queue.Queue())
        try:
            await c.db.connect()
            await c._resolve_private_policy()
            c.req_session = await c._create_session()
            if mode == 'http':
                assert await c.fetch(base + '/redirect') is None
            elif mode == 'browser':
                assert not (await c._render_response_with_playwright(base + '/redirect')).text
            else:
                await c.download_document(base + '/document.pdf')
            assert not any(hit[0] == '/secret' for hit in site.hits)
            assert any(item['url'] == base + '/secret' for item in c.report['robots_denied'])
        finally:
            await c._shutdown()
            c._close_log_handlers()
            await runner.cleanup()
    run(go())


def test_redirect_to_another_origin_loads_that_origins_robots(tmp_path):
    async def go():
        other = Site()
        other.robots = 'User-agent: *\nDisallow: /\n'
        other.pages['/secret'] = (page('Hemligt', 'Hemligt'), None)
        other_runner, other_base = await serve(other)
        class RedirectSite(Site):
            async def handle(self, request):
                if request.path == '/redirect':
                    raise web.HTTPFound(other_base + '/secret')
                return await super().handle(request)
        site = RedirectSite()
        site.pages['/'] = (page('Start', 'Starttext', [('/redirect', 'Annan värd')]), None)
        runner, base = await serve(site)
        try:
            c = await run_crawl(base, tmp_path, find_sitemap=False)
            assert ('/robots.txt', False, 'GET') in other.hits
            assert not any(hit[0] == '/secret' for hit in other.hits)
            assert len(c._robots_rules) == 2
        finally:
            await runner.cleanup()
            await other_runner.cleanup()
    run(go())


def test_sitemaps_are_discovered_when_robots_rules_are_disabled(tmp_path):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Starttext'), None)
        site.pages['/hidden'] = (page('Sitemap', 'ENDast via sitemap'), None)
        site.sitemap_paths = ['/hidden']
        site.robots = 'User-agent: *\nDisallow: /\nSitemap: /sitemap.xml\n'
        runner, base = await serve(site)
        try:
            c = await run_crawl(base, tmp_path, respect_robots=False)
            assert c.stats.pages_visited == 2
            assert ('/sitemap.xml', False, 'GET') in site.hits
        finally:
            await runner.cleanup()
    run(go())


def test_concurrent_sitemaps_share_rate_limit(tmp_path):
    class SitemapSite(Site):
        def __init__(self):
            super().__init__()
            self.times = []
        async def handle(self, request):
            if request.path.startswith('/sm'):
                self.times.append(time.monotonic())
                return web.Response(text='<urlset/>', content_type='application/xml')
            return await super().handle(request)
    async def go():
        site = SitemapSite()
        runner, base = await serve(site)
        c = uwc.AsyncWebCrawler(config(base, tmp_path, delay=0.08))
        try:
            await c._resolve_private_policy()
            c.req_session = await c._create_session()
            await asyncio.gather(*[c._parse_sitemap(base + f'/sm{i}.xml') for i in range(5)])
            intervals = [b - a for a, b in zip(site.times, site.times[1:])]
            assert len(site.times) == 5
            assert min(intervals) >= 0.065
        finally:
            await c._shutdown()
            c._close_log_handlers()
            await runner.cleanup()
    run(go())


def test_adaptive_delay_recovers_without_crossing_configured_or_robots_minimum():
    async def go():
        limiter = uwc.PerDomainRateLimiter(20)
        limiter.set_minimum('other', 3)
        limiter.slow_down()
        assert limiter.delay > limiter.base_delay
        for _ in range(50):
            limiter.succeeded()
        assert limiter.delay == limiter.base_delay == 0.05
        assert limiter.domain_minimums['other'] == 3
        slow = uwc.PerDomainRateLimiter(1 / 20)
        slow.slow_down()
        assert slow.delay >= 20
    run(go())


def test_proxy_socket_is_closed_even_when_shutdown_raises_reset():
    class ResetSocket:
        closed = False
        def fileno(self):
            return -1 if self.closed else 123
        def shutdown(self, how):
            raise ConnectionResetError('Återställd anslutning')
        def close(self):
            self.closed = True
    sock = ResetSocket()
    writer = uwc._ProxySocketWriter(sock)
    writer.close()
    writer.close()
    assert sock.closed


def test_proxy_closes_after_repeated_client_resets():
    async def go():
        proxy = uwc.BrowserEgressProxy()
        await proxy.start()
        port = uwc.urlparse(proxy.url).port
        clients = []
        try:
            for _ in range(20):
                client = socket.socket()
                client.setblocking(False)
                await asyncio.get_running_loop().sock_connect(client, ('127.0.0.1', port))
                clients.append(client)
                await asyncio.get_running_loop().sock_sendall(client, b'\x05\x01\x00')
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                  struct.pack('HH' if sys.platform == 'win32' else 'ii', 1, 0))
                client.close()
            await asyncio.wait_for(proxy.close(), timeout=2)
            assert not proxy.tasks and not proxy.writers
            assert proxy.server.fileno() == -1
            assert proxy._accept_task is None
        finally:
            for client in clients:
                client.close()
            await proxy.close()
    run(go())


@pytest.mark.parametrize('failure', ['page', 'download', 'conversion', 'all_pages'])
def test_cli_exit_code_and_report_reflect_resource_errors(tmp_path, monkeypatch, failure):
    class ErrorSite(Site):
        async def handle(self, request):
            if request.path == '/error' or (failure == 'all_pages' and request.path == '/'):
                return web.Response(status=403, text='Ingen åtkomst')
            if request.path == '/file.pdf' and failure == 'download':
                return web.Response(status=403)
            return await super().handle(request)
    if failure == 'conversion':
        async def fail_conversion(*args, **kwargs):
            return {'text': None, 'ocr': False, 'error': 'Parserfel'}
        monkeypatch.setattr(uwc.ConversionProcessRunner, 'run', fail_conversion)
    async def go():
        site = ErrorSite()
        links = [('/error', 'Fel')] if failure == 'page' else [('/file.pdf', 'Dokument')]
        site.pages['/'] = (page('Start', 'Starttext', links), None)
        site.files['/file.pdf'] = (make_pdf('Dokumenttext'), None)
        runner, base = await serve(site)
        path = tmp_path / 'config.json'
        path.write_text(json.dumps([{**config(base, tmp_path), 'name': 'test',
                                     'find_sitemap': False}]), encoding='utf-8')
        try:
            code = await uwc.run_cli_mode(str(path), output_dir=str(tmp_path / 'out'))
            report = json.loads((tmp_path / 'out' / 'test' / 'crawl_report.json').read_text())
            assert code == 1
            assert report['status'] == ('failed' if failure == 'all_pages' else 'partial')
        finally:
            await runner.cleanup()
    run(go())


def test_page_limit_is_a_successful_intentional_scope(tmp_path):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Starttext', [('/other', 'Annan')]), None)
        site.pages['/other'] = (page('Annan', 'Mer text'), None)
        runner, base = await serve(site)
        try:
            c = await run_crawl(base, tmp_path, max_pages=1, find_sitemap=False)
            report = json.loads((tmp_path / 'crawl_report.json').read_text())
            assert c.delivery_status() == report['status'] == 'success'
            assert not report['completed_naturally']
        finally:
            await runner.cleanup()
    run(go())


def test_failed_startup_still_writes_a_failed_report(tmp_path):
    async def go():
        site = Site()
        runner, base = await serve(site)
        c = uwc.AsyncWebCrawler(config(base, tmp_path, headless_mode='login_then_headless',
                                       cookie_file=str(tmp_path / 'missing-cookies.json')))
        try:
            await c.crawl()
            report = json.loads((tmp_path / 'crawl_report.json').read_text())
            assert report['status'] == 'failed'
            assert 'cookie_file' in report['fatal_error']
            assert not report['completed_naturally']
        finally:
            await runner.cleanup()
    run(go())


@pytest.mark.parametrize('visited', [0, 1])
def test_stopped_run_cannot_report_success(tmp_path, visited):
    c = uwc.AsyncWebCrawler(config('https://test.se', tmp_path))
    try:
        c.stats.pages_visited = visited
        c.stop()
        assert c.delivery_status() == ('partial' if visited else 'failed')
    finally:
        c._close_log_handlers()


@pytest.mark.parametrize('artifact', ['index.csv', 'manifest', 'chunks.jsonl', 'changes.jsonl'])
def test_export_errors_are_fatal_and_reported(tmp_path, monkeypatch, artifact):
    original = uwc.atomic_write_text
    if artifact in ('index.csv', 'manifest'):
        def fail_write(path, text):
            if artifact in path:
                raise OSError('Simulerat skrivfel')
            return original(path, text)
        monkeypatch.setattr(uwc, 'atomic_write_text', fail_write)
    elif artifact == 'chunks.jsonl':
        def fail_chunks(*args):
            raise OSError('Simulerat skrivfel')
        monkeypatch.setattr(uwc.AsyncWebCrawler, '_write_chunks_jsonl', fail_chunks)
    else:
        original_replace = uwc.os.replace
        def fail_replace(source, target):
            if str(target).endswith('changes.jsonl'):
                raise OSError('Simulerat skrivfel')
            return original_replace(source, target)
        monkeypatch.setattr(uwc.os, 'replace', fail_replace)
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Starttext'), None)
        runner, base = await serve(site)
        try:
            c = await run_crawl(base, tmp_path, find_sitemap=False)
            report = json.loads((tmp_path / 'crawl_report.json').read_text())
            assert c.delivery_status() == report['status'] == 'failed'
            assert report['export_failures_count'] == 1
            assert artifact in report['export_failures'][0]['artifact']
        finally:
            await runner.cleanup()
    run(go())


def test_registered_unreadable_source_does_not_silently_replace_existing_export(tmp_path):
    c = uwc.AsyncWebCrawler(config('https://test.se', tmp_path))
    folder = tmp_path / 'texter'
    folder.mkdir(exist_ok=True)
    (folder / 'broken.json').write_text('{broken')
    export = tmp_path / 'chunks.jsonl'
    export.write_text('FÖREGÅENDE EXPORT')
    c._export_files = {'broken.json'}
    try:
        with pytest.raises(RuntimeError, match='Registrerad källfil'):
            c._write_chunks_jsonl()
        assert export.read_text() == 'FÖREGÅENDE EXPORT'
    finally:
        c._close_log_handlers()


def test_public_document_failure_does_not_launch_browser(tmp_path, monkeypatch):
    async def no_browser(*args):
        pytest.fail('Publika dokumentfel får inte starta Chromium')
    monkeypatch.setattr(uwc.AsyncWebCrawler, '_download_via_playwright', no_browser)
    async def go():
        class ErrorSite(Site):
            async def handle(self, request):
                if request.path.endswith('.pdf'):
                    return web.Response(status=403)
                return await super().handle(request)
        site = ErrorSite()
        site.pages['/'] = (page('Start', 'Starttext', [('/file.pdf', 'Dokument')]), None)
        runner, base = await serve(site)
        try:
            c = await run_crawl(base, tmp_path, find_sitemap=False)
            assert len(c.report['download_failures']) == 1
            assert c.delivery_status() == 'partial'
        finally:
            await runner.cleanup()
    run(go())


@needs_chromium
@pytest.mark.parametrize('length_header', [False, True])
def test_browser_document_limit_aborts_during_transfer(tmp_path, length_header):
    class StreamSite(Site):
        def __init__(self):
            super().__init__()
            self.sent = 0
            self.disconnected = asyncio.Event()
        async def handle(self, request):
            headers = {'Content-Type': 'application/pdf',
                       'Content-Disposition': 'attachment; filename="large.pdf"'}
            if length_header:
                headers['Content-Length'] = str(65536 * 100)
            response = web.StreamResponse(headers=headers)
            await response.prepare(request)
            try:
                for i in range(100):
                    await response.write((b'%PDF-' if i == 0 else b'') + b'x' * 65531)
                    self.sent += 1
                    await asyncio.sleep(0.03)
                await response.write_eof()
            except (ConnectionError, asyncio.CancelledError):
                self.disconnected.set()
            return response
    async def go():
        site = StreamSite()
        runner, base = await serve(site)
        c = uwc.AsyncWebCrawler(config(base, tmp_path, respect_robots=False,
                                       max_download_mb=0.1), queue.Queue())
        c._allow_private = True
        target = tmp_path / 'large.part'
        try:
            assert not await c._download_via_playwright(base + '/large.pdf', str(target))
            await asyncio.wait_for(site.disconnected.wait(), 3)
            assert site.sent < 15, 'Överföringen måste stoppas långt innan hela filen laddats ner'
            assert not target.exists()
        finally:
            await c._shutdown()
            c._close_log_handlers()
            await runner.cleanup()
    run(go())


@needs_chromium
def test_browser_inline_document_is_streamed_without_attachment_event(tmp_path):
    async def go():
        site = Site()
        data = make_pdf('INLINE DOKUMENT')
        site.files['/inline.pdf'] = (data, None)
        runner, base = await serve(site)
        c = uwc.AsyncWebCrawler(config(base, tmp_path, respect_robots=False), queue.Queue())
        c._allow_private = True
        target = tmp_path / 'inline.part'
        try:
            assert await c._download_via_playwright(base + '/inline.pdf', str(target))
            assert target.read_bytes() == data
        finally:
            await c._shutdown()
            c._close_log_handlers()
            await runner.cleanup()
    run(go())
