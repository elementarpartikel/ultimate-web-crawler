"""Regressioner för verifierade avskrifter i skannade kommunala PDF:er."""

from hashlib import sha256
from pathlib import Path

import pytest

import pdf_source_corrections as corrections
from test_crawler import uwc


def test_stockholm_population_matches_printed_totals():
    parishes = corrections.STOCKHOLM_POPULATION_PARISHES
    districts = [row for _, _, rows in parishes for row in rows]
    assert len(parishes) == 15
    assert sorted(row[1] for row in districts) == list(range(1, 36))
    assert all(male + female == total for _, _, male, female, total in districts)
    assert all(sum(row[4] for row in rows) == subtotal for _, subtotal, rows in parishes)
    assert sum(subtotal for _, subtotal, _ in parishes) == 398639
    assert tuple(sum(row[column] for row in districts) for column in (2, 3, 4)) == (179046, 219593, 398639)


def test_brannkyrka_welfare_matches_printed_year_totals():
    assert corrections.BRANNKYRKA_WELFARE_YEARS == (1901, 1902, 1903, 1904, 1905)
    rows = corrections.BRANNKYRKA_WELFARE_ROWS
    assert len(rows) == 7
    assert all(len(amounts) == 5 for _, amounts in rows)
    for amounts in zip(*(amounts for _, amounts in rows)):
        cents = [100 * kronor + (ore or 0) for kronor, ore in amounts]
        assert sum(cents[:-1]) == cents[-1]
    assert rows[-1] == ('Summa kronor', ((25634, 13), (32529, 74), (32572, 19), (32890, 99), (34826, 30)))


@pytest.mark.parametrize(('matching_sha', 'ocr'), [(True, True), (True, False), (False, True)])
def test_brannkyrka_welfare_separates_kronor_and_ore_in_document_chunks(tmp_path, monkeypatch, matching_sha, ocr):
    source = tmp_path / 'bilaga.pdf'
    with uwc.pymupdf.open() as image, uwc.pymupdf.open() as doc:
        image_page = image.new_page(width=564, height=682)
        image_page.insert_text((140, 227), 'Tryckt tabell')
        doc.new_page(width=564, height=682).insert_text((140, 200), 'Inledning på första sidan.')
        page = doc.new_page(width=564, height=682)
        page.insert_image(page.rect, pixmap=image_page.get_pixmap())
        doc.new_page(width=564, height=682).insert_text((140, 200), 'Fortsättning på tredje sidan.')
        doc.save(source)

    digest = sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(corrections, 'BRANNKYRKA_SIZE', source.stat().st_size)
    monkeypatch.setattr(corrections, 'BRANNKYRKA_SHA256', digest if matching_sha else '0' * 64)
    converter = uwc.DocumentConverter(str(tmp_path), ocr=ocr, ocr_max_pages=1)
    calls = []

    textpage = object()
    recognized = [
        {'bbox': (126, 155, 420, 168), 'spans': [{'text': 'Ingress om fattigvården.'}]},
        {'bbox': (140, 224, 500, 238), 'spans': [{'text': 'Hus och inventarier 478197 874148'}]},
        {'bbox': (126, 340, 400, 353), 'spans': [{'text': 'Avsnitt om epidemisjukstugan.'}]},
    ]

    def read_ocr(page, *, full):
        assert full
        calls.append(page.number)
        return textpage

    get_text = uwc.pymupdf.Page.get_text

    def read_text(page, option='text', **kwargs):
        if kwargs.get('textpage') is not textpage:
            return get_text(page, option, **kwargs)
        if option == 'dict':
            return {'blocks': [{'lines': recognized}]}
        if option == 'words':
            return []
        return '\n'.join(line['spans'][0]['text'] for line in recognized)

    monkeypatch.setattr(converter, 'ocr_available', lambda: ocr)
    monkeypatch.setattr(converter, '_ocr_textpage', read_ocr)
    monkeypatch.setattr(uwc.pymupdf.Page, 'get_text', read_text)
    url = 'https://test.invalid/bilaga.pdf'
    result = converter.convert(str(source), url)
    assert result, converter.last_error
    assert calls == ([1] if ocr else [])
    assert converter.last_used_ocr is ocr
    markdown = Path(result).read_text(encoding='utf-8')
    assert 'Inledning på första sidan.' in markdown
    assert 'Fortsättning på tredje sidan.' in markdown
    _, chunks = uwc.markdown_file_to_chunks(markdown, is_document=True)
    page2 = '\n'.join(chunk['content'] for chunk in chunks if chunk['page_number'] == 2)
    if ocr:
        assert 'Ingress om fattigvården.' in page2
        assert 'Avsnitt om epidemisjukstugan.' in page2
    if not matching_sha:
        assert '478197 874148' in page2
        assert '1901 (öre)' not in markdown
        return

    assert '478197' not in page2 and '874148' not in page2
    header = ('| Utgiftspost | 1901 (kr) | 1901 (öre) | 1902 (kr) | 1902 (öre) |'
              ' 1903 (kr) | 1903 (öre) | 1904 (kr) | 1904 (öre) | 1905 (kr) | 1905 (öre) |')
    tables = [chunk for chunk in chunks if header in chunk['content']]
    assert tables and all(chunk['page_number'] == 2 and chunk['page_url'] == url + '#page=2' for chunk in tables)
    rows = [line for chunk in tables for line in chunk['content'].splitlines()
            if line.startswith('| ') and line != header]
    assert rows == [
        '| Hus och inventarier | 1 079 | 70 | 1 683 | 21 | 1 975 | 96 | 478 | 97 | 374 | 48 |',
        '| Understödstagares beklädnad, vård och underhåll | 19 299 | 54 | 21 868 | 72 |'
        ' 23 381 | 33 | 23 613 | 94 | 27 081 | 75 |',
        '| Förbrukningspersedlar | 294 | 98 | 1 746 | 44 | 547 | — | 1 318 | 88 | 653 | 65 |',
        '| Aflöningar | 460 | — | 520 | — | 620 | — | 666 | — | 690 | — |',
        '| Ersättning till andra kommuner | 3 089 | 72 | 4 176 | 85 | 3 330 | 40 | 3 621 | 97 | 2 562 | 15 |',
        '| Öfriga behof | 1 410 | 19 | 2 534 | 52 | 2 717 | 50 | 3 191 | 23 | 3 464 | 27 |',
        '| Summa kronor | 25 634 | 13 | 32 529 | 74 | 32 572 | 19 | 32 890 | 99 | 34 826 | 30 |',
    ]


