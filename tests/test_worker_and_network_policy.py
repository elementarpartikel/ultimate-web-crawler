"""Regressioner för workerns startmiljö och crawlerns nätverksbeslut."""
import asyncio
import json
import logging
from pathlib import Path
from queue import Queue
import socket

import pytest
from aiohttp import web

from crawler_runtime import ConversionProcessRunner
from test_crawler import Site, config, page, published, run, run_crawl, serve, uwc


def test_document_worker_and_child_receive_only_selected_environment(tmp_path, monkeypatch):
    forbidden = ('OPENAI_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'HTTPS_PROXY', 'PYTHONPATH', 'SSLKEYLOGFILE')
    for name in forbidden:
        monkeypatch.setenv(name, 'syntetiskt-varde')
    monkeypatch.setenv('TESSDATA_PREFIX', str(tmp_path / 'tessdata'))
    probe = tmp_path / 'probe.py'
    probe.write_text('''import json, os, pathlib, subprocess, sys
sys.stdin.buffer.read(1)
job = pathlib.Path(sys.argv[1]).parent
child = json.loads(subprocess.check_output([sys.executable, '-c',
    'import json,os; print(json.dumps(list(os.environ)))']))
result = {'leaked': sorted(set(%r).intersection(set(os.environ) | set(child))),
    'cwd': os.getcwd(), 'home': os.environ.get('HOME'), 'temp': os.environ.get('TMPDIR'),
    'tessdata': os.environ.get('TESSDATA_PREFIX'), 'job': str(job), 'has_path': 'PATH' in os.environ}
pathlib.Path(sys.argv[2]).write_text(json.dumps({'error': json.dumps(result)}), encoding='utf-8')
''' % (forbidden,), encoding='utf-8')

    async def go():
        source = tmp_path / 'original.pdf'
        source.write_bytes(b'%PDF-test')
        runner = ConversionProcessRunner(str(tmp_path / 'jobs'), worker_script=str(probe))
        try:
            result = await runner.run(str(source), 'https://test.invalid/a.pdf', {}, {}, 'a_doc.md', 10000)
            assert result['error'].startswith('{'), result['error']
            observed = json.loads(result['error'])
            assert observed['leaked'] == []
            assert observed['cwd'] == observed['home'] == observed['temp'] == observed['job']
            assert observed['tessdata'] == str(tmp_path / 'tessdata') and observed['has_path']
        finally:
            await runner.close()
    run(go())


def test_production_worker_blocks_python_socket_and_dns_calls(tmp_path):
    probe = tmp_path / 'probe_network.py'
    root = str(Path(uwc.__file__).parent)
    probe.write_text('''import json, pathlib, socket, sys, types
sys.path.insert(0, %r)
from conversion_worker import main

def attempt_network(converter, filepath, url, ref):
    blocked = []
    for name, call in [('socket', socket.socket), ('dns', lambda: socket.getaddrinfo('localhost', 80))]:
        try:
            call()
        except PermissionError:
            blocked.append(name)
    return {'error': json.dumps(blocked)}

module = types.SimpleNamespace(
    DocumentConverter=lambda *args, **kwargs: types.SimpleNamespace(can_convert=lambda path: True),
    convert_document_result=attempt_network,
    atomic_write_text=lambda path, text: pathlib.Path(path).write_text(text, encoding='utf-8'))
sys.exit(main(converter_module=module))
''' % root, encoding='utf-8')

    async def go():
        source = tmp_path / 'original.pdf'
        source.write_bytes(b'%PDF-test')
        runner = ConversionProcessRunner(str(tmp_path / 'jobs'), worker_script=str(probe))
        try:
            result = await runner.run(str(source), 'https://test.invalid/a.pdf', {}, {}, 'a_doc.md', 10000)
            assert json.loads(result['error']) == ['socket', 'dns']
        finally:
            await runner.close()
    run(go())


@pytest.mark.parametrize('enabled', [False, True])
def test_legacy_conversion_is_an_explicit_opt_in(tmp_path, monkeypatch, enabled):
    original = b'{\\rtf1\\ansi Ett testdokument med avgifter.}'

    class LegacySite(Site):
        async def handle(self, request):
            if request.path == '/taxa.rtf':
                return web.Response(body=original, content_type='application/rtf')
            return await super().handle(request)

    async def go():
        calls = []

        async def convert(self, path, url, ref, options, *args, **kwargs):
            calls.append(options)
            assert enabled and options['allow_libreoffice'] is True
            return {'text': None, 'ocr': False, 'error': 'Simulerad konvertering'}

        monkeypatch.setattr(ConversionProcessRunner, 'run', convert)
        site = LegacySite()
        site.pages['/'] = (page('Start', 'Kommunal sida med en taxa', [('/taxa.rtf', 'Taxa')]), None)
        runner, base = await serve(site)
        try:
            options = {'allow_libreoffice': True} if enabled else {}
            crawler = await run_crawl(base, tmp_path, find_sitemap=False, **options)
            assert bool(calls) == enabled
            assert bool(crawler.report['legacy_conversion_skipped']) != enabled
            assert crawler.delivery_status() == ('partial' if enabled else 'success')
            generation = published(tmp_path)
            assert next((generation / 'dokument').glob('*.rtf')).read_bytes() == original
        finally:
            await runner.cleanup()
    run(go())


def test_verified_cached_legacy_text_is_reused_without_starting_libreoffice(tmp_path, monkeypatch):
    async def go():
        calls = []

        async def convert(self, path, url, ref, options, *args, **kwargs):
            calls.append(options)
            assert options['allow_libreoffice'] is True
            text = f'# Taxa\n\n**Dokument-URL:** {url}\n\n---\n\nGILTIG DOKUMENTTEXT\n\n---\n'
            return {'text': text, 'ocr': False, 'error': ''}

        monkeypatch.setattr(ConversionProcessRunner, 'run', convert)
        site = Site()
        site.pages['/'] = (page('Start', 'Kommunal sida med en taxa', [('/taxa.rtf', 'Taxa')]), 's1')
        site.files['/taxa.rtf'] = (b'{\\rtf1\\ansi Regler och avgifter.}', 'd1')
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False, allow_libreoffice=True)
            result = await run_crawl(base, tmp_path, find_sitemap=False)
            assert len(calls) == 1 and not result.report['conversion_failures']
            assert 'GILTIG DOKUMENTTEXT' in (published(tmp_path) / 'chunks.jsonl').read_text(encoding='utf-8')
        finally:
            await runner.cleanup()
    run(go())


