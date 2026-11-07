"""Acceptansprov för en crawl från en startadress till ett lokalt källindex."""

import io
import json
import sqlite3
from collections import defaultdict
from contextlib import closing

from docx import Document
from openpyxl import Workbook

from test_crawler import Site, page, published, read_changes, run, serve, uwc


SECTIONS = ('Bibliotek', 'Bygglov', 'Förskola', 'Idrott', 'Parkering',
            'Renhållning', 'Skola', 'Vatten', 'Äldreomsorg')
BOOKING = '/bibliotek/tjanst-1'
LIBRARY = '/bibliotek/tjanst-2'
PERMIT = '/bygglov/tjanst-1'
POOL = '/idrott/tjanst-1'
DOCUMENTS = {
    '/filer/oppettider.pdf': (LIBRARY, 'application/pdf'),
    '/filer/komplettering.docx': (
        PERMIT, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    '/filer/badhustaxa.xlsx': (
        POOL, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
}


class AcceptanceSite(Site):
    async def handle(self, request):
        response = await super().handle(request)
        if request.path in DOCUMENTS and response.status == 200:
            response.content_type = DOCUMENTS[request.path][1]
        return response


def add_documents(site):
    with uwc.pymupdf.open() as pdf:
        pdf.set_metadata({'title': 'Bibliotekets öppettider'})
        pdf.new_page().insert_text((60, 90), 'Välkommen till Testkommunens bibliotek.')
        pdf.new_page().insert_text((60, 90), 'Bokbussen stannar vid Björkbacka på tisdagar klockan 16.')
        site.files['/filer/oppettider.pdf'] = (pdf.tobytes(), '"pdf-v1"')
    doc = Document()
    doc.add_heading('Komplettering av bygglov', 1)
    doc.add_paragraph('Vid en komplettering krävs en situationsplan i skala 1:400.')
    with io.BytesIO() as buffer:
        doc.save(buffer)
        site.files['/filer/komplettering.docx'] = (buffer.getvalue(), '"docx-v1"')
    with closing(Workbook()) as workbook, io.BytesIO() as buffer:
        sheet = workbook.active
        sheet.title = 'Badhustaxa'
        sheet.append(['Biljettyp', 'Pris i kronor'])
        sheet.append(['Morgonsim senior', 85])
        sheet.append(['Kvällssim vuxen', 110])
        workbook.save(buffer)
        site.files['/filer/badhustaxa.xlsx'] = (buffer.getvalue(), '"xlsx-v1"')


def make_site():
    site = AcceptanceSite()
    sections = []
    for section in SECTIONS:
        section_url = '/' + uwc.slugify(section)
        sections.append((section_url, section))
        leaves = []
        for i in range(1, 11):
            path = f'{section_url}/tjanst-{i}'
            title = f'{section} tjänst {i}'
            leaves.append((path, title))
            links = [(doc_path, 'Läs bilagan') for doc_path, (referer, _) in DOCUMENTS.items()
                     if referer == path]
            body = f'Information om tjänst {i} inom {section.lower()} för boende i Testkommunen.'
            site.pages[path] = (page(title, body, links), f'"{section_url[1:]}-{i}-v1"')
        site.pages[section_url] = (page(section, f'Alla tjänster inom {section.lower()}.', leaves), '"v1"')
    site.pages['/'] = (page('Testkommunen', 'Hitta kommunens service och tillhörande bilagor.', sections), '"v1"')
    paragraphs = ''.join(
        f'<p>Avsnitt {i} om bokningen av grupprum. Bibliotekets besökare kan välja ett ledigt bord, '
        'studera tillsammans och boka utrustning med sitt bibliotekskort. Information om lokalerna '
        'finns vid receptionen och på bokningssidan.</p>' for i in range(1, 20)
    )
    site.pages[BOOKING] = (page('Boka grupprum', 'Grupprum för kvällsbokning stänger klockan 19.00.')
                           .replace('</main>', paragraphs + '<p>Övergångsrutinen heter Arkivrutin.</p></main>'),
                           '"booking-v1"')
    add_documents(site)
    return site


def read_corpus(generation):
    groups = defaultdict(list)
    ids = set()
    with (generation / 'chunks.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            chunk = json.loads(line)
            assert chunk['id'] not in ids
            ids.add(chunk['id'])
            assert chunk['content'].strip() and chunk['heading_path'] and chunk['context']
            groups[chunk['url']].append(chunk)
    for chunks in groups.values():
        assert [chunk['chunk_index'] for chunk in chunks] == list(range(1, len(chunks) + 1))
        assert all(chunk['total_chunks'] == len(chunks) for chunk in chunks)
    return groups


def index_sources(index, groups):
    index.executemany(
        'INSERT INTO passages VALUES (?, ?, ?, ?)',
        [(chunk['id'], url, chunk.get('page_url', url), chunk['content'])
         for url, chunks in groups.items() for chunk in chunks],
    )


def check_sources(index, base, closing_time):
    checks = (
        ('grupprum AND kvällsbokning', BOOKING, closing_time),
        ('bokbussen AND Björkbacka', '/filer/oppettider.pdf#page=2', 'tisdagar klockan 16'),
        ('komplettering AND situationsplan', '/filer/komplettering.docx', '1:400'),
        ('morgonsim AND senior', '/filer/badhustaxa.xlsx',
         'Tabellrad. Biljettyp: Morgonsim senior. Pris i kronor: 85.'),
    )
    for query, citation, answer in checks:
        row = index.execute('SELECT citation, content FROM passages WHERE passages MATCH ? '
                            'ORDER BY rank LIMIT 1', (query,)).fetchone()
        assert row is not None, query
        assert row[0] == base + citation and answer in row[1], (query, row)


def test_main_workflow_from_one_url_to_searchable_sources(tmp_path):
    site = make_site()
    settings = tmp_path / 'sites.json'
    output = tmp_path / 'out'
    site_output = output / 'acceptans'

    async def go():
        runner, base = await serve(site)
        settings.write_text(json.dumps({
            'name': 'acceptans', 'start_url': base + '/', 'delay': 0.05, 'concurrency': 8,
            'find_sitemap': False, 'use_hybrid': True, 'allow_private_hosts': True,
            'download_docs': True, 'convert_docs_to_md': True, 'ocr': False,
        }), encoding='utf-8')
        try:
            assert await uwc.run_cli_mode(str(settings), output_dir=str(output)) == 0
            first = published(site_output)
            before = read_corpus(first)
            assert len(site.pages) == 100
            assert set(before) == {base + path for path in (*site.pages, *DOCUMENTS)}
            assert all(chunk['source_type'] == 'page' for path in site.pages for chunk in before[base + path])
            for path, (referer, _) in DOCUMENTS.items():
                assert all(chunk['source_type'] == 'document' and chunk['referer_url'] == base + referer
                           for chunk in before[base + path])
            pdf_path, = (first / 'texter').glob('oppettider_*_doc.md')
            pdf_text = pdf_path.read_text(encoding='utf-8')
            pdf_url = base + '/filer/oppettider.pdf'
            assert f'\n\nPDF-sida 1. Sidlänk: <{pdf_url}#page=1>\n\nVälkommen' in pdf_text
            assert f'\n\nPDF-sida 2. Sidlänk: <{pdf_url}#page=2>\n\nBokbussen' in pdf_text
            assert all('Sidlänk:' not in chunk['content'] for chunk in before[pdf_url])

            changed = base + BOOKING
            assert len(before[changed]) > 1
            with closing(sqlite3.connect(':memory:')) as index:
                index.execute('CREATE VIRTUAL TABLE passages USING fts5('
                              'id UNINDEXED, url UNINDEXED, citation UNINDEXED, content)')
                index_sources(index, before)
                check_sources(index, base, '19.00')
                assert index.execute("SELECT count(*) FROM passages WHERE passages MATCH 'Arkivrutin'").fetchone()[0]

                site.pages[BOOKING] = (page('Boka grupprum', 'Grupprum för kvällsbokning stänger klockan 21.00.'),
                                       '"booking-v2"')
                assert await uwc.run_cli_mode(str(settings), output_dir=str(output)) == 0
                second = published(site_output)
                after = read_corpus(second)
                report = json.loads((second / 'crawl_report.json').read_text(encoding='utf-8'))
                assert first != second and report['status'] == 'success'
                assert report['stats']['pages_not_modified_304'] == 99
                assert report['stats']['documents_downloaded'] == 0
                assert report['chunk_export']['reused_sources'] == 102
                assert set(before) == set(after)
                assert {url for url in after if after[url] != before[url]} == {changed}
                assert len(after[changed]) == 1
                updated = [event['url'] for event in read_changes(second) if event['event'] == 'updated']
                assert updated == [changed]
                index.execute('DELETE FROM passages WHERE url = ?', (updated[0],))
                index_sources(index, {updated[0]: after[updated[0]]})
                check_sources(index, base, '21.00')
                assert index.execute("SELECT count(*) FROM passages WHERE passages MATCH 'Arkivrutin'").fetchone() == (0,)
                total = sum(len(chunks) for chunks in after.values())
                assert index.execute('SELECT count(*), count(DISTINCT id) FROM passages').fetchone() == (total, total)
                assert read_corpus(first) == before
        finally:
            await runner.cleanup()

    run(go())
