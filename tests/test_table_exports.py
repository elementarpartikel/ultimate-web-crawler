"""Regressioner för tabeller och läsbara radstycken i exporten."""
import io
import json

import pytest
from openpyxl import Workbook

from crawler_text import add_table_row_text
from test_crawler import Site, page, published, run, run_crawl, serve


def source_chunks(output, url, source_type):
    lines = (published(output) / 'chunks.jsonl').read_text(encoding='utf-8').splitlines()
    return [row['content'] for row in map(json.loads, lines)
            if row['url'] == url and row['source_type'] == source_type]


@pytest.mark.parametrize('save_format', ['.md', '.json', '.txt'])
def test_html_fallback_keeps_columns_and_cell_content_in_published_chunks(tmp_path, save_format):
    site = Site()
    rows = ''.join(f'<tr><td>Tillstånd för kommunal parkering i område {i}</td><td>{i}</td></tr>'
                   for i in range(50))
    table = ('<table><thead><tr><th>Avgiftsområde</th></tr></thead><tbody>'
             '<tr><td><span>Zon A |\n  B</span><br>Helg</td><td>1250 kronor</td></tr>'
             + rows + '<tr><td>Avgiftsfri rad</td></tr></tbody></table>')
    html = page('Parkeringsavgifter', 'Tabellen visar avgifter per område.')
    site.pages['/'] = (html.replace('</main>', table + '</main>'), 'start')

    async def go():
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, use_trafilatura=False, save_format=save_format)
            chunks = source_chunks(tmp_path, base + '/', 'page')
            table_chunks = [chunk for chunk in chunks if '| Tillstånd för kommunal parkering i område' in chunk]
            assert len(table_chunks) > 1
            for chunk in table_chunks:
                assert '| Avgiftsområde |  |\n|---|---|' in chunk
            assert any(r'| Zon A \| B<br>Helg | 1250 kronor |' in chunk.splitlines() for chunk in chunks)
            assert any('| Avgiftsfri rad |  |' in chunk.splitlines() for chunk in chunks)
            content = '\n'.join(chunks)
            assert 'Tabellrad. Avgiftsområde: Zon A | B / Helg. Kolumn 2: 1250 kronor.' in content
            assert 'Tabellrad. Avgiftsområde: Tillstånd för kommunal parkering i område 0. Kolumn 2: 0.' in content
            assert 'Tabellrad. Avgiftsområde: Avgiftsfri rad.' in content
        finally:
            await runner.cleanup()

    run(go())


def test_excel_keeps_columns_and_cell_content_in_published_chunks(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Parkering'
    sheet.append(['Avgiftsområde'])
    sheet.append(['Zon A | B\nHelg\rKväll\r\nExtra', '1250 kronor'])
    for i in range(50):
        sheet.append([f'Tillstånd för kommunal parkering i område {i}', i])
    sheet.append(['Avgiftsfri rad'])
    with io.BytesIO() as buffer:
        workbook.save(buffer)
        site = Site()
        site.files['/avgifter.xlsx'] = (buffer.getvalue(), 'xlsx')
    workbook.close()
    site.pages['/'] = (page('Start', 'Här finns kommunens taxa.', [('/avgifter.xlsx', 'Avgifter')]), 'start')

    async def go():
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path)
            chunks = source_chunks(tmp_path, base + '/avgifter.xlsx', 'document')
            assert len(chunks) > 1
            table_chunks = [chunk for chunk in chunks if '| Tillstånd för kommunal parkering i område' in chunk]
            assert len(table_chunks) > 1
            for chunk in table_chunks:
                assert '| Avgiftsområde |  |\n|---|---|' in chunk
            assert any(r'| Zon A \| B<br>Helg<br>Kväll<br>Extra | 1250 kronor |' in chunk.splitlines()
                       for chunk in chunks)
            assert any('| Tillstånd för kommunal parkering i område 0 | 0 |' in chunk.splitlines()
                       for chunk in chunks)
            assert any('| Avgiftsfri rad |  |' in chunk.splitlines() for chunk in chunks)
            row = 'Tabellrad. Avgiftsområde: Zon A | B / Helg / Kväll / Extra. Kolumn 2: 1250 kronor.'
            doc_path, = (published(tmp_path) / 'texter').glob('*_doc.md')
            document = doc_path.read_text(encoding='utf-8')
            assert f'\n\n{row}\n\n' in document
            assert f'**Dokument-URL:** {base}/avgifter.xlsx' in document
            assert any(row in chunk.splitlines() for chunk in chunks)
            assert any('Tabellrad. Avgiftsområde: Tillstånd för kommunal parkering i område 49. Kolumn 2: 49.'
                       in chunk.splitlines() for chunk in chunks)
        finally:
            await runner.cleanup()

    run(go())


def test_trafilatura_exports_table_and_labelled_rows(tmp_path):
    pytest.importorskip('trafilatura')
    site = Site()
    table = ('<table><tr><th>Biljettyp</th><th>Pris i kronor</th></tr>'
             '<tr><td>Morgonsim senior</td><td>85</td></tr></table>')
    site.pages['/'] = (page('Badhustaxa', 'Här är priserna för kommunens badhus.').replace(
        '</main>', table + '</main>'), 'start')

    async def go():
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, use_trafilatura=True)
            doc_path, = (published(tmp_path) / 'texter').glob('*.md')
            source = doc_path.read_text(encoding='utf-8')
            assert '| Morgonsim senior | 85 |' in source
            row = 'Tabellrad. Biljettyp: Morgonsim senior. Pris i kronor: 85.'
            assert f'\n\n{row}\n\n' in source
            assert any(row in chunk.splitlines() for chunk in source_chunks(tmp_path, base + '/', 'page'))
        finally:
            await runner.cleanup()

    run(go())


def test_table_row_text_keeps_escaped_cells_and_skips_code():
    example = '```md\n| Namn | Pris |\n|---|---|\n| Exempel | 999 |\n```'
    table = r'| Zon A \| B | 85 |' + '\n' + r'| Väg C:\\Test | 110 |'
    result = add_table_row_text(f'{example}\n\n{table}', allow_headerless=True)
    assert result.startswith(f'{example}\n\n{table}\n\n')
    assert 'Tabellrad. Kolumn 1: Zon A | B. Kolumn 2: 85.' in result
    assert r'Tabellrad. Kolumn 1: Väg C:\Test. Kolumn 2: 110.' in result
    assert result.count('Tabellrad.') == 2