@pytest.mark.parametrize(('original_bytes', 'mixed_ocr'), [(True, False), (False, False), (True, True)])
def test_verified_tables_only_replace_target_regions_in_matching_pdf(tmp_path, monkeypatch, original_bytes,
                                                                     mixed_ocr):
    source = tmp_path / 'kommun.pdf'
    with uwc.pymupdf.open() as doc:
        for number in range(1, 4):
            page = doc.new_page(width=396.72, height=574.56)
            y = 220 if number == 3 else 61
            page.insert_text((65, y), f'Inledande uppgift om mantalsskrivningen på sida {number}')
            if number != 3:
                page.insert_text((75, 300), 'Ofullständig OCR-rad för tabellen 398,689')
                page.insert_text((75, 405), 'Folkmangdens storlek vid nedannlmnda års slut')
                page.insert_text((220, 471), '1901 318,398')
                page.insert_text((75, 498), 'Folkmängden vid 1916 års slut, se Supplementet.')
        doc.save(source)
    # Båda varianterna kan öppnas och har samma filnamn och storlek.
    pdf = source.read_bytes()
    original = pdf + b'%1\n'
    source.write_bytes(original if original_bytes else pdf + b'%2\n')
    monkeypatch.setattr(corrections, 'STOCKHOLM_POPULATION_SIZE', len(original))
    monkeypatch.setattr(corrections, 'STOCKHOLM_POPULATION_SHA256', sha256(original).hexdigest())

    converter = uwc.DocumentConverter(str(tmp_path), ocr=mixed_ocr)
    if mixed_ocr:
        def ocr_lines(page, regions, native):
            return [
                {'bbox': (180, 473, 250, 483), 'spans': [{'text': 'Felläsning från ny OCR'}]},
                {'bbox': (180, 503, 250, 513), 'spans': [{'text': 'Bildtext från ny OCR'}]},
            ]

        monkeypatch.setattr(converter, 'ocr_available', lambda: True)
        monkeypatch.setattr(converter, '_pdf_image_regions', lambda page, *_: [page.rect] if page.number == 1 else [])
        monkeypatch.setattr(converter, '_pdf_mixed_ocr_lines', ocr_lines)
    url = 'https://test.invalid/annan-sokvag.pdf'
    result = converter.convert(str(source), url)
    assert result, converter.last_error
    markdown = Path(result).read_text(encoding='utf-8')
    assert markdown.count('Ofullständig OCR-rad') == (1 if original_bytes else 2)
    assert markdown.count('Folkmangdens storlek vid nedannlmnda års slut') == (1 if original_bytes else 2)
    assert markdown.count('1901 318,398') == (1 if original_bytes else 2)
    assert markdown.count('Inledande uppgift om mantalsskrivningen') == 3
    assert markdown.count('Folkmängden vid 1916 års slut, se Supplementet.') == 2
    assert converter.last_used_ocr is mixed_ocr
    assert 'Felläsning från ny OCR' not in markdown
    assert ('Bildtext från ny OCR' in markdown) is mixed_ocr
    _, chunks = uwc.markdown_file_to_chunks(markdown, is_document=True)
    page2 = '\n'.join(chunk['content'] for chunk in chunks if chunk['page_number'] == 2)
    if not original_bytes:
        assert 'verifierad avskrift' not in markdown
        assert '398,689' in page2
        assert '1901 318,398' in page2
        return

    assert '398,689' not in page2
    assert any('verifierad avskrift' in chunk['heading'] for chunk in chunks if chunk['page_number'] == 2)
    assert all(chunk['page_url'] == url + '#page=2' for chunk in chunks if chunk['page_number'] == 2)
    assert all('verifierad avskrift' not in chunk['content'] for chunk in chunks if chunk['page_number'] != 2)
    assert '| Kungsholms | Kronobergsroten | 17 | 6 861 | 7 471 | 14 332 |' in page2
    assert '| Hedvig Eleonora | Artilleriroten | 11 | 3 555 | 6 088 | 9 643 |' in page2
    assert '| Engelbrekts | Humlegårdsroten | 9 | 3 310 | 6 779 | 10 089 |' in page2
    assert '| Maria | Heleneborgsroten | 31 | 8 677 | 9 203 | 17 880 |' in page2
    assert '| Bromma | Brommaroten | 35 | 3 018 | 3 194 | 6 212 |' in page2
    assert '| Katarina | 58 125 |' in page2
    assert '| Summa för Stockholm | – | – | 179 046 | 219 593 | 398 639 |' in page2
    rows = [line.split('|')[1:-1] for line in page2.splitlines() if line.startswith('|')]
    assert sorted(int(row[2]) for row in rows if len(row) == 6 and row[2].strip().isdigit()) == list(range(1, 36))
    district_chunks = [chunk for chunk in chunks if '| Rotenummer |' in chunk['content']]
    assert len(district_chunks) > 1
    assert all('Antal personer i roten vid 1915 års slut |' in chunk['content'] for chunk in district_chunks)

    historical_header = '| År | Antal personer i Stockholm vid årets slut |'
    historical_chunks = [chunk for chunk in chunks if historical_header in chunk['content']]
    assert historical_chunks
    assert all(chunk['page_number'] == 2 and chunk['page_url'] == url + '#page=2' for chunk in historical_chunks)
    historical_rows = [line.split('|')[1:-1] for chunk in historical_chunks
                       for line in chunk['content'].splitlines() if line.startswith('| ')]
    assert [' '.join(cell.strip() for cell in row) for row in historical_rows if row[0].strip().isdigit()] == [
        '1663 cirka 40 000', '1763 72 989', '1800 75 517', '1820 75 569',
        '1830 81 512', '1840 83 629', '1850 91 544', '1860 109 878',
        '1865 126 180', '1870 133 597', '1875 144 974', '1880 168 019',
        '1885 211 139', '1890 245 317', '1895 267 100', '1898 291 580',
        '1899 297 148', '1900 301 050', '1901 301 695', '1902 305 115',
        '1903 309 496', '1904 318 398', '1905 324 488', '1906 332 738',
        '1907 337 460', '1908 339 582', '1909 341 816', '1910 343 832',
        '1911 346 599', '1912 376 171', '1913 382 085', '1914 386 270',
    ]


