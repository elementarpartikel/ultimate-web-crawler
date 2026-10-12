"""Regressioner för dokumentversioner, aktualitet och exportens filregister."""
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta

import pytest
from aiohttp import web

from test_crawler import (Site, config, make_pdf, page, read_changes, run,
                          run_crawl, serve, texts, uwc, published)


def corpus(out):
    return (out / 'chunks.jsonl').read_text(encoding='utf-8')


def doc_site():
    site = Site()
    site.pages['/'] = (page('Start', 'Starttext', [('/taxa.pdf', 'Taxa för tjänster')]), 's1')
    site.files['/taxa.pdf'] = (make_pdf('GAMMAL TAXA 100 kronor'), 'd1')
    return site


def test_document_same_size_without_validators_is_updated(tmp_path):
    async def go():
        site = doc_site()
        old = make_pdf('Taxa 100 kronor')
        new = make_pdf('Taxa 200 kronor')
        assert len(old) == len(new)
        site.files['/taxa.pdf'] = (old, None)
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            site.files['/taxa.pdf'] = (new, None)
            changed = await run_crawl(base, tmp_path)
            same = await run_crawl(base, tmp_path)
            assert changed.counts['docs_updated'] == 1
            assert same.counts['docs_updated'] == 0
            assert 'Taxa 200 kronor' in corpus(tmp_path)
            assert 'Taxa 100 kronor' not in corpus(tmp_path)
            assert next((tmp_path / 'dokument').glob('*.pdf')).read_bytes() == new
        finally:
            await runner.cleanup()
    run(go())


def test_failed_conversion_removes_old_text_and_retries_on_304(tmp_path, monkeypatch):
    async def go():
        site = doc_site()
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            site.files['/taxa.pdf'] = (make_pdf('NY TAXA 200 kronor'), 'd2')

            async def fail(self, *args, **kwargs):
                return {'text': None, 'ocr': False, 'error': 'Simulerat konverteringsfel'}

            with monkeypatch.context() as patch:
                patch.setattr(uwc.ConversionProcessRunner, 'run', fail)
                failed = await run_crawl(base, tmp_path)
            assert len(failed.report['conversion_failures']) == 1
            assert 'GAMMAL TAXA' not in corpus(tmp_path)
            assert not [n for n in texts(tmp_path) if n.endswith('_doc.md')]
            site.hits.clear()
            healed = await run_crawl(base, tmp_path)
            assert healed.stats.documents_downloaded == 0
            assert ('/taxa.pdf', True, 'GET') in site.hits
            assert 'NY TAXA 200' in corpus(tmp_path)
            assert not healed.report['conversion_failures']
        finally:
            await runner.cleanup()
    run(go())


@pytest.mark.parametrize('status', [404, 410])
def test_unlinked_deleted_document_is_removed_without_browser(tmp_path, monkeypatch, status):
    class GoneSite(Site):
        async def handle(self, request):
            if request.path in self.gone:
                self.hits.append((request.path, False, request.method))
                return web.Response(status=status)
            return await super().handle(request)

    async def no_browser(*args):
        pytest.fail('Borttagna dokument får inte starta en webbläsare')

    monkeypatch.setattr(uwc.AsyncWebCrawler, '_download_via_playwright', no_browser)

    async def go():
        template = doc_site()
        site = GoneSite()
        site.pages, site.files = template.pages, template.files
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            site.pages['/'] = (page('Start', 'Länken är borttagen'), 's2')
            site.gone.add('/taxa.pdf')
            result = await run_crawl(base, tmp_path)
            assert result.counts['docs_removed'] == 1
            assert result.report['gone_documents'] == [{'url': base + '/taxa.pdf', 'status': status}]
            assert not list((tmp_path / 'dokument').glob('*.pdf'))
            assert not [n for n in texts(tmp_path) if n.endswith('_doc.md')]
            assert 'GAMMAL TAXA' not in corpus(tmp_path)
            assert any(c['event'] == 'document_removed' for c in read_changes(tmp_path))
            manifest = json.loads(next(tmp_path.glob('manifest_*.json')).read_text(encoding='utf-8'))
            assert manifest['document_count'] == 0
            with sqlite3.connect(next(published(tmp_path).glob('*_cache.db'))) as db:
                assert db.execute('SELECT COUNT(*) FROM doc_cache').fetchone()[0] == 0
        finally:
            await runner.cleanup()
    run(go())


