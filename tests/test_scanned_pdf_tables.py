"""Regressioner för geometrisk tabelläsning på skannade PDF-sidor."""

from pathlib import Path

import pytest

from pdf_scan_tables import scan_table_items
from test_crawler import run, uwc


def words_for_rows(rows):
    words = []
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            x = 40 + 125 * column
            y = 100 + 18 * row_index
            for word in value.split():
                width = len(word) * 5
                words.append((x, y, x + width, y + 10, word))
                x += width + 3
    return words


def test_word_geometry_preserves_two_year_columns_and_amounts():
    rows = [
        ('År', 'Uppbörd', 'År', 'Uppbörd'),
        ('', 'Kronor', '', 'Kronor'),
        ('1898', '1 234,50', '1901', '− 80,25'),
        ('1899', '2 045', '1902', '—'),
        ('1900', '3 154', '1903', '4 001*'),
    ]
    # Läsordningen i textlagret får vara omkastad.
    [table] = scan_table_items(reversed(words_for_rows(rows)))
    assert table['text'].splitlines() == [
        '| År | Uppbörd (Kronor) | År | Uppbörd (Kronor) |',
        '|---|---|---|---|',
        '| 1898 | 1 234,50 | 1901 | − 80,25 |',
        '| 1899 | 2 045 | 1902 | — |',
        '| 1900 | 3 154 | 1903 | 4 001* |',
    ]


@pytest.mark.parametrize('bad_row', [
    ('1903', 'I 200', '500'),  # Teckenfel får inte rättas till ett tal.
    ('1902', '1200', '500'),   # Ett dubblerat år är inte en belagd serie.
    ('1903', '', '500'),       # En oläslig cell får inte flytta nästa belopp.
])
def test_scan_table_refuses_ambiguous_numeric_rows(bad_row):
    rows = [('År', 'Intäkt', 'Utgift'), ('1901', '1500', '600'),
            ('1902', '1600', '700'), bad_row, ('1904', '1700', '800')]
    assert not scan_table_items(words_for_rows(rows))


def test_amount_row_cannot_become_header_just_because_values_have_four_digits():
    rows = [('Inkomster', '1265', '1288'), ('Bidrag', '501', '402'),
            ('Avgifter', '301', '204'), ('Hyra', '70', '80')]
    assert not scan_table_items(words_for_rows(rows))


def make_scan(path, *, layer):
    with uwc.pymupdf.open() as original, uwc.pymupdf.open() as pdf:
        image = original.new_page(width=600, height=800)
        rows = [('År', 'Intäkt (kr)', 'Utgift (kr)'),
                ('2021', '12 500', '9 400'),
                ('2022', '14 600', '10 300'),
                ('2023', '15 750', '11 250'),
                ('2024', '16 800', '11 950')]
        text = [((50, 95), 'Kommunens avgifter per år'), ((50, 325), 'Fotnot om redovisningen.')]
        text.extend(((x, 160 + index * 28), value)
                    for index, row in enumerate(rows) for x, value in zip((50, 220, 400), row))
        for pos, value in text:
            image.insert_text(pos, value, fontsize=14)
        page = pdf.new_page(width=600, height=800)
        page.insert_image(page.rect, pixmap=image.get_pixmap(dpi=200))
        if layer:
            # Textlagret lagras kolumnvis, även om bilden visar sammanhängande rader.
            for pos, value in sorted(text, key=lambda item: (item[0][0], item[0][1])):
                page.insert_text(pos, value, fontsize=14, render_mode=3 if layer == 'hidden' else 0)
        pdf.save(path)


@pytest.mark.parametrize('layer', ['hidden', 'painted'])
def test_scan_uses_hidden_layer_but_preserves_visible_digital_text(tmp_path, layer):
    source = tmp_path / 'avgifter.pdf'
    make_scan(source, layer=layer)
    converter = uwc.DocumentConverter(str(tmp_path), ocr=False)
    result = converter.convert(str(source), 'https://test.invalid/avgifter.pdf')
    assert result, converter.last_error
    markdown = Path(result).read_text(encoding='utf-8')
    assert 'Fotnot om redovisningen.' in markdown
    assert not converter.last_used_ocr
    assert ('| 2023 | 15 750 | 11 250 |' in markdown) is (layer == 'hidden')
    row_text = 'Tabellrad. År: 2023. Intäkt (kr): 15 750. Utgift (kr): 11 250.'
    assert (row_text in markdown) is (layer == 'hidden')
    assert markdown.count('15 750') == (2 if layer == 'hidden' else 1)


def test_scan_worker_exports_recognized_rows_with_page_links(tmp_path):
    converter = uwc.DocumentConverter(str(tmp_path))
    if not converter.ocr_available():
        pytest.skip('Tesseracts språkdata saknas i testmiljön')
    source = tmp_path / 'avgifter.pdf'
    make_scan(source, layer=None)
    url = 'https://test.invalid/avgifter.pdf'

    async def go():
        runner = uwc.ConversionProcessRunner(str(tmp_path / 'jobs'))
        try:
            result = await runner.run(str(source), url, {}, {'ocr_languages': 'eng', 'ocr_max_pages': 1},
                                      converter.md_filename(url, source.name), 5_000_000)
            assert result['text'], result['error']
            assert result['ocr'] and '**Textkälla:** OCR' in result['text']
            _, chunks = uwc.markdown_file_to_chunks(result['text'], is_document=True)
            content = '\n'.join(chunk['content'] for chunk in chunks)
            assert '| 2021 | 12 500 | 9 400 |' in content
            assert '| 2024 | 16 800 | 11 950 |' in content
            assert 'Fotnot om redovisningen.' in content
            assert all(chunk['page_number'] == 1 and chunk['page_url'] == url + '#page=1' for chunk in chunks)
        finally:
            await runner.close()

    run(go())