@pytest.mark.parametrize('matching_sha', [True, False])
def test_economic_comparison_preserves_printed_rows_units_and_page(tmp_path, monkeypatch, matching_sha):
    source = tmp_path / 'kommun.pdf'
    with uwc.pymupdf.open() as doc:
        for number in range(1, 5):
            page = doc.new_page(width=405.12, height=579.84)
            page.insert_text((45, 86), 'Folkmängd nyåret 1916: 398,689')
            page.insert_text((45, 111), 'Bevillningstaxerad inkomst: 371,725,680')
            page.insert_text((45, 169), 'Inkomst- och förmögenhetsskatt: 11,993,243')
            page.insert_text((45, 211), 'Anmärkning om egendomar utanför staden')
            if number == 3:
                page.insert_text((45, 541), 'Annons nedanför tabellerna')
        doc.save(source)
    digest = sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(corrections, 'STOCKHOLM_POPULATION_SIZE', source.stat().st_size)
    monkeypatch.setattr(corrections, 'STOCKHOLM_POPULATION_SHA256', digest if matching_sha else '0' * 64)

    converter = uwc.DocumentConverter(str(tmp_path), ocr=False)
    url = 'https://test.invalid/kommun.pdf'
    result = converter.convert(str(source), url)
    assert result, converter.last_error
    _, chunks = uwc.markdown_file_to_chunks(Path(result).read_text(encoding='utf-8'), is_document=True)
    header = ('| Jämförelsepunkt | Enhet | Stockholm | Stockholms andel av riket (%) '
              '| Övriga städer | Landsbygd | Hela riket |')
    tables = [chunk for chunk in chunks if header in chunk['content']]
    page3 = '\n'.join(chunk['content'] for chunk in chunks if chunk['page_number'] == 3)
    assert 'Anmärkning om egendomar utanför staden' in page3
    assert 'Annons nedanför tabellerna' in page3
    errors = ('398,689', '371,725,680', '11,993,243')
    if not matching_sha:
        assert not tables
        assert all(error in page3 for error in errors)
        return

    assert tables and all(chunk['page_number'] == 3 and chunk['page_url'] == url + '#page=3' for chunk in tables)
    assert not any(error in page3 for error in errors)
    assert 'Procenttalen anger Stockholms andel av hela riket' in page3
    rows = [[cell.strip() for cell in line.split('|')[1:-1]]
            for chunk in tables for line in chunk['content'].splitlines()
            if line.startswith('| ') and line != header]
    assert [row[0] for row in rows] == [
        'Folkmängd nyåret 1916', 'Bevillningstaxerade fastigheter', 'Bevillningsfria fastigheter',
        'Bevillningstaxerad inkomst', 'Bevillningstaxerad inkomst av kapital',
        'Bevillningstaxerad inkomst av tjänst eller pension',
        'Bevillningstaxerad inkomst av rörelse, yrke eller eljest',
        'Beskattningsbar inkomst', 'Fastighetsbevillning', 'Inkomstbevillning',
        'Inkomst- och förmögenhetsskatt', 'Taxerad inkomst och förmögenhet',
        'Bevillning för särskilda förmåner', 'Krigskonjunkturskatt',
    ]
    assert [row[1] for row in rows] == ['personer'] + ['kr'] * 13
    assert [' '.join(cell.replace(' ', '') for cell in row[2:]) for row in rows] == [
        '398639 7,0 1187233 4126868 5712740',
        '1562059500 18,1 2187988910 4837473100 8587521510',
        '392651500 28,5 389366360 595332950 1377350810',
        '374725680 21,0 685244461 723216661 1783186802',
        '32366526 31,9 29347576 39645780 101359882',
        '206266115 18,8 414809500 473901989 1094977604',
        '136093039 23,2 241087385 209668892 586849316',
        '327394880 24,0 565519162 469937786 1362851828',
        '78125 16,9 109993 274703 462821',
        '327652 24,0 565262 470689 1363603',
        '14019505 32,7 16923490 11992243 42935238',
        '480453700 24,9 716612720 734891604 1931958024',
        '100854 31,8 209755 6833 317442',
        '1991288 30,7 3503279 984757 6479324',
    ]


