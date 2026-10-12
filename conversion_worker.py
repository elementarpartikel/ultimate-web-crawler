"""Intern worker: ett enda dokument, eget minne och eget temporärt filutrymme."""
import json
import os
import sys
import time


def main(job_path=None, result_path=None, converter_module=None):
    # Inga parsers eller barnprocesser får starta innan föräldern skyddat processträdet.
    if job_path is None:
        job_path, result_path = sys.argv[1:3]
    with open(job_path, encoding='utf-8') as request_file:
        request = json.load(request_file)
    if request.get('go_file'):
        deadline = time.monotonic() + 10
        while not os.path.isfile(request['go_file']):
            if time.monotonic() >= deadline:
                return 2
            time.sleep(0.02)
    elif sys.stdin is None or sys.stdin.buffer.read(1) != b'G':
        return 2
    module = converter_module
    if module is None:
        import document_conversion as module
    options = request['options']
    converter = module.DocumentConverter(
        request['output'], pii_cleaner=lambda text: module.clean_pii_text(options, text),
        ocr=options.get('ocr', True), ocr_languages=options.get('ocr_languages', 'swe+eng'),
        ocr_max_pages=options.get('ocr_max_pages', 50), work_dir=os.path.dirname(job_path))
    if request.get('expected_hash') and module.file_sha256(request['input']) != request['expected_hash']:
        result = {'path': None, 'ocr': False, 'error': 'Originalversionen ändrades före konverteringen'}
    elif not converter.can_convert(request['input']):
        result = {'path': None, 'ocr': False, 'error': 'Dokumentkonverteringen kräver ett saknat bibliotek eller LibreOffice'}
    else:
        result = module.convert_document_result(converter, request['input'], request['url'], request['ref'])
    module.atomic_write_text(result_path, json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