def test_sitemap_safety_check_survives_daily_skips(tmp_path, monkeypatch):
    real_datetime = uwc.datetime
    start = real_datetime.now()
    day = 0

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            value = start + timedelta(days=day)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(uwc, 'datetime', Clock)

    async def go():
        nonlocal day
        site = Site()
        site.pages['/'] = (page('Start', 'Starttext', [('/a', 'A')]), 's1')
        site.pages['/a'] = (page('Avgifter', 'GAMMAL VERSION'), None)
        site.sitemap_paths = ['/a']
        site.sitemap_lastmod['/a'] = '2020-01-01'
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            for day in range(1, 21):
                if day == 10:
                    site.pages['/a'] = (page('Avgifter', 'NY VERSION'), None)
                site.hits.clear()
                await run_crawl(base, tmp_path)
                if day == 14:
                    assert not any(h[0] == '/a' for h in site.hits)
                if day == 15:
                    assert any(h[0] == '/a' for h in site.hits)
            assert 'NY VERSION' in corpus(tmp_path)
            assert 'GAMMAL VERSION' not in corpus(tmp_path)
        finally:
            await runner.cleanup()
    run(go())


def test_304_updates_check_time_but_not_fetch_time(tmp_path):
    async def go():
        db = uwc.AsyncCrawlDatabase(str(tmp_path / 'cache.db'))
        await db.connect()
        try:
            await db.save_cache('https://test.se/', 'hash', 'Titel', 100)
            first = await db.get_cache('https://test.se/')
            await asyncio.sleep(0.01)
            await db.touch_cache('https://test.se/')
            checked = await db.get_cache('https://test.se/')
            assert checked['checked_at'] > first['checked_at']
            assert checked['fetched_at'] == first['fetched_at']
            await db.touch_cache('https://test.se/', checked=False)
            skipped = await db.get_cache('https://test.se/')
            assert skipped['checked_at'] == checked['checked_at']
        finally:
            await db.close()
    run(go())


@pytest.mark.parametrize('incremental', [True, False])
def test_404_removes_old_page_also_in_full_crawl(tmp_path, incremental):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'GAMMAL SIDA'), 's1')
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            site.gone.add('/')
            result = await run_crawl(base, tmp_path, incremental=incremental)
            assert result.counts['removed'] == 1
            assert not texts(tmp_path)
            assert corpus(tmp_path) == ''
        finally:
            await runner.cleanup()
    run(go())


@pytest.mark.parametrize('replacement', ['language', 'short'])
def test_rejected_page_removes_its_previous_text(tmp_path, replacement):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'GAMMAL GODKÄND TEXT'), 's1')
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, languages=['sv'])
            if replacement == 'language':
                html = page('Start', 'New English content').replace("lang='sv'", "lang='en'")
            else:
                html = '<html lang="sv"><title>Start</title><main><p>Kort text.</p></main>' + ' ' * 600
            site.pages['/'] = (html, 's2')
            result = await run_crawl(base, tmp_path, languages=['sv'])
            assert result.counts['removed'] == 1
            if replacement == 'short':
                # Hela korpusen försvann bakom ett kort 200-svar: utan 404/410
                # ska nya publiceringsspärren bevara den förra leveransen.
                assert result.delivery_status() == 'failed'
                assert result.report['publication_blocked']
                assert 'GAMMAL GODKÄND TEXT' in corpus(tmp_path)
            else:
                assert 'GAMMAL GODKÄND TEXT' not in corpus(tmp_path)
                assert not texts(tmp_path)
        finally:
            await runner.cleanup()
    run(go())


def test_export_keeps_unvisited_registered_pages_and_archives_orphans(tmp_path):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Starttext', [('/a', 'A')]), 's1')
        site.pages['/a'] = (page('A', 'BEVARAD SIDA'), 'a1')
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            orphan = tmp_path / 'texter' / 'orphan.md'
            orphan.write_text('# Föräldralös\n\n**Källa:** https://test.se/gammal\n\n---\n\n'
                              'FÖRÄLDRALÖST INNEHÅLL\n\n---\n\n**Källa:** https://test.se/gammal\n',
                              encoding='utf-8')
            limited = await run_crawl(base, tmp_path, max_pages=1)
            assert limited.stats.pages_visited == 1
            assert 'BEVARAD SIDA' in corpus(tmp_path)
            assert 'FÖRÄLDRALÖST' not in corpus(tmp_path)
            assert not orphan.exists()
            assert list((tmp_path / 'texter_utgangna').rglob('*orphan.md'))
            assert len(limited.report['retired_text_files']) == 1
        finally:
            await runner.cleanup()
    run(go())


