"""Regressioner för omförsök, fördröjd SPA-text och begränsad sitemap-läsning."""
import asyncio
import gzip
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from aiohttp import web

from test_advanced import needs_chromium
from test_crawler import Site, config, make_pdf, page, published, run, run_crawl, serve, uwc


@pytest.mark.parametrize('first_failure', [429, 503, 'avbruten ström'])
def test_document_retries_temporary_failures_and_publishes_complete_file(tmp_path, first_failure):
    class DocumentSite(Site):
        def __init__(self):
            super().__init__()
            self.attempts = []
            self.payload = make_pdf('HELT DOKUMENT EFTER OMFORSOK')
            self.pages['/'] = (page('Start', 'Dokumentlänk', [('/taxa.pdf', 'Taxa')]), None)

        async def handle(self, request):
            if request.path != '/taxa.pdf':
                return await super().handle(request)
            self.attempts.append(time.monotonic())
            if len(self.attempts) == 1:
                if isinstance(first_failure, int):
                    return web.Response(status=first_failure, headers={'Retry-After': '0'})
                response = web.StreamResponse(headers={'Content-Type': 'application/pdf', 'Content-Length': '65536'})
                await response.prepare(request)
                await response.write(b'%PDF-1.7\nOfullstandig fil')
                request.transport.close()
                return response
            return web.Response(body=self.payload, content_type='application/pdf')

    async def go():
        site = DocumentSite()
        runner, base = await serve(site)
        try:
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert result.delivery_status() == 'success' and not result.report['download_failures']
            assert len(site.attempts) == 2
            assert site.attempts[1] - site.attempts[0] >= 0.65
            generation = published(tmp_path)
            originals = list((generation / 'dokument').iterdir())
            assert len(originals) == 1 and originals[0].read_bytes() == site.payload
            assert 'HELT DOKUMENT' in (generation / 'chunks.jsonl').read_text(encoding='utf-8')
        finally:
            await runner.cleanup()
    run(go())


@pytest.mark.parametrize(('header', 'attempts'), [('0', 3), ('120', 1)])
def test_exhausted_document_retries_keep_previous_version(tmp_path, header, attempts):
    class OutageSite(Site):
        outage = False
        requests = 0

        async def handle(self, request):
            if request.path == '/taxa.pdf' and self.outage:
                self.requests += 1
                assert request.headers.get('If-None-Match') == 'previous'
                return web.Response(status=503, headers={'Retry-After': header})
            return await super().handle(request)

    async def go():
        site = OutageSite()
        original = make_pdf('BEVARAD TAXA')
        site.pages['/'] = (page('Start', 'Dokumentlänk', [('/taxa.pdf', 'Taxa')]), None)
        site.files['/taxa.pdf'] = (original, 'previous')
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False)
            site.outage = True
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert result.delivery_status() == 'partial'
            assert site.requests == attempts and len(result.report['download_failures']) == 1
            generation = published(tmp_path)
            assert next((generation / 'dokument').iterdir()).read_bytes() == original
            assert 'BEVARAD TAXA' in (generation / 'chunks.jsonl').read_text(encoding='utf-8')
        finally:
            await runner.cleanup()
    run(go())


def test_document_retry_after_accepts_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=20)
    assert 17 <= uwc.document_retry_delay(0, format_datetime(future, usegmt=True)) <= 20


def test_stop_interrupts_document_retry_after(tmp_path, monkeypatch):
    class BusySite(Site):
        requests = 0

        async def handle(self, request):
            if request.path == '/taxa.pdf':
                self.requests += 1
                return web.Response(status=503, headers={'Retry-After': '60'})
            return await super().handle(request)

    async def go():
        waiting = asyncio.Event()
        original = uwc.document_retry_delay

        def observe_retry(attempt, header):
            waiting.set()
            return original(attempt, header)

        monkeypatch.setattr(uwc, 'document_retry_delay', observe_retry)
        site = BusySite()
        site.pages['/'] = (page('Start', 'Dokumentlänk', [('/taxa.pdf', 'Taxa')]), None)
        runner, base = await serve(site)
        crawler = uwc.AsyncWebCrawler(config(base, tmp_path, find_sitemap=False))
        task = asyncio.create_task(crawler.crawl())
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            crawler.stop()
            await asyncio.wait_for(task, 3)
            assert site.requests == 1 and not (tmp_path / 'current.json').exists()
        finally:
            if not task.done():
                crawler.stop()
                await task
            await runner.cleanup()
    run(go())


