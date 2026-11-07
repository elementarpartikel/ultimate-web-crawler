"""Syntetiska PDF-fixturer för tabellfortsättningar, spalter och blandad OCR."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_crawler import run, uwc


def draw_table(page, rows, top, x=(40, 230, 380, 560)):
    bottom = top + 17 * len(rows)
    for col in x:
        page.draw_line((col, top), (col, bottom), width=0.6)
    for index, row in enumerate(rows):
        y = top + 17 * index
        page.draw_line((x[0], y), (x[-1], y), width=0.6)
        for col, value in zip(x, row):
            if value:
                page.insert_text((col + 4, y + 12), value, fontsize=9)
    page.draw_line((x[0], bottom), (x[-1], bottom), width=0.6)


@pytest.mark.parametrize('repeated_header', [False, True])
def test_continued_table_uses_real_header_and_group_without_repeating_the_first_row(tmp_path, repeated_header):
    source = tmp_path / 'avgifter.pdf'
    title = ('Kategori', 'Variant', 'Avgift i kronor')
    with uwc.pymupdf.open() as doc:
        first = doc.new_page(width=600, height=800)
        draw_table(first, [title, ('Parkering', 'Bil', '500'), ('', 'Buss', '900')], 685)
        second = doc.new_page(width=600, height=800)
        rows = [('', 'FORTSATTNING', '1350')]
        rows += [(f'Kategori {i}', f'Avgift for kommunens arende {i}', str(i + 2000)) for i in range(40)]
        draw_table(second, ([title] if repeated_header else []) + rows, 75)
        doc.save(source)
    url = 'https://test.invalid/avgifter.pdf?ref=kommun'
    output = uwc.DocumentConverter(str(tmp_path), ocr=False).convert(str(source), url)
    assert output
    md = Path(output).read_text(encoding='utf-8')
    page_two = md.split(' – sida 2\n', 1)[1]
    assert '| Kategori | Variant | Avgift i kronor |' in page_two
    assert '| Parkering | FORTSATTNING | 1350 |' in page_two
    assert '900' not in page_two
    assert md.count(f'PDF-sida 2. Sidlänk: <{url}#page=2>') == md.count(' – sida 2\n') > 1
    assert '#page=1' not in page_two
    _, chunks = uwc.markdown_file_to_chunks(md, is_document=True)
    second_chunks = [chunk for chunk in chunks if chunk['page_number'] == 2]
    assert len(second_chunks) > 1
    second_lines = [line for chunk in second_chunks for line in chunk['content'].splitlines()]
    assert second_lines.count('| Parkering | FORTSATTNING | 1350 |') == 1
    assert second_lines.count('Tabellrad. Kategori: Parkering. Variant: FORTSATTNING. Avgift i kronor: 1350.') == 1
    assert all(c['page_url'] == url + '#page=2' for c in second_chunks)
    table_chunks = [chunk for chunk in second_chunks if '|---|---|---|' in chunk['content']]
    assert len(table_chunks) > 1
    assert all('| Kategori | Variant | Avgift i kronor |' in c['content'] for c in table_chunks)


def test_independent_table_on_next_page_keeps_its_own_header(tmp_path):
    source = tmp_path / 'separata-tabeller.pdf'
    with uwc.pymupdf.open() as doc:
        first = doc.new_page(width=600, height=800)
        draw_table(first, [('Kategori', 'Variant', 'Avgift'), ('Parkering', 'Bil', '500'), ('', 'Buss', '900')], 685)
        second = doc.new_page(width=600, height=800)
        draw_table(second, [('Område', 'Period', 'Antal'), ('Torg', 'Sommar', '15'), ('Hamnen', 'Vinter', '10')], 75)
        doc.save(source)
    text = uwc.DocumentConverter(str(tmp_path), ocr=False)._extract_pdf(str(source))
    page_two = text.split('## Sida 2', 1)[1]
    assert '| Område | Period | Antal |' in page_two
    assert 'Parkering' not in page_two


def test_pdf_grouped_headers_follow_values_across_chunks(tmp_path):
    source = tmp_path / 'grupprubriker.pdf'
    columns = (40, 180, 265, 350, 435, 520)
    top, second, body_top = 90, 114, 132
    labels = ('Omrade', 'Utan moms', 'Med moms', 'Utan moms', 'Med moms')
    rows = [(f'Kommunal avgift for omrade {i}', str(i + 1000), str(i + 2000),
             str(i + 3000), str(i + 4000)) for i in range(42)]
    bottom = body_top + 15 * len(rows)
    with uwc.pymupdf.open() as doc:
        page = doc.new_page(width=600, height=820)
        for left, right in zip(columns, columns[1:]):
            page.draw_rect(uwc.pymupdf.Rect(left + 5, second, right - 5, bottom),
                           color=None, fill=(0.9, 0.9, 0.9))
        for i, x in enumerate(columns):
            page.draw_line((x, second if i in (2, 4) else top), (x, bottom), width=0.6)
        for y in (top, second, *range(body_top, bottom + 1, 15)):
            page.draw_line((columns[0], y), (columns[-1], y), width=0.6)
        for col, text in ((0, 'Fast avgift'), (1, 'Dricksvatten'), (3, 'Spillvatten')):
            page.insert_text((columns[col] + 8, top + 15), text, fontsize=9)
        for i, row in enumerate([labels, *rows]):
            for x, text in zip(columns, row):
                page.insert_text((x + 8, body_top - 6 + 15 * i), text, fontsize=9)
        doc.save(source)

    url = 'https://test.invalid/grupprubriker.pdf'
    output = uwc.DocumentConverter(str(tmp_path), ocr=False).convert(str(source), url)
    assert output
    _, chunks = uwc.markdown_file_to_chunks(Path(output).read_text(encoding='utf-8'), is_document=True)
    table_chunks = [chunk for chunk in chunks if '| Kommunal avgift for omrade' in chunk['content']]
    assert len(table_chunks) > 1
    header = ('| Fast avgift<br>Omrade | Dricksvatten<br>Utan moms | Dricksvatten<br>Med moms '
              '| Spillvatten<br>Utan moms | Spillvatten<br>Med moms |')
    assert all(header in chunk['content'] for chunk in table_chunks)
    assert all(chunk['page_url'] == url + '#page=1' for chunk in table_chunks)
    assert '| Kommunal avgift for omrade 41 | 1041 | 2041 | 3041 | 4041 |' in table_chunks[-1]['content']
    expected = ('Tabellrad. Fast avgift / Omrade: Kommunal avgift for omrade 41. '
                'Dricksvatten / Utan moms: 1041. Dricksvatten / Med moms: 2041. '
                'Spillvatten / Utan moms: 3041. Spillvatten / Med moms: 4041.')
    assert any(expected in chunk['content'].splitlines() and chunk['page_url'] == url + '#page=1' for chunk in chunks)


def test_pdf_actualtext_stays_in_its_own_salary_table_cell(tmp_path):
    source = tmp_path / 'loner.pdf'
    with uwc.pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        draw_table(page, [('Kategori', 'Grupp', 'Antal'), ('', 'Totalt', '84'),
                          ('Handlaggare', 'Kvinna', '71')], 100, (40, 230, 380, 560))
        visible = 'Administrativ assistent (K)'
        left = 229 - uwc.pymupdf.get_text_length(visible, fontsize=9)
        page.insert_text((left, 129), visible, fontsize=9)
        stream = page.get_contents()[-1]
        label = 'Tillganglig yrkestitel med annat namn'
        encoded = ('\ufeff' + label).encode('utf-16-be').hex().encode('ascii')
        doc.update_stream(stream, b'/Span << /ActualText <' + encoded + b'> >> BDC\n'
                          + doc.xref_stream(stream) + b'\nEMC\n')
        doc.save(source)

    text = uwc.DocumentConverter(str(tmp_path), ocr=False)._extract_pdf(str(source))
    assert f'| {label} | Totalt | 84 |' in text
    assert '| Handlaggare | Kvinna | 71 |' in text


def test_pdf_strict_grid_cannot_merge_rows_or_change_an_amount(tmp_path):
    converter = uwc.DocumentConverter(str(tmp_path), ocr=False)
    bounds = (10, 10, 300, 80)

    def table(width, edges):
        return SimpleNamespace(
            col_count=width, bbox=bounds, header=SimpleNamespace(external=False),
            rows=[SimpleNamespace(bbox=(10, top, 300, bottom)) for top, bottom in zip(edges, edges[1:])],
        )

    source = table(4, [10, 30, 55, 80])
    strict = table(3, [10, 30, 55, 80])
    rows = [['Avgift', '', 'Exklusive', 'Inklusive'], ['Hamtning', '', '500', '625'],
            ['Behandling', '', '800', '1000']]
    cleaned = [[row[0], *row[2:]] for row in rows]
    assert converter._pdf_strict_table_matches(source, rows, strict, cleaned)
    assert not converter._pdf_strict_table_matches(
        source, rows, table(3, [10, 30, 80]), [cleaned[0], ['Hamtning och behandling', '500', '1000']])
    cleaned[-1][-1] = '1001'
    assert not converter._pdf_strict_table_matches(source, rows, strict, cleaned)
    cleaned[-1][-1] = '1000'
    with_prose = table(4, [10, 30, 55, 80, 105])
    with_prose.bbox = (10, 10, 300, 105)
    assert converter._pdf_strict_table_matches(
        with_prose, [*rows, ['Beskrivande text under tabellen', None, None, None]], strict, cleaned)


@pytest.mark.parametrize('contradictory_color', [False, True])
def test_pdf_vat_matrix_uses_legend_and_cell_extent(tmp_path, contradictory_color):
    source = tmp_path / 'matris.pdf'
    with uwc.pymupdf.open() as doc:
        page = doc.new_page(width=540, height=600)
        for x, start, end in ((40, 80, 270), (480, 80, 270), (110, 135, 215),
                              (175, 105, 270), (325, 135, 175), (325, 215, 270)):
            page.draw_line((x, start), (x, end))
        for y in (80, 105, 135, 175, 215, 248, 270):
            page.draw_line((40, y), (480, y))
        for left, top in ((325, 135), (175, 175)):
            page.draw_rect(uwc.pymupdf.Rect(left + 1, top + 1, 479, top + 19),
                           color=None, fill=(0.85, 0.85, 0.85))
            if contradictory_color:
                page.draw_rect(uwc.pymupdf.Rect(left + 1, top + 20, 479, top + 39),
                               color=None, fill=(0.6, 0.9, 0.6))
        for x, y, label in (
            (45, 94, 'Kommunala nyttoavgifter'), (45, 120, 'Prisklass och volym'),
            (181, 120, 'grå ruta = exkl. moms, vit ruta = inkl. moms'),
            (48, 150, '2'), (116, 150, '2 000'), (340, 150, '1 200 kr'), (340, 168, '1 500 kr'),
            (48, 191, '1'), (116, 191, '600'), (200, 191, '800 kr'), (200, 208, '1 000 kr'),
            (45, 234, 'Mätarinstallation'), (190, 234, 'Q 4'), (335, 234, 'Q 10'),
            (45, 263, 'Mätarklass'), (190, 263, 'A'), (335, 263, 'B'),
        ):
            page.insert_text((x, y), label, fontsize=9)
        doc.save(source)
    url = 'https://test.invalid/matris.pdf'
    converted = uwc.DocumentConverter(str(tmp_path), ocr=False).convert(str(source), url)
    _, chunks = uwc.markdown_file_to_chunks(Path(converted).read_text(encoding='utf-8'), is_document=True)
    text = '\n'.join(chunk['content'] for chunk in chunks)
    assert all(chunk['page_url'] == url + '#page=1' for chunk in chunks)
    if contradictory_color:
        assert '| 2 | 2 000 | B | Q 10 | 1 200 kr | 1 500 kr |' not in text
        assert '1 200 kr' in text and '1 500 kr' in text
    else:
        assert '| 2 | 2 000 | B | Q 10 | 1 200 kr | 1 500 kr |' in text
        assert '| 1 | 600 | A | Q 4 | 800 kr | 1 000 kr |' in text
        assert '| 1 | 600 | B | Q 10 | 800 kr | 1 000 kr |' in text
        assert '| 2 | 2 000 | A |' not in text


def test_pdf_stacked_amounts_preserve_labels_and_leave_ambiguous_pairs_unassigned(tmp_path):
    source = tmp_path / 'abonnemang.pdf'
    with uwc.pymupdf.open() as doc:
        page = doc.new_page(width=560, height=720)
        xs = (40, 220, 325, 410, 500)
        for x in xs:
            page.draw_line((x, 90), (x, 350))
        for y in (90, 115, 350):
            page.draw_line((xs[0], y), (xs[-1], y))
        for x, title in zip(xs, ('Abonnemang', 'Avgift per', 'Ex. moms', 'Ink. moms')):
            page.insert_text((x + 5, 105), title, fontsize=9)
        for y, name in ((142, 'Dygnshyra'), (155, 'Dimension Q 10'), (168, 'Dimension Q 16'),
                        (218, 'Arshyra'), (231, 'Dimension Q 10'),
                        (280, 'Alternativ ett'), (290, 'Alternativ tva'),
                        (318, 'Lagsta avgift'), (331, '(100 m3 x 17,40 kr/m3, exkl moms)')):
            page.insert_text((45, y), name, fontsize=10)
        for y, unit, ex, inc in ((155, 'dygn', '100', '125'), (168, 'dygn', '200', '250'),
                                 (231, 'ar', '300', '375'), (285, '', '900', '1125'),
                                 (331, 'ar', '1 740', '2 175')):
            if unit:
                page.insert_text((225, y), unit, fontsize=10)
            page.insert_text((340, y), ex, fontsize=10)
            page.insert_text((425, y), inc, fontsize=10)
        page.insert_text((225, 244), 'per abonnemang', fontsize=10)
        doc.save(source)
    text = uwc.DocumentConverter(str(tmp_path), ocr=False)._extract_pdf(str(source))
    assert '| Dygnshyra<br>Dimension Q 10 | dygn | 100 | 125 |' in text
    assert '| Arshyra<br>Dimension Q 10 | ar<br>per abonnemang | 300 | 375 |' in text
    assert '| Lagsta avgift<br>(100 m3 x 17,40 kr/m3, exkl moms) | ar | 1 740 | 2 175 |' in text
    assert '| 900 | 1125 |' in text
    assert 'utan entydig radkoppling' in text
    assert '| Alternativ ett |' not in text and '| Alternativ tva |' not in text


@pytest.mark.parametrize('columns', [3, 4])
def test_pdf_reads_uneven_columns_by_band(tmp_path, columns):
    source = tmp_path / 'spalter.pdf'
    starts = (30, 165, 345, 490)[:columns]
    with uwc.pymupdf.open() as doc:
        page = doc.new_page(width=660, height=800)
        for band, top in enumerate((130, 440), 1):
            page.insert_text((30, top - 38), f'BREDD {band} Gemensam rubrik for alla spalter i dokumentet', fontsize=23)
            for row in range(4):
                for col, x in enumerate(starts, 1):
                    page.insert_text((x, top + row * 21), f'B{band}C{col}R{row} lokal information', fontsize=9)
        doc.save(source)
    text = uwc.DocumentConverter(str(tmp_path), ocr=False)._extract_pdf(str(source))
    order = [f'BREDD {band}' if col == 0 else f'B{band}C{col}R{row}'
             for band in (1, 2) for col in range(columns + 1) for row in (range(4) if col else range(1))]
    assert [text.index(item) for item in order] == sorted(text.index(item) for item in order)


def test_borderless_table_with_three_columns_keeps_row_values_together(tmp_path):
    source = tmp_path / 'kantlos.pdf'
    with uwc.pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        for i in range(4):
            y = 150 + i * 27
            page.insert_text((45, y), f'Kommunal avgift {i}', fontsize=11)
            page.insert_text((250, y), f'12 50{i},00 kr', fontsize=11)
            page.insert_text((425, y), f'15 60{i},00 kr', fontsize=11)
        doc.save(source)
    text = uwc.DocumentConverter(str(tmp_path), ocr=False)._extract_pdf(str(source))
    assert all(f'Kommunal avgift {i} | 12 50{i},00 kr | 15 60{i},00 kr' in text for i in range(4))


def raster_text():
    with uwc.pymupdf.open() as src:
        p = src.new_page(width=480, height=230)
        p.insert_text((22, 75), 'Municipal parking fee 1250 kronor', fontsize=21)
        p.insert_text((22, 119), 'Applications are processed in April', fontsize=19)
        return p.get_pixmap(dpi=200)


def test_real_mixed_ocr_worker_preserves_native_text_and_page_reference(tmp_path):
    converter = uwc.DocumentConverter(str(tmp_path))
    if not converter.ocr_available():
        pytest.skip('Tesseracts språkdata saknas i testmiljön')
    source = tmp_path / 'kombinerad.pdf'
    with uwc.pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((50, 110), 'Digital underrubrik om kommunala avgifter', fontsize=15)
        page.insert_image(uwc.pymupdf.Rect(55, 190, 535, 420), pixmap=raster_text())
        page.insert_text((50, 510), 'Digital information efter bilden', fontsize=12)
        doc.save(source)

    async def go():
        runner = uwc.ConversionProcessRunner(str(tmp_path / 'jobs'))
        url = 'https://test.invalid/kombinerad.pdf'
        try:
            result = await runner.run(str(source), url, {}, {'ocr_languages': 'eng'},
                                      converter.md_filename(url, source.name), 10_000_000)
            assert result['ocr'] and result['text'], result['error']
            assert '**Textkälla:** OCR' in result['text']
            _, chunks = uwc.markdown_file_to_chunks(result['text'], is_document=True)
            text = '\n'.join(c['heading'] + '\n' + c['content'] for c in chunks)
            assert text.count('Digital information efter bilden') == 1
            assert text.index('Digital underrubrik') < text.index('1250') < text.index('Digital information')
            assert all(c['page_url'] == url + '#page=1' for c in chunks)
        finally:
            await runner.close()

    run(go())


def test_mixed_ocr_skips_small_images_and_shares_attempt_budget(tmp_path, monkeypatch):
    source = tmp_path / 'forsok.pdf'
    pix = raster_text()
    with uwc.pymupdf.open() as doc:
        for i in range(5):
            p = doc.new_page(width=600, height=800)
            if i != 2:
                p.insert_text((50, 120), f'Digital text pa sida {i + 1} ska alltid finnas kvar.', fontsize=12)
            rect = uwc.pymupdf.Rect(10, 10, 40, 25) if i == 0 else uwc.pymupdf.Rect(55, 190, 535, 420)
            p.insert_image(rect, pixmap=pix)
        doc.save(source)
    converter = uwc.DocumentConverter(str(tmp_path), ocr_max_pages=2)
    monkeypatch.setattr(converter, 'ocr_available', lambda: True)
    attempts = []

    def fail_mixed(page, *_):
        attempts.append((page.number, 'mixed'))
        raise RuntimeError('OCR-fel i bildområdet')

    monkeypatch.setattr(converter, '_pdf_mixed_ocr_lines', fail_mixed)
    monkeypatch.setattr(converter, '_ocr_page', lambda page, **kwargs: attempts.append((page.number, 'full')) or '')
    text = converter._extract_pdf(str(source))
    assert attempts == [(1, 'mixed'), (2, 'full')]
    assert 'Digital text pa sida 2' in text and 'Digital text pa sida 5' in text
    assert not converter.last_used_ocr
