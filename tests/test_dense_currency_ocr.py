"""Regressioner för automatisk omläsning av täta kronor- och örestabeller."""

import pytest

from pdf_dense_currency import _verified_rows
from test_crawler import run, uwc


def test_dense_amounts_keep_printed_dash_and_leading_zero():
    values = [['125', '05', '310', '—'], ['260', '99', '470', '10'], ['386', '04', '780', '10']]
    result = _verified_rows((values, values), [1901, 1902], ['Hyra', 'Bidrag', 'Summa kronor'])
    assert '| Hyra | 125 | 05 | 310 | — |' in result
    assert '| Summa kronor | 386 | 04 | 780 | 10 |' in result


@pytest.mark.parametrize(('cell', 'both'), [('15', False), ('', False), ('-', False), ('15', True)])
def test_dense_amounts_reject_disagreement_missing_values_and_wrong_totals(cell, both):
    a = [['125', '05', '310', '—'], ['260', '99', '470', '10'], ['386', '04', '780', '10']]
    b = [row.copy() for row in a]
    b[0][1] = cell
    if both:
        a[0][1] = cell
    assert _verified_rows((a, b), [1901, 1902], ['Hyra', 'Bidrag', 'Summa kronor']) is None


def make_dense_scan(path, *, pages=1):
    rows = [
        ('Hyra', ('1240', '32', '1560', '78', '1220', '21')),
        ('Bidrag', ('2710', '18', '2915', '12', '2005', '59')),
        ('Avgifter', ('1060', '47', '1002', '28', '1401', '50')),
        ('Summa kronor', ('5010', '97', '5478', '18', '4627', '30')),
    ]
    columns = (210, 250, 265, 305, 320, 360, 375)
    fitz = uwc.pymupdf
    with fitz.open() as template, fitz.open() as scan:
        original = template.new_page(width=470, height=365)
        original.insert_text((50, 61), 'Kommunens utgifter per år', fontsize=15)
        for year, x in zip((1901, 1902, 1903), columns[::2]):
            title = f'År {year}.'
            center = x + (55 - fitz.get_text_length(title, fontsize=12)) / 2
            original.insert_text((center, 104), title, fontsize=12)
        for x in (45, *columns):
            original.draw_line((x, 120), (x, 245), width=0.55)
        for y in (120, 211):
            original.draw_line((45, y), (375, y), width=0.8)
        for y, (label, values) in zip((143, 169, 195, 231), rows):
            original.insert_text((50, y), label, fontsize=11)
            for x, value in zip(columns[1:], values):
                right = x - 2.1 - fitz.get_text_length(value, fontsize=10)
                original.insert_text((right, y), value, fontsize=10)
        original.insert_text((50, 269), 'Text efter tabellen om nästa ämne.', fontsize=12)
        image = original.get_pixmap(dpi=180)
        for _ in range(pages):
            page = scan.new_page(width=470, height=365)
            page.insert_image(page.rect, pixmap=image)
        scan.save(path, deflate=True)


def test_failed_dense_reread_keeps_raw_text_and_counts_against_budget(tmp_path, monkeypatch):
    converter = uwc.DocumentConverter(str(tmp_path), ocr_max_pages=2)
    if not converter.ocr_available():
        pytest.skip('Tesseracts språkdata saknas i testmiljön')
    source = tmp_path / 'avgifter.pdf'
    make_dense_scan(source, pages=2)
    attempts = []
    read = converter._ocr_textpage

    def fail_reread(page, *, full, dpi=200):
        attempts.append(dpi)
        if dpi != 288:
            return read(page, full=full, dpi=dpi)
        return None

    monkeypatch.setattr(converter, '_ocr_textpage', fail_reread)
    content = converter._extract_pdf(str(source))
    assert attempts == [200, 288]
    assert '## Sida 1' in content and '## Sida 2' not in content
    assert '1901 (öre)' not in content and 'Text efter tabellen' in content


def test_dense_scan_worker_recovers_cells_within_ocr_budget(tmp_path):
    converter = uwc.DocumentConverter(str(tmp_path))
    if not converter.ocr_available():
        pytest.skip('Tesseracts språkdata saknas i testmiljön')
    source = tmp_path / 'avgifter.pdf'
    make_dense_scan(source)
    url = 'https://test.invalid/avgifter.pdf'

    async def go():
        runner = uwc.ConversionProcessRunner(str(tmp_path / 'jobs'))
        try:
            markdown = converter.md_filename(url, source.name)
            baseline = await runner.run(str(source), url, {}, {'ocr_max_pages': 1}, markdown, 5_000_000)
            assert baseline['text'], baseline['error']
            assert '1901 (öre)' not in baseline['text']
            result = await runner.run(str(source), url, {}, {'ocr_max_pages': 2}, markdown, 5_000_000)
            assert result['text'], result['error']
            assert result['ocr'] and '**Textkälla:** OCR' in result['text']
            _, chunks = uwc.markdown_file_to_chunks(result['text'], is_document=True)
            content = '\n'.join(chunk['content'] for chunk in chunks)
            assert all(chunk['page_url'] == url + '#page=1' for chunk in chunks)
            header = '| Post | 1901 (kr) | 1901 (öre) | 1902 (kr) | 1902 (öre) | 1903 (kr) | 1903 (öre) |'
            assert header in content
            assert [line for line in content.splitlines() if line.startswith('| ') and line != header] == [
                '| Hyra | 1 240 | 32 | 1 560 | 78 | 1 220 | 21 |',
                '| Bidrag | 2 710 | 18 | 2 915 | 12 | 2 005 | 59 |',
                '| Avgifter | 1 060 | 47 | 1 002 | 28 | 1 401 | 50 |',
                '| Summa kronor | 5 010 | 97 | 5 478 | 18 | 4 627 | 30 |',
            ]
            assert content.index('Kommunens utgifter per år') < content.index(header) < content.index('Text efter')
        finally:
            await runner.close()

    run(go())
