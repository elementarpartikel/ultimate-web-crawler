"""Regressioner för innehållsstyrande URL-parametrar och publicerade källor."""
import csv
import json
import sqlite3

import pytest
from aiohttp import web

from test_crawler import Site, make_pdf, page, published, run, run_crawl, serve, uwc


class QuerySite(Site):
    def __init__(self, parameter, include_bare=False, document_path='/avgifter.pdf'):
        super().__init__()
        self.document_path = document_path
        self.requests = []
        self.contents = {}
        variants = [('norr', 'Norra kommundelen'), ('soder', 'Sodra kommundelen')]
        if include_bare:
            variants.append(('', 'Hela kommunen'))
        links = []
        for value, title in variants:
            suffix = f'?{parameter}={value}' if value else ''
            path = '/taxa' + suffix
            document = document_path + suffix
            links.extend([(path, title), (document, 'Dokument ' + title)])
            self.contents[path] = (page(title, f'Taxan gäller {title}.'), None)
            self.contents[document] = (make_pdf(f'Avgifter for {title}'), 'application/pdf')
        self.pages['/'] = (page('Start', 'Här finns kommunens olika taxor.', links), '"start"')

    async def handle(self, request):
        if request.path not in ('/taxa', self.document_path):
            return await super().handle(request)
        validator = request.headers.get('If-None-Match')
        self.requests.append((request.path_qs, validator))
        if request.path_qs not in self.contents:
            return web.Response(status=404)
        body, content_type = self.contents[request.path_qs]
        # En ETag behöver bara vara unik för samma URL.
        headers = {'ETag': '"taxa"'}
        if validator == headers['ETag']:
            return web.Response(status=304, headers=headers)
        if content_type:
            return web.Response(body=body, content_type=content_type, headers=headers)
        return web.Response(text=body, content_type='text/html', headers=headers)


def records(generation, name='chunks.jsonl'):
    return [json.loads(line) for line in (generation / name).read_text(encoding='utf-8').splitlines()]


def assert_variants(generation, base, parameter, include_bare=False, document_path='/avgifter.pdf'):
    chunks = records(generation)
    assert len({chunk['id'] for chunk in chunks}) == len(chunks)
    expected = {
        f'{base}{path}?{parameter}={value}': title
        for path in ('/taxa', document_path)
        for value, title in [('norr', 'Norra kommundelen'), ('soder', 'Sodra kommundelen')]
    }
    if include_bare:
        expected.update({base + path: 'Hela kommunen' for path in ('/taxa', document_path)})
    variants = [chunk for chunk in chunks if chunk['url'] != base + '/']
    assert {chunk['url'] for chunk in variants} == expected.keys()
    for url, title in expected.items():
        assert title in ' '.join(chunk['content'] for chunk in variants if chunk['url'] == url)
    manifest = json.loads(next(generation.glob('manifest_*.json')).read_text(encoding='utf-8'))
    assert {doc['download_url'] for doc in manifest['documents']} == {
        url for url in expected if url.startswith(base + document_path)}


@pytest.mark.parametrize('parameter', ['ref', 'source', 'state'])
def test_query_variants_have_distinct_page_and_document_sources(tmp_path, parameter):
    async def go():
        site = QuerySite(parameter)
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            assert_variants(published(tmp_path), base, parameter)
            site.requests.clear()
            await run_crawl(base, tmp_path)
            assert set(site.requests) == {(path, '"taxa"') for path in site.contents}
            assert_variants(published(tmp_path), base, parameter)
        finally:
            await runner.cleanup()

    run(go())


@pytest.mark.parametrize(('include_bare', 'document_path'), [
    (False, '/avgifter.pdf'), (True, '/avgifter.pdf'), (False, '/dokumentfil'),
])
def test_upgrade_splits_collapsed_cache_and_preserves_existing_page_citations(
        tmp_path, include_bare, document_path):
    async def go():
        parameter = 'state'
        site = QuerySite(parameter, include_bare=include_bare, document_path=document_path)
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, ignore_query_params=[parameter])
            old_generation = published(tmp_path)
            old_page_url = base + '/taxa?state=norr'
            old_chunks = records(old_generation)
            assert {chunk['url'] for chunk in old_chunks} == {
                base + '/', old_page_url, base + document_path}
            old_ids = {chunk['id'] for chunk in old_chunks if chunk['url'] == old_page_url}
            old_name = uwc.stable_filename(base + '/taxa', '.md')
            old_text = (old_generation / 'texter' / old_name).read_bytes()
            cache = next(old_generation.glob('*_cache.db'))
            # Efterlikna en dokumentcache från före uppgraderingen.
            with sqlite3.connect(cache) as db:
                db.execute('ALTER TABLE doc_cache DROP COLUMN fetch_url')
                db.commit()
            db.close()
            old_db = cache.read_bytes()

            site.requests.clear()
            await run_crawl(base, tmp_path)
            generation = published(tmp_path)
            assert generation != old_generation
            assert_variants(generation, base, parameter, include_bare, document_path)
            assert ('/taxa?state=norr', '"taxa"') in site.requests
            assert ('/taxa?state=soder', None) in site.requests
            assert (document_path, None) in site.requests
            if include_bare:
                assert ('/taxa', None) in site.requests
            new_name = uwc.stable_filename(old_page_url, '.md')
            assert (generation / 'texter' / new_name).read_bytes() == old_text
            assert (old_generation / 'texter' / old_name).read_bytes() == old_text
            assert cache.read_bytes() == old_db
            assert {chunk['id'] for chunk in records(generation) if chunk['url'] == old_page_url} == old_ids
            with (generation / 'index.csv').open(encoding='utf-8-sig', newline='') as source:
                index = {row['URL']: row['Filnamn'] for row in csv.DictReader(source)}
            assert index[old_page_url] == new_name
            assert (base + '/taxa' in index) == include_bare
            changes = records(generation, 'changes.jsonl')
            assert any(change['event'] == 'updated' and change.get('file') == new_name
                       and change.get('previous_file') == old_name for change in changes)
            assert all('?' not in change['url'] for change in changes)
            if not include_bare:
                assert any(change['event'] == 'document_text_removed'
                           and change.get('reason') == 'HTTP 404' for change in changes)
        finally:
            await runner.cleanup()

    run(go())


def test_limited_upgrade_keeps_unvisited_page_under_its_recovered_key(tmp_path):
    async def go():
        site = Site()
        path = '/taxa?ref=kommun&state=norr'
        site.pages['/'] = (page('Start', 'Här finns taxan.', [(path, 'Taxa')]), '"start"')
        site.pages['/taxa'] = (page('Taxa', 'Taxa för den norra kommundelen.'), '"taxa"')
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, keep_query_params=['ref'], ignore_query_params=['state'])
            old_generation = published(tmp_path)
            old_chunks = records(old_generation)
            site.hits.clear()
            await run_crawl(base, tmp_path, max_pages=1)
            generation = published(tmp_path)
            assert generation != old_generation
            assert not any(hit[0] == '/taxa' for hit in site.hits)
            assert records(generation) == old_chunks
            expected_url = base + path
            assert (generation / 'texter' / uwc.stable_filename(expected_url, '.md')).is_file()
            assert not (generation / 'texter' / uwc.stable_filename(base + '/taxa?ref=kommun', '.md')).exists()
        finally:
            await runner.cleanup()

    run(go())