@needs_chromium
def test_spa_waits_for_data_requested_after_domcontentloaded(tmp_path):
    class SpaSite(Site):
        async def handle(self, request):
            if request.path == '/api':
                await asyncio.sleep(0.8)
                return web.Response(text='RENDERAD TAXA för kommunens tjänster finns här.', content_type='text/plain')
            return await super().handle(request)

    async def go():
        site = SpaSite()
        site.pages['/'] = ('''<html><title>Taxor</title><main id="app"><p>Laddar taxorna</p></main>
            <script>setTimeout(async () => {
                document.querySelector('p').textContent = await (await fetch('/api')).text();
            }, 200);</script></html>''', 'skal')
        runner, base = await serve(site)
        try:
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            corpus = (published(tmp_path) / 'chunks.jsonl').read_text(encoding='utf-8')
            assert result.stats.playwright_fallbacks == 1
            assert 'RENDERAD TAXA' in corpus and 'Laddar taxorna' not in corpus
        finally:
            await runner.cleanup()
    run(go())


@needs_chromium
def test_spa_selector_timeout_preserves_previous_generation(tmp_path):
    async def go():
        site = Site()
        site.pages['/'] = (page('Källa', 'TIDIGARE KÄLLTEXT'), 'samma-etag')
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False)
            previous = published(tmp_path)
            # En stor HTML-sida skulle normalt inte trigga hybridmotorns JS-fallback.
            shell = page('Källa', 'Laddar uppdateringen ' * 120)
            ready_script = "<script>setTimeout(() => document.querySelector('main').id='klar', 200)</script>"
            site.pages['/'] = (shell + ready_script, 'samma-etag')
            result = await run_crawl(base, tmp_path, find_sitemap=False, spa_ready_selector='#klar')
            assert result.stats.playwright_fallbacks == 1 and published(tmp_path) != previous
            updated = published(tmp_path)
            site.pages['/'] = (shell, 'samma-etag')
            result = await run_crawl(base, tmp_path, find_sitemap=False,
                                     spa_ready_selector='#klar', spa_wait_seconds=0.2)
            assert result.report['spa_timeouts'] == [{'url': base + '/'}]
            assert result.delivery_status() == 'failed' and published(tmp_path) == updated
        finally:
            await runner.cleanup()
    run(go())


def test_gzip_sitemap_parser_leaves_event_loop_responsive(tmp_path, monkeypatch):
    class SitemapSite(Site):
        async def handle(self, request):
            if request.path == '/sitemap.xml':
                xml = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>http://{request.host}/gömd</loc><lastmod>2026-01-01</lastmod></url></urlset>'
                return web.Response(body=gzip.compress(xml.encode()), content_type='application/gzip')
            return await super().handle(request)

    async def go():
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = uwc.safe_gunzip

        def slow_unpack(content, limit):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3), 'Eventloopen måste kunna köra medan uppackningen pågår'
            return original(content, limit)

        monkeypatch.setattr(uwc, 'safe_gunzip', slow_unpack)
        site = SitemapSite()
        runner, base = await serve(site)
        crawler = uwc.AsyncWebCrawler(config(base, tmp_path, respect_robots=False))
        crawler._allow_private = True
        crawler.req_session = await crawler._create_session()
        task = asyncio.create_task(crawler._parse_sitemap(base + '/sitemap.xml'))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not task.done()
            release.set()
            await task
            assert crawler.sitemap_lastmod == {base + '/gömd': '2026-01-01'}
            assert crawler.url_queue.size() == 2
        finally:
            release.set()
            await task
            await crawler._shutdown()
            crawler._close_log_handlers()
            await runner.cleanup()
    run(go())


def test_sitemap_index_limits_queued_children_and_concurrent_reads(tmp_path, monkeypatch):
    async def go():
        base = 'https://test.invalid'
        crawler = uwc.AsyncWebCrawler(config(base, tmp_path))
        visited = []
        root = base + '/sitemap.xml'

        async def fetch(url, **kwargs):
            visited.append(url)
            assert len(crawler._sitemap_tasks) <= 4
            if url == root:
                children = [root, *[f'/sm{i}.xml' for i in range(700)]]
                body = '<sitemapindex>' + ''.join(f'<sitemap><loc>{url}</loc></sitemap>' for url in children) + '</sitemapindex>'
            else:
                body = '<sitemapindex><sitemap><loc>/sitemap.xml</loc></sitemap></sitemapindex>'
            return uwc.FetchResult(body=body.encode(), status=200, final_url=url)

        monkeypatch.setattr(crawler, 'fetch', fetch)
        try:
            await crawler._parse_sitemap(root)
            assert len(visited) == len(set(visited)) == 500
            assert not crawler._sitemap_tasks
        finally:
            crawler._close_log_handlers()
    run(go())


@pytest.mark.parametrize('defused', [True, False])
def test_utf16_sitemap_rejects_entity_declarations(monkeypatch, defused):
    if defused and not uwc.HAS_DEFUSEDXML:
        pytest.skip('defusedxml saknas')
    monkeypatch.setattr(uwc, 'HAS_DEFUSEDXML', defused)
    xml = '<!DOCTYPE urlset [<!ENTITY x "https://test.invalid/a">]><urlset><url><loc>&x;</loc></url></urlset>'
    with pytest.raises(ValueError):
        uwc.parse_sitemap_xml(xml.encode('utf-16'), 10000)