def test_unlinked_page_is_rechecked_and_removed(tmp_path):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Starttext', [('/a', 'A')]), 's1')
        site.pages['/a'] = (page('A', 'BORTTAGEN OLÄNKAD SIDA'), 'a1')
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            site.pages['/'] = (page('Start', 'Länken är borttagen'), 's2')
            site.gone.add('/a')
            result = await run_crawl(base, tmp_path)
            assert result.counts['removed'] == 1
            assert 'BORTTAGEN OLÄNKAD SIDA' not in corpus(tmp_path)
        finally:
            await runner.cleanup()
    run(go())


def test_maintenance_respects_depth_and_url_filters(tmp_path):
    async def go():
        site = doc_site()
        site.pages['/a'] = (page('A', 'Känd sida'), 'a1')
        site.pages['/'] = (page('Start', 'Starttext', [('/a', 'A'), ('/taxa.pdf', 'Taxa')]), 's1')
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            site.pages['/'] = (page('Start', 'Alla länkar borttagna'), 's2')
            site.hits.clear()
            await run_crawl(base, tmp_path, max_depth=1)
            assert not any(h[0] in ('/a', '/taxa.pdf') for h in site.hits)
            site.hits.clear()
            await run_crawl(base, tmp_path, exclude_keywords=['/a', 'taxa.pdf'])
            assert not any(h[0] in ('/a', '/taxa.pdf') for h in site.hits)
            assert not any(r['url'].endswith(('/a', '/taxa.pdf'))
                           for r in map(json.loads, corpus(tmp_path).splitlines()))
        finally:
            await runner.cleanup()
    run(go())


def test_document_conversion_profile_changes_rebuild_on_304(tmp_path):
    async def go():
        site = doc_site()
        site.files['/taxa.pdf'] = (make_pdf('Kontakta person@example.org för mer information'), 'd1')
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, remove_email=False)
            assert 'person@example.org' in corpus(tmp_path)
            await run_crawl(base, tmp_path, remove_email=True)
            assert 'person@example.org' not in corpus(tmp_path)
            assert '[E-POST]' in corpus(tmp_path)
        finally:
            await runner.cleanup()
    run(go())


def test_old_cache_is_migrated_without_trusting_skip_timestamp(tmp_path):
    path = tmp_path / 'cache.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE page_cache (url TEXT PRIMARY KEY, content_hash TEXT, '
                   'title TEXT, crawled_at TEXT, content_length INTEGER)')
        db.execute('INSERT INTO page_cache VALUES (?, ?, ?, ?, ?)',
                   ('https://test.se/a', 'old', 'A', datetime.now().isoformat(), 100))
    async def go():
        db = uwc.AsyncCrawlDatabase(str(path))
        await db.connect()
        try:
            cached = await db.get_cache('https://test.se/a')
            assert cached['fetched_at'] is None and cached['checked_at'] is None
            c = uwc.AsyncWebCrawler(config('https://test.se', tmp_path / 'out'))
            c.sitemap_lastmod['https://test.se/a'] = '2020-01-01'
            assert not c._sitemap_says_unchanged('https://test.se/a', cached)
            c._close_log_handlers()
        finally:
            await db.close()
    run(go())


def test_document_markdown_survives_failed_atomic_replace(tmp_path, monkeypatch):
    converter = uwc.DocumentConverter(str(tmp_path), ocr=False)
    original = tmp_path / 'original.pdf'
    original.write_bytes(make_pdf('GAMMAL ATOMÄR TEXT'))
    output = converter.convert(str(original), 'https://test.se/original.pdf')
    old = open(output, encoding='utf-8').read()
    original.write_bytes(make_pdf('NY ATOMÄR TEXT'))
    def fail(*args):
        raise OSError('Simulerat avbrott före atomärt filbyte')
    monkeypatch.setattr(uwc.os, 'replace', fail)
    assert converter.convert(str(original), 'https://test.se/original.pdf') is None
    assert open(output, encoding='utf-8').read() == old


