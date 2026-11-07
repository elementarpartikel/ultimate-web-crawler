"""Regressioner för radvis rättelse av äldre OCR i skannade PDF:er."""
from pathlib import Path

import pytest

from test_crawler import run, uwc


def scanned_pdf(path, *, old, raster_scale=1, visible=False, count=1):
    mupdf = uwc.pymupdf
    with mupdf.open() as image, mupdf.open() as doc:
        reference = image.new_page(width=600, height=800)
        reference.insert_text((48, 120), 'Kommunalt beslut om avgiften 1250 kronor', fontsize=18)
        raster = reference.get_pixmap(dpi=150)
        for _ in range(count):
            page = doc.new_page(width=600, height=800)
            page.insert_image(mupdf.Rect(0, 0, 600 * raster_scale, 800 * raster_scale), pixmap=raster)
            page.insert_text((48, 120), old, fontsize=18, render_mode=0 if visible else 3)
            page.insert_text((48, 230), 'Digital arkivmarkering', fontsize=12)
        doc.save(path)


@pytest.mark.parametrize('mode', ['success', 'changed_number', 'changed_sign', 'changed_percent',
                                  'failed', 'unavailable', 'off', 'small_image', 'painted',
                                  'good_layer', 'budget'])
def test_legacy_ocr_repairs_only_supported_rows(tmp_path, monkeypatch, mode):
    source = tmp_path / 'skannat-beslut.pdf'
    old = ('Kommunalt beslut om avgiften 1250 kronor' if mode == 'good_layer'
           else 'Kommunalt besl~t om avgiften 1250 kronor')
    if mode in ('changed_sign', 'changed_percent'):
        old = old.replace('1250', '-1250' if mode == 'changed_sign' else '1250%')
    scanned_pdf(source, old=old, raster_scale=0.5 if mode == 'small_image' else 1,
                visible=mode == 'painted', count=2 if mode == 'budget' else 1)
    converter = uwc.DocumentConverter(str(tmp_path), ocr=mode != 'off', ocr_max_pages=1)
    monkeypatch.setattr(converter, 'ocr_available', lambda: mode != 'unavailable')
    monkeypatch.setattr(converter, '_pdf_image_regions', lambda *args: [])
    attempts = []
    candidate = object()

    def ocr(page, *, full, dpi=200):
        attempts.append((page.number, full, dpi))
        return None if mode in ('failed', 'budget') else candidate

    get_text = uwc.pymupdf.Page.get_text

    def text(page, option='text', **kwargs):
        if kwargs.get('textpage') is not candidate:
            return get_text(page, option, **kwargs)
        if option == 'text':
            return 'Kommunalt beslut om avgiften'
        words = get_text(page, 'words')
        return [(*word[:4], {'besl~t': 'beslut', '-1250': '1250', '1250%': '1250',
                             '1250': '1258' if mode == 'changed_number' else '1250'}.get(word[4], word[4]),
                 *word[5:]) for word in words]

    monkeypatch.setattr(converter, '_ocr_textpage', ocr)
    monkeypatch.setattr(uwc.pymupdf.Page, 'get_text', text)
    output = converter._extract_pdf(str(source))
    expected_attempt = mode in ('success', 'changed_number', 'changed_sign', 'changed_percent', 'failed', 'budget')
    assert attempts == ([(0, True, 300)] if expected_attempt else [])
    assert 'Digital arkivmarkering' in output
    assert '1258' not in output
    if mode == 'success':
        assert 'Kommunalt beslut om avgiften 1250 kronor' in output
        assert old not in output
    else:
        assert old in output
    assert converter.last_used_ocr is (mode == 'success')
    if mode == 'budget':
        assert output.count(old) == 2


def test_legacy_ocr_worker_repairs_visible_scan_and_preserves_page_citation(tmp_path):
    converter = uwc.DocumentConverter(str(tmp_path))
    if not converter.ocr_available():
        pytest.skip('Tesseracts språkdata saknas i testmiljön')
    source = tmp_path / 'skannat-beslut.pdf'
    scanned_pdf(source, old='Kommunalt besl~t om avgiften 1250 kronor')
    url = 'https://test.invalid/beslut.pdf'

    async def go():
        runner = uwc.ConversionProcessRunner(str(tmp_path / 'jobs'))
        try:
            result = await runner.run(str(source), url, {}, {'ocr_languages': 'eng'},
                                      converter.md_filename(url, source.name), 5_000_000)
            assert result['text'], result['error']
            assert result['ocr'] and '**Textkälla:** OCR' in result['text']
            _, chunks = uwc.markdown_file_to_chunks(result['text'], is_document=True)
            content = '\n'.join(chunk['content'] for chunk in chunks)
            assert 'Kommunalt beslut om avgiften 1250 kronor' in content
            assert 'Digital arkivmarkering' in content
            assert all(chunk['page_url'] == url + '#page=1' for chunk in chunks)
        finally:
            await runner.close()

    run(go())