@pytest.mark.parametrize('matching_sha', [True, False])
def test_customs_revenue_keeps_year_city_amount_and_1914_note(tmp_path, monkeypatch, matching_sha):
    source = tmp_path / 'kommun.pdf'
    broken = 'Tulluppbörden: 40,327,807 59,941,176'
    with uwc.pymupdf.open() as doc:
        for number in range(1, 5):
            page = doc.new_page(width=405.12, height=579.84)
            page.insert_text((45, 220), f'Anmärkning före tulltabellen på sida {number}')
            if number != 2:
                page.insert_text((45, 280), broken)
            if number == 3:
                page.insert_text((45, 541), 'Annons nedanför tulltabellen')
        doc.save(source)
    digest = sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(corrections, 'STOCKHOLM_POPULATION_SIZE', source.stat().st_size)
    monkeypatch.setattr(corrections, 'STOCKHOLM_POPULATION_SHA256', digest if matching_sha else '0' * 64)

    converter = uwc.DocumentConverter(str(tmp_path), ocr=False)
    url = 'https://test.invalid/kommun.pdf'
    result = converter.convert(str(source), url)
    assert result, converter.last_error
    markdown = Path(result).read_text(encoding='utf-8')
    assert markdown.count(broken) == (2 if matching_sha else 3)
    _, chunks = uwc.markdown_file_to_chunks(markdown, is_document=True)
    page3 = '\n'.join(chunk['content'] for chunk in chunks if chunk['page_number'] == 3)
    assert 'Anmärkning före tulltabellen på sida 3' in page3
    assert 'Annons nedanför tulltabellen' in page3

    header = '| År | Stockholm (kr) | Göteborg (kr) | Malmö (kr) | Norrköping (kr) | Hela riket (kr) |'
    tables = [chunk for chunk in chunks if header in chunk['content']]
    if not matching_sha:
        assert not tables
        assert broken in page3
        return

    assert len(tables) > 1
    assert all(chunk['page_number'] == 3 and chunk['page_url'] == url + '#page=3' for chunk in tables)
    rows = [line for chunk in tables for line in chunk['content'].splitlines()
            if line.startswith('| ') and line != header]
    assert [int(row.split('|')[1]) for row in rows] == [1855, 1865, 1875, *range(1880, 1916)]
    assert [tuple(int(cell.replace(' ', '').rstrip('*')) for cell in row.split('|')[1:-1])
            for row in rows] == list(corrections.STOCKHOLM_CUSTOMS)
    assert rows[0] == '| 1855 | 3 876 457 | 4 432 735 | 571 082 | 586 022 | 12 004 543 |'
    assert '| 1884 | 11 929 401 | 8 551 250 | 2 770 639 | 1 274 331 | 33 837 744 |' in rows
    assert '| 1905 | 20 327 807 | 11 215 700 | 7 139 356 | 2 000 315 | 60 368 150 |' in rows
    assert '| 1912 | 22 915 070 | 12 177 801 | 6 824 339 | 2 995 596 | 65 969 496 |' in rows
    assert [row for row in rows if '*' in row] == [
        '| 1914 | 21 184 254 | 11 576 667 | 6 697 523 | 2 351 705 | 59 941 376* |',
    ]
    assert rows[-1] == '| 1915 | 20 387 676 | 13 298 149 | 5 831 054 | 1 441 367 | 56 157 241 |'
    assert 'Lastpenningar ingick i tullmedlen till och med 1913.' in page3
    assert 'Från 1914 redovisades de separat och uppgick det året till 761 392 kr.' in page3