def test_old_document_cache_reconverts_unversioned_markdown(tmp_path):
    async def go():
        site = doc_site()
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            with sqlite3.connect(next(published(tmp_path).glob('*_cache.db'))) as db:
                db.execute('UPDATE doc_cache SET content_hash=NULL, converted_hash=NULL, '
                           'md_filename=NULL, conversion_profile=NULL')
            for name in texts(tmp_path):
                if name.endswith('_doc.md'):
                    (published(tmp_path) / 'texter' / name).write_text('# GAMMAL OVERSIONERAD TEXT', encoding='utf-8')
            result = await run_crawl(base, tmp_path)
            assert result.stats.documents_downloaded == 0
            assert 'GAMMAL OVERSIONERAD TEXT' not in corpus(tmp_path)
            assert 'GAMMAL TAXA 100' in corpus(tmp_path)
        finally:
            await runner.cleanup()
    run(go())


def test_export_rejects_document_text_when_original_version_differs(tmp_path):
    async def go():
        site = doc_site()
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            new = make_pdf('NY TAXA 200 kronor')
            site.files['/taxa.pdf'] = (new, 'd2')
            # Simulera extern korruption av den publicerade originalversionen.
            # Ett avbrott i arbetsmappen kan numera inte ändra denna generation.
            next((published(tmp_path) / 'dokument').glob('*.pdf')).write_bytes(new)
            limited = await run_crawl(base, tmp_path, max_pages=1)
            assert 'GAMMAL TAXA' not in corpus(tmp_path)
            assert limited.report['document_version_mismatch']
            await run_crawl(base, tmp_path)
            assert 'NY TAXA 200' in corpus(tmp_path)
        finally:
            await runner.cleanup()
    run(go())


def test_transient_document_failure_preserves_matching_old_version(tmp_path):
    class FailingSite(Site):
        failed = False
        async def handle(self, request):
            if self.failed and request.path == '/taxa.pdf':
                return web.Response(status=503)
            return await super().handle(request)
    async def go():
        initial = doc_site()
        site = FailingSite()
        site.pages, site.files = initial.pages, initial.files
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            site.failed = True
            result = await run_crawl(base, tmp_path)
            assert result.report['download_failures']
            assert 'GAMMAL TAXA 100' in corpus(tmp_path)
            assert result.counts['docs_removed'] == 0
        finally:
            await runner.cleanup()
    run(go())


def test_parallel_conversions_across_crawlers_have_independent_diagnostics(tmp_path, monkeypatch):
    active, peak = 0, 0
    async def result(self, filepath, url, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        success = url.endswith('/a.pdf')
        return {'text': '# A\n\n**Dokument-URL:** https://test.se/a.pdf\n\n---\n\n'
                        'Dokumentets text\n\n---\n' if success else None,
                'ocr': success, 'error': '' if success else 'Konverteringsfel för B'}
    monkeypatch.setattr(uwc.ConversionProcessRunner, 'run', result)
    async def go():
        a = uwc.AsyncWebCrawler(config('https://test.se', tmp_path / 'a'))
        b = uwc.AsyncWebCrawler(config('https://test.se', tmp_path / 'b'))
        try:
            await asyncio.gather(a._convert_document('https://test.se/a.pdf', 'a.pdf', 'a.pdf', {}),
                                 b._convert_document('https://test.se/b.pdf', 'b.pdf', 'b.pdf', {}))
            assert peak == 2
            assert a.report['ocr_used'] == [{'url': 'https://test.se/a.pdf', 'file': 'a.pdf'}]
            assert not b.report['ocr_used']
            assert b.report['conversion_failures'][0]['reason'] == 'Konverteringsfel för B'
        finally:
            a._close_log_handlers()
            b._close_log_handlers()
    run(go())


def test_export_failure_marks_delivery_failed(tmp_path, monkeypatch):
    def fail(self):
        raise OSError('Simulerat exportfel')
    monkeypatch.setattr(uwc.AsyncWebCrawler, '_write_chunks_jsonl', fail)
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Starttext'), 's1')
        runner, base = await serve(site)
        try:
            result = await run_crawl(base, tmp_path)
            assert 'JSONL-export misslyckades' in result.fatal_error
            report = json.loads((tmp_path / 'crawl_report.json').read_text(encoding='utf-8'))
            assert report['fatal_error'] == result.fatal_error
        finally:
            await runner.cleanup()
    run(go())
