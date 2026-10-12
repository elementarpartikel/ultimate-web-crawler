"""Skydda tidigare korpus vid driftfel och bevara urval/layout."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from aiohttp import web

from test_crawler import Site, config, page, published, run, run_crawl, serve, uwc
import crawler_runtime as runtime


class OutageSite(Site):
    outage = None
    async def handle(self, request):
        if self.outage == 'robots' and request.path == '/robots.txt':
            self.hits.append((request.path, False, request.method))
            return web.Response(status=503, headers={'Retry-After': '0'})
        if self.outage == '403' and request.path != '/robots.txt':
            return web.Response(status=403)
        return await super().handle(request)


def populate(site):
    site.pages['/'] = (page('Start', 'ORIGINAL START', [(f'/p{i}', str(i)) for i in range(5)]), 's')
    for i in range(5):
        site.pages[f'/p{i}'] = (page(f'Rubrik {i}', f'ORIGINAL {i}'), str(i))


@pytest.mark.parametrize('outage', ['robots', 'network', '403'])
def test_total_outage_preserves_current_and_cache(tmp_path, monkeypatch, outage):
    async def go():
        site = OutageSite()
        populate(site)
        server, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False)
            before = (tmp_path / 'current.json').read_bytes()
            previous = published(tmp_path)
            assert len(list((previous / 'texter').glob('*.md'))) == 6
            site.outage = outage
            with monkeypatch.context() as patch:
                if outage == 'network':
                    async def unavailable(*args, **kwargs):
                        return None
                    patch.setattr(uwc.AsyncWebCrawler, 'fetch', unavailable)
                result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert result.delivery_status() == 'failed'
            assert (tmp_path / 'current.json').read_bytes() == before
            report = json.loads((tmp_path / 'crawl_report.json').read_text(encoding='utf-8'))
            assert not report['publication_committed']
            assert len(list((published(tmp_path) / 'texter').glob('*.md'))) == 6
            site.outage = None
            healed = await run_crawl(base, tmp_path, find_sitemap=False)
            assert healed.delivery_status() == 'success'
            assert healed.stats.pages_not_modified_304 == 6
        finally:
            await server.cleanup()
    run(go())


@pytest.mark.parametrize('options', [
    {'exclude_keywords': ['nothing-matches']}, {'allowed_domains': ['unused.se']},
    {'languages': ['sv']}, {'respect_canonical': False}, {'use_hybrid': False},
])
def test_selection_change_with_max_pages_keeps_unvisited_sources(tmp_path, options):
    async def go():
        site = Site()
        populate(site)
        server, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False)
            c = uwc.AsyncWebCrawler(config(base, tmp_path, find_sitemap=False, max_pages=1, **options))
            # En oförändrad 304-cache ska inte behöva starta browsern vid motorbyte.
            await c.crawl()
            assert len(list((published(tmp_path) / 'texter').glob('*.md'))) == 6
        finally:
            await server.cleanup()
    run(go())


def test_unconfirmed_mass_loss_blocks_partial_publication(tmp_path):
    async def go():
        site = Site()
        populate(site)
        server, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False)
            before = (tmp_path / 'current.json').read_bytes()
            for i in range(5):
                site.pages[f'/p{i}'] = ('<html><title>Service</title><body>Vänta</body></html>', 'error')
            result = await run_crawl(base, tmp_path, find_sitemap=False, use_hybrid=True)
            assert result.delivery_status() == 'failed'
            assert result.report['publication_blocked']
            assert (tmp_path / 'current.json').read_bytes() == before
        finally:
            await server.cleanup()
    run(go())


def test_confirmed_mass_deletion_can_publish_empty_corpus(tmp_path):
    async def go():
        site = Site()
        populate(site)
        server, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False)
            before = published(tmp_path)
            site.gone.update(site.pages)
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert result.delivery_status() == 'success'
            assert published(tmp_path) != before
            assert not list((published(tmp_path) / 'texter').glob('*.md'))
        finally:
            await server.cleanup()
    run(go())


def test_unlined_price_list_keeps_each_label_next_to_its_price(tmp_path):
    source = tmp_path / 'taxa.pdf'
    doc = uwc.pymupdf.open()
    p = doc.new_page(width=600, height=800)
    rows = [('Bygglov villa', '12 000 kr'), ('Bygglov garage', '4 500 kr'),
            ('Rivningslov', '3 000 kr'), ('Strandskyddsdispens', '8 000 kr')]
    for i, (label, price) in enumerate(rows):
        p.insert_text((50, 150 + i * 30), label, fontsize=10)
        p.insert_text((360, 150 + i * 30), price, fontsize=10)
    doc.save(source)
    doc.close()
    text = uwc.DocumentConverter(str(tmp_path))._extract_pdf(str(source))
    for i, (label, price) in enumerate(rows):
        assert text.index(label) < text.index(price)
        if i + 1 < len(rows):
            assert text.index(price) < text.index(rows[i + 1][0])


def test_transient_robots_error_retries_then_publishes(tmp_path):
    class RetrySite(Site):
        attempts = 0
        async def handle(self, request):
            if request.path == '/robots.txt':
                self.attempts += 1
                if self.attempts == 1:
                    return web.Response(status=503, headers={'Retry-After': '0'})
            return await super().handle(request)
    async def go():
        site = RetrySite()
        populate(site)
        server, base = await serve(site)
        try:
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert result.delivery_status() == 'success'
            assert site.attempts == 2
            assert len(list((published(tmp_path) / 'texter').glob('*.md'))) == 6
        finally:
            await server.cleanup()
    run(go())


def test_real_connection_failure_keeps_previous_delivery(tmp_path):
    async def go():
        site = Site()
        populate(site)
        server, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False)
            before = (tmp_path / 'current.json').read_bytes()
        finally:
            await server.cleanup()
        result = await run_crawl(base, tmp_path, find_sitemap=False)
        assert result.delivery_status() == 'failed'
        assert (tmp_path / 'current.json').read_bytes() == before
    run(go())


def test_partial_waf_failure_does_not_delete_cached_source(tmp_path):
    class WafSite(Site):
        failed = False
        async def handle(self, request):
            if self.failed and request.path == '/p2':
                return web.Response(status=403)
            return await super().handle(request)
    async def go():
        site = WafSite()
        populate(site)
        server, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False)
            before = published(tmp_path)
            site.failed = True
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert result.delivery_status() == 'partial'
            assert published(tmp_path) != before
            assert len(list((published(tmp_path) / 'texter').glob('*.md'))) == 6
            assert not result.counts['removed']
        finally:
            await server.cleanup()
    run(go())


def test_unreachable_secondary_robots_does_not_retire_cached_sources(tmp_path):
    async def go():
        secondary = OutageSite()
        secondary.pages['/info'] = (page('Annan sajt', 'SEKUNDAR KALLA'), 'a')
        other_server, other_base = await serve(secondary)
        site = Site()
        site.pages['/'] = (page('Start', 'STARTTEXT', [(other_base + '/info', 'Information')]), 's')
        server, base = await serve(site)
        try:
            options = {'find_sitemap': False, 'allowed_domains': [uwc.urlparse(other_base).netloc]}
            await run_crawl(base, tmp_path, **options)
            secondary.outage = 'robots'
            result = await run_crawl(base, tmp_path, **options)
            assert result.delivery_status() == 'partial'
            assert result.report['robots_unreachable']
            assert not result.report['retired_text_files']
            assert 'SEKUNDAR KALLA' in (published(tmp_path) / 'chunks.jsonl').read_text(encoding='utf-8')
        finally:
            await server.cleanup()
            await other_server.cleanup()
    run(go())


def test_unknown_legacy_profile_cannot_publish_large_migration_loss(tmp_path, monkeypatch):
    async def go():
        site = Site()
        populate(site)
        server, base = await serve(site)
        try:
            generate = uwc.AsyncWebCrawler._generate_report
            async def legacy_report(c):
                await generate(c)
                path = Path(c.output_dir) / 'crawl_report.json'
                data = json.loads(path.read_text(encoding='utf-8'))
                data.pop('page_processing_profile', None)
                uwc.atomic_write_text(str(path), json.dumps(data))
            with monkeypatch.context() as patch:
                patch.setattr(uwc.AsyncWebCrawler, '_page_profile', lambda c: 'legacy-profile')
                patch.setattr(uwc.AsyncWebCrawler, '_generate_report', legacy_report)
                await run_crawl(base, tmp_path, find_sitemap=False)
            before = (tmp_path / 'current.json').read_bytes()
            result = await run_crawl(base, tmp_path, find_sitemap=False, max_pages=1,
                                     exclude_keywords=['nothing-matches'])
            assert result.report['publication_blocked']
            assert (tmp_path / 'current.json').read_bytes() == before
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert result.delivery_status() == 'success'
            assert len(list((published(tmp_path) / 'texter').glob('*.md'))) == 6
        finally:
            await server.cleanup()
    run(go())


def test_unknown_document_profile_cannot_empty_limited_upgrade(tmp_path, monkeypatch):
    from test_crawler import make_pdf
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'STARTTEXT', [(f'/f{i}.pdf', f'Taxa {i}') for i in range(5)]), 's')
        for i in range(5):
            site.files[f'/f{i}.pdf'] = (make_pdf(f'Taxeinformation {i}'), str(i))
        server, base = await serve(site)
        try:
            generate = uwc.AsyncWebCrawler._generate_report
            async def legacy_report(c):
                await generate(c)
                path = Path(c.output_dir) / 'crawl_report.json'
                data = json.loads(path.read_text(encoding='utf-8'))
                data.pop('document_processing_profile', None)
                uwc.atomic_write_text(str(path), json.dumps(data))
            with monkeypatch.context() as patch:
                patch.setattr(uwc.AsyncWebCrawler, '_conversion_profile', lambda c: 'legacy-profile')
                patch.setattr(uwc.AsyncWebCrawler, '_generate_report', legacy_report)
                await run_crawl(base, tmp_path, find_sitemap=False)
            before = (tmp_path / 'current.json').read_bytes()
            result = await run_crawl(base, tmp_path, find_sitemap=False, max_pages=1)
            assert result.report['publication_blocked']
            assert (tmp_path / 'current.json').read_bytes() == before
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert result.delivery_status() == 'success'
            assert len(list((published(tmp_path) / 'texter').glob('*_doc.md'))) == 5
        finally:
            await server.cleanup()
    run(go())


def test_pdf_table_band_does_not_interleave_following_prose_columns(tmp_path):
    source = tmp_path / 'mixed.pdf'
    doc = uwc.pymupdf.open()
    p = doc.new_page(width=600, height=800)
    for i in range(4):
        p.insert_text((50, 150 + i * 30), f'Avgift typ {i}', fontsize=10)
        p.insert_text((360, 150 + i * 30), f'{i + 1} 000 kr', fontsize=10)
        p.insert_text((50, 350 + i * 30), f'VANSTER{i} avgifter och villkor', fontsize=10)
        p.insert_text((330, 350 + i * 30), f'HOGER{i} kontakt och adresser', fontsize=10)
    doc.save(source)
    doc.close()
    text = uwc.DocumentConverter(str(tmp_path))._extract_pdf(str(source))
    assert text.index('Avgift typ 0') < text.index('1 000 kr') < text.index('Avgift typ 1')
    assert text.index('VANSTER3') < text.index('HOGER0')


@pytest.mark.parametrize('save_format', ['.md', '.json', '.txt'])
def test_language_filter_rechecks_unvisited_cached_sources(tmp_path, save_format):
    async def go():
        site = Site()
        populate(site)
        html, etag = site.pages['/p2']
        site.pages['/p2'] = (html.replace("lang='sv'", "lang='en'"), etag)
        server, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False, save_format=save_format)
            result = await run_crawl(base, tmp_path, max_pages=1, languages=['sv'],
                                     find_sitemap=False, save_format=save_format)
            assert len(list((published(tmp_path) / 'texter').glob('*' + save_format))) == 5
            assert result.report['wrong_language'] == [{'url': base + '/p2', 'language': 'en'}]
        finally:
            await server.cleanup()
    run(go())


def test_original_hash_is_reused_within_run_but_not_across_runs(tmp_path, monkeypatch):
    from test_crawler import make_pdf
    calls = []
    original_hash = uwc.file_sha256
    def measured(path):
        calls.append(str(path))
        return original_hash(path)
    monkeypatch.setattr(uwc, 'file_sha256', measured)
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'TEXT', [('/taxa.pdf', 'Taxa')]), 's')
        site.files['/taxa.pdf'] = (make_pdf('Taxeinformation'), 'pdf')
        server, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False)
            for _ in range(2):
                calls.clear()
                await run_crawl(base, tmp_path, find_sitemap=False)
                assert len(calls) == 1
        finally:
            await server.cleanup()
    run(go())


def test_changed_file_metadata_invalidates_per_run_hash(tmp_path):
    path = tmp_path / 'original.pdf'
    path.write_bytes(b'ONE')
    c = uwc.AsyncWebCrawler(config('https://test.se', tmp_path / 'out'))
    async def go():
        first = await c._original_hash(str(path))
        replacement = tmp_path / 'new.pdf'
        replacement.write_bytes(b'TWO')
        os.utime(replacement, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
        os.replace(replacement, path)
        assert await c._original_hash(str(path)) != first
    try:
        run(go())
    finally:
        c._close_log_handlers()


def test_pdf_conversion_imports_no_gui_web_or_office_libraries(tmp_path):
    from test_crawler import make_pdf
    source = tmp_path / 'taxa.pdf'
    source.write_bytes(make_pdf('Taxeinformation'))
    code = '''import document_conversion as d, json, sys
d.DocumentConverter(sys.argv[2], ocr=False)._extract_pdf(sys.argv[1])
print(json.dumps(sorted(name for name in sys.modules if name.split('.')[0] in
    {'tkinter', 'customtkinter', 'trafilatura', 'playwright', 'aiohttp', 'openpyxl', 'pptx', 'docx'})))'''
    result = subprocess.run([sys.executable, '-c', code, str(source), str(tmp_path)],
                            cwd=str(Path(uwc.__file__).parent), capture_output=True, text=True,
                            check=True, timeout=15)
    assert json.loads(result.stdout.splitlines()[-1]) == []


def test_session_probe_is_singleflight_and_expires(tmp_path, monkeypatch):
    c = uwc.AsyncWebCrawler(config('https://test.se', tmp_path))
    calls = []
    async def probe():
        calls.append(1)
        await asyncio.sleep(0.01)
        return True
    monkeypatch.setattr(c, '_verify_login_uncached', probe)
    async def go():
        assert all(await asyncio.gather(*(c._verify_login() for _ in range(8))))
        assert len(calls) == 1
        c._session_probe_cache = (time.monotonic() - 61, True)
        assert await c._verify_login() is True
        assert len(calls) == 2
    try:
        run(go())
    finally:
        c._close_log_handlers()


def test_throttling_and_recovery_only_affect_failed_host():
    limiter = uwc.PerDomainRateLimiter(2)
    limiter.slow_down(domain='a.se')
    assert limiter.domain_delays['a.se'] > limiter.base_delay
    assert limiter.domain_delays.get('b.se', limiter.delay) == limiter.base_delay
    slower = limiter.domain_delays['a.se']
    limiter.succeeded('b.se')
    assert limiter.domain_delays['a.se'] == slower
    limiter.succeeded('a.se')
    assert limiter.domain_delays['a.se'] < slower


def test_generation_retention_keeps_current_latest_and_reader_grace(tmp_path, monkeypatch):
    clock = [100000.0]
    monkeypatch.setattr(runtime.time, 'time', lambda: clock[0])
    generations = []
    for i in range(4):
        p = runtime.PublicationWorkspace(str(tmp_path), 'cache.db', f'run{i}')
        p.begin()
        Path(p.workdir, 'texter').mkdir(exist_ok=True)
        Path(p.workdir, 'texter', 'a.md').write_text(str(i))
        p.publish('success')
        generations.append(Path(p.generation_dir))
        assert p.prune(2, 1) == []
        p.release()
        clock[0] += 10
    assert all(folder.exists() for folder in generations)
    clock[0] += 3600
    p = runtime.PublicationWorkspace(str(tmp_path), 'cache.db', 'cleanup')
    p.begin()
    try:
        removed = p.prune(2, 1)
        assert len(removed) == 2
        assert not generations[0].exists() and not generations[1].exists()
        assert generations[2].exists() and generations[3].exists()
        assert published(tmp_path) == generations[3]
    finally:
        p.abort()


@pytest.mark.parametrize('options', [{'generation_keep': -1}, {'generation_keep': 1.5},
                                    {'generation_min_age_hours': 0}, {'generation_min_age_hours': 'NaN'}])
def test_retention_config_is_validated(options):
    errors, _ = uwc.validate_site_config({'start_url': 'https://test.se/', **options})
    assert errors


def test_failed_status_is_refused_at_publication_boundary(tmp_path):
    p = runtime.PublicationWorkspace(str(tmp_path), 'cache.db', 'failed')
    p.begin()
    try:
        with pytest.raises(ValueError):
            p.publish('failed')
        assert not (tmp_path / 'current.json').exists()
    finally:
        p.abort()


def test_retention_does_not_follow_alias_to_current_generation(tmp_path, monkeypatch):
    p = runtime.PublicationWorkspace(str(tmp_path), 'cache.db', 'current')
    p.begin()
    p.publish('success')
    alias = tmp_path / '.generations' / ('f' * 32)
    alias.mkdir()
    (alias / 'generation.json').write_text('{}')
    realpath = runtime.os.path.realpath
    def alias_resolution(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(alias)):
            return p.generation_dir
        return realpath(path, *args, **kwargs)
    monkeypatch.setattr(runtime.os.path, 'realpath', alias_resolution)
    try:
        assert p.prune(1, 0.01) == []
        assert Path(p.generation_dir).exists()
        assert alias.exists()
    finally:
        p.release()