@pytest.mark.parametrize(('configured', 'reason', 'permitted'), [
    (None, 'intranet_literal', True), (True, 'explicit_allow', True), (False, 'explicit_deny', False),
])
def test_private_policy_is_reported_and_false_blocks_even_loopback(tmp_path, caplog, configured, reason, permitted):
    async def go():
        site = Site()
        site.pages['/'] = (page('Start', 'Privat lokal testserver'), None)
        runner, base = await serve(site)
        try:
            options = {} if configured is None else {'allow_private_hosts': configured}
            messages = Queue()
            c = uwc.AsyncWebCrawler(config(base, tmp_path, respect_robots=False, find_sitemap=False, **options),
                                    msg_queue=messages)
            await asyncio.wait_for(c.crawl(), timeout=60)
            report = json.loads((tmp_path / 'crawl_report.json').read_text(encoding='utf-8'))
            assert report['network_policy'] == {'allow_private_hosts': permitted, 'reason': reason}
            assert bool(site.hits) == permitted
            assert c.delivery_status() == ('success' if permitted else 'failed')
            warnings = [data for kind, data in iter(messages.get_nowait, ('done', 'Klar'))
                        if kind == 'log' and isinstance(data, tuple)]
            expected = 'private_network_automatic' if configured is None else 'private_network_explicit'
            assert warnings == ([('WARNING', expected)] if permitted else [])
            if configured is None:
                assert any(record.levelno == logging.WARNING and 'Automatiskt intranätläge' in record.message
                           for record in caplog.records)
        finally:
            await runner.cleanup()
    run(go())


@pytest.mark.parametrize(('answers', 'reason'), [
    (['10.1.2.3', 'fd00::1'], 'intranet_dns'),
    (['10.1.2.3', '93.184.215.14'], 'public_default'),
    (['64:ff9b::7f00:1', '10.1.2.3'], 'public_default'),
    (['64:ff9b:1::1'], 'public_default'),
    (['169.254.169.254'], 'public_default'),
])
def test_dns_only_enables_auto_intranet_for_genuine_intranet_addresses(tmp_path, monkeypatch, answers, reason):
    async def go():
        async def resolve(*args, **kwargs):
            return [(socket.AF_INET6 if ':' in address else socket.AF_INET,
                     socket.SOCK_STREAM, 0, '', (address, 0)) for address in answers]

        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        crawler = uwc.AsyncWebCrawler(config('https://test.invalid', tmp_path))
        try:
            await crawler._resolve_private_policy()
            assert crawler._private_policy_reason == reason
            assert crawler._allow_private == (reason == 'intranet_dns')
            assert await crawler._check_playwright_url_allowed('https://test.invalid') == crawler._allow_private
        finally:
            crawler._close_log_handlers()
    run(go())


@pytest.mark.parametrize('address', ['64:ff9b::7f00:1', '64:ff9b:1::1', '2002:7f00:1::', '2001::1'])
def test_transition_literal_cannot_enable_intranet_or_start_http_request(tmp_path, monkeypatch, address):
    async def forbidden(*args, **kwargs):
        pytest.fail('Övergångsadressen får inte orsaka något HTTP-anrop')

    monkeypatch.setattr(uwc.aiohttp.ClientSession, '_request', forbidden)
    result = run(run_crawl(f'http://[{address}]', tmp_path, respect_robots=False, find_sitemap=False))
    assert not result._allow_private and result.delivery_status() == 'failed'
    report = json.loads((tmp_path / 'crawl_report.json').read_text(encoding='utf-8'))
    assert report['network_policy'] == {'allow_private_hosts': False, 'reason': 'public_default'}


def test_safe_resolver_rejects_mixed_nat64_answers_and_keeps_public_ipv6(monkeypatch):
    async def go():
        public = '2606:4700:4700::1111'
        answers = [{'host': public}, {'host': '64:ff9b::7f00:1'}]

        async def resolve(*args, **kwargs):
            return answers

        resolver = uwc.SafeResolver(False)
        monkeypatch.setattr(resolver._inner, 'resolve', resolve)
        try:
            with pytest.raises(uwc.BlockedDNSError):
                await resolver.resolve('test.invalid')
            answers.pop()
            assert await resolver.resolve('test.invalid') == [{'host': public}]
        finally:
            await resolver.close()
    run(go())
