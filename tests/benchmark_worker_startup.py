"""Manuell lokal mätning av import/starttid, utan nätverksanrop."""
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
before = root / 'backup' / 'sixth-round-2026-10-12' / 'ultimate-web-crawler.py'
temporary_baseline = None
if len(sys.argv) > 1:
    before = Path(sys.argv[1]).resolve()
elif not before.is_file():
    # En vanlig Git-klon saknar lokal backup; använd versionsmärkningen före ändringen.
    baseline = subprocess.run(['git', 'show', 'v7.1.0:ultimate-web-crawler.py'], cwd=root,
                              capture_output=True, check=True).stdout
    temporary_baseline = tempfile.TemporaryDirectory(prefix='crawler-benchmark-')
    before = Path(temporary_baseline.name) / 'baseline.py'
    before.write_bytes(baseline)
programs = {
    'före': '''import importlib.util, sys
spec = importlib.util.spec_from_file_location('baseline', sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
''',
    'efter_pdf_worker': 'import document_conversion as d; d._load_parser(".pdf")',
}
result = {}
for name, code in programs.items():
    samples = []
    for _ in range(5):
        started = time.perf_counter()
        subprocess.run([sys.executable, '-c', code, str(before)], cwd=root,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=20)
        samples.append(time.perf_counter() - started)
    result[name] = {'median_seconds': round(statistics.median(samples), 3),
                    'samples_seconds': [round(value, 3) for value in samples]}
print(json.dumps(result, ensure_ascii=False, indent=2))
if temporary_baseline:
    temporary_baseline.cleanup()
