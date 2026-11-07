"""Regressioner för bekräftade högprioriterade granskningsfynd, endast lokala sajter."""
import asyncio
import ctypes
import gzip
import json
import os
import queue
import sys

import pytest
from aiohttp import CookieJar, web
from yarl import URL

import crawler_runtime as runtime
from crawler_text import clean_pii_text
from test_crawler import Site, config, make_pdf, page, published, run, run_crawl, serve, uwc
from test_advanced import needs_chromium


@pytest.mark.parametrize('status', [200, 404, 410])
@pytest.mark.parametrize('mode', ['http', pytest.param('browser', marks=needs_chromium), 'document'])
def test_off_domain_redirect_never_reaches_target_or_removes_saved_source(tmp_path, mode, status):
    class Target(Site):
        async def handle(self, request):
            self.hits.append(request.path)
            return web.Response(status=status, text='Externt svar')
    class Source(Site):
        target = None
        async def handle(self, request):
            if self.target and request.path == ('/file.pdf' if mode == 'document' else '/'):
                raise web.HTTPFound(self.target)
            return await super().handle(request)
    async def go():
        source, target = Source(), Target()
        source.pages['/'] = (page('Källa', 'Kommunal källtext', [('/file.pdf', 'Policy')]
                                 if mode == 'document' else []), None)
        source.files['/file.pdf'] = (make_pdf('BEVARAD DOKUMENTVERSION'), None)
        target_runner, target_base = await serve(target)
        source_runner, base = await serve(source)
        options = dict(find_sitemap=False, use_hybrid=mode != 'browser',
                       convert_docs_to_md=mode == 'document', respect_robots=False)
        try:
            await run_crawl(base, tmp_path, **options)
            old = published(tmp_path)
            old_chunks = (old/'chunks.jsonl').read_bytes()
            source.target = target_base.replace('127.0.0.1', 'localhost') + '/missing'
            c = await run_crawl(base, tmp_path, **options)
            assert not target.hits, 'Varken robots eller svarskropp får hämtas på den otillåtna värden'
            assert c.report['off_domain_redirects']
            assert not c.report['gone_pages'] and not c.report['gone_documents']
            assert not any(ch['event'] in ('removed', 'document_removed') for ch in c.changes)
            assert c.delivery_status() != 'success'
            assert (published(tmp_path)/'chunks.jsonl').read_bytes() == old_chunks
            assert (old/'chunks.jsonl').read_bytes() == old_chunks
        finally:
            await source_runner.cleanup()
            await target_runner.cleanup()
    run(go())


@pytest.mark.parametrize('allowed', [False, True])
def test_explicitly_allowed_redirects_and_unrestricted_crawls_still_work(tmp_path, allowed):
    class Redirect(Site):
        target = ''
        async def handle(self, request):
            if request.path == '/':
                raise web.HTTPFound(self.target)
            return await super().handle(request)
    async def go():
        target, source = Site(), Redirect()
        target.pages['/'] = (page('Målsida', 'Tillåten extern källtext'), None)
        tr, target_base = await serve(target)
        sr, base = await serve(source)
        source.target = target_base.replace('127.0.0.1', 'localhost') + '/'
        try:
            c = await run_crawl(base, tmp_path, find_sitemap=False, convert_docs_to_md=False,
                               strict_domain=allowed, allowed_domains=['localhost'] if allowed else [])
            assert c.delivery_status() == 'success'
            assert 'Tillåten extern källtext' in (published(tmp_path)/'chunks.jsonl').read_text(encoding='utf-8')
        finally:
            await sr.cleanup()
            await tr.cleanup()
    run(go())


def test_untrusted_final_404_cannot_remove_page_even_if_fetch_guard_is_bypassed(tmp_path, monkeypatch):
    async def go():
        site = Site(); site.pages['/'] = (page('Källa', 'Bevara denna text'), None)
        runner, base = await serve(site)
        try:
            await run_crawl(base, tmp_path, find_sitemap=False, convert_docs_to_md=False)
            previous = published(tmp_path)
            async def fake_fetch(self, *args, **kwargs):
                return uwc.FetchResult(status=404, final_url='https://outside.invalid/missing')
            monkeypatch.setattr(uwc.AsyncWebCrawler, 'fetch', fake_fetch)
            c = await run_crawl(base, tmp_path, find_sitemap=False, respect_robots=False,
                               convert_docs_to_md=False)
            assert not c.report['gone_pages']
            assert published(tmp_path) == previous
        finally:
            await runner.cleanup()
    run(go())


class Widget:
    """Enkel widgetattrapp för konfigurationens väg genom riktiga GUI-metoder."""
    def __init__(self, value=''):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value
    def delete(self, *args):
        self.value = ''
    def insert(self, index, value):
        self.value = value


def gui_fixture(tmp_path, lang='sv'):
    gui = uwc.AppGUI.__new__(uwc.AppGUI)
    gui.lang = lang
    gui.texts = {
        'template_none': {'sv':'Ingen mall', 'en':'No template'},
        'template_loaded': {'sv':'Laddad {}', 'en':'Loaded {}'},
        'template_detached': {'sv':'Bortkopplad', 'en':'Detached'},
        'run_modes': {'headless':{'sv':'Standard', 'en':'Standard'},
                      'login_then_headless':{'sv':'Inloggning', 'en':'Login'}},
    }
    for name, value in {
        'url_entry':'https://intra.example.org/', 'delay_entry':'0.5', 'max_pages_entry':'0',
        'max_depth_entry':'0', 'concurrency_entry':'4', 'exclude_entry':'', 'require_entry':'',
        'lang_filter_entry':'', 'format_var':'.md', 'headless_var':'Standard', 'theme_switch':0,
        'dir_var':str(tmp_path/'out'), 'lang_var':'🇸🇪 SV', 'template_var':'Ingen mall',
    }.items():
        setattr(gui, name, Widget(value))
    for name in ('docs_var','convert_docs_var','hybrid_var','traf_var','sitemap_var','robots_var',
                 'strict_var','rm_email_var','rm_phone_var','rm_pnr_var','rm_ip_var'):
        setattr(gui, name, Widget(True))
    gui.full_var = Widget(False)
    gui.msg_queue = queue.Queue()
    gui._log_to_gui = lambda *args: None
    gui._error_dialog = lambda *args: None
    gui.SETTINGS_PATH = str(tmp_path/'settings.json')
    gui.templates = {'Intranät': {'start_url':'https://intra.example.org/',
        'output_dir':str(tmp_path/'chosen'), 'cookie_file':str(tmp_path/'session.json'),
        'session_check_url':'https://intra.example.org/protected', 'allowed_domains':['docs.example.org'],
        'include_subdomains':True, 'ignore_https_errors':True, 'user_agent':'TestCrawler/1',
        'ocr':False, 'keep_role_emails':True, 'max_page_mb':2, 'max_download_mb':20,
        'conversion_memory_mb':512, 'quarantine_flagged_chunks':False, 'save_format':'Ingen text'}}
    return gui


@pytest.mark.parametrize('lang', ['sv','en'])
def test_gui_preserves_advanced_template_settings_through_start_and_restart(tmp_path, monkeypatch, lang):
    gui = gui_fixture(tmp_path, lang)
    gui._apply_template('Intranät')
    assert gui.format_var.get() == ('Ingen text' if lang == 'sv' else 'No text')
    gui.rm_email_var.set(False)
    gui._save_settings()
    loaded = gui_fixture(tmp_path, lang)
    loaded._load_settings()
    assert loaded._template_choice == 'Intranät'
    assert loaded.dir_var.get() == str(tmp_path/'chosen')
    assert loaded.format_var.get() == gui.format_var.get()
    captured = []
    def capture(cfg, *args):
        captured.append(cfg)
        raise RuntimeError('Testet avslutas före trådstart')
    monkeypatch.setattr(uwc, 'AsyncWebCrawler', capture)
    loaded.start_crawl()
    result = captured[0]
    for key, value in gui.templates['Intranät'].items():
        if key != 'save_format':
            assert result[key] == value
    assert result['remove_email'] is False
    assert result['save_format'] == ('Ingen text' if lang == 'sv' else 'No text')


def test_gui_detaches_advanced_options_when_origin_changes_and_when_template_is_unselected(tmp_path):
    gui = gui_fixture(tmp_path)
    gui._apply_template('Intranät')
    assert gui._template_options_for('https://intra.example.org/other')['cookie_file']
    assert gui._template_options_for('https://other.example.org/') == {}
    assert gui._template_choice is None
    assert gui._template_options_for('https://intra.example.org/') == {}
    gui._apply_template('Intranät')
    assert gui._template_options_for('http://intra.example.org/') == {}
    gui._apply_template('Intranät')
    gui._apply_template('Ingen mall')
    assert gui._template_options_for('https://intra.example.org/') == {}
    gui._apply_template('Intranät')
    gui.templates['Annan'] = {'start_url':'https://other.example.org/'}
    gui._apply_template('Annan')
    assert 'cookie_file' not in gui._template_options_for('https://other.example.org/')


def test_cookie_transfer_preserves_host_domain_path_secure_expiry_and_filters_idp(tmp_path):
    async def go():
        c = uwc.AsyncWebCrawler(config('https://intra.example.org',tmp_path,allowed_domains=['docs.example.org']))
        try:
            cookies = [
                {'name':'shared','value':'ok','domain':'.example.org','path':'/private','secure':True},
                {'name':'host','value':'ok','domain':'intra.example.org','path':'/'},
                {'name':'expired','value':'old','domain':'.example.org','path':'/','expires':1},
                {'name':'idp','value':'secret','domain':'.identity.invalid','path':'/'},
            ]
            jar = CookieJar()
            c._inject_playwright_cookies(jar, cookies)
            assert set(jar.filter_cookies(URL('https://intra.example.org/private'))) == {'shared','host'}
            assert set(jar.filter_cookies(URL('https://docs.example.org/private'))) == {'shared'}
            assert not jar.filter_cookies(URL('https://child.intra.example.org/'))
            assert set(jar.filter_cookies(URL('http://intra.example.org/private'))) == {'host'}
            assert not jar.filter_cookies(URL('https://identity.invalid/'))
            assert len(c._site_cookies([{'name':'legacy','value':'ok','url':'https://intra.example.org/'}])) == 1
        finally:
            c._close_log_handlers()
    run(go())


def test_cookie_file_filters_idp_but_keeps_it_in_interactive_memory(tmp_path):
    path = tmp_path/'cookies.json'
    c = uwc.AsyncWebCrawler(config('https://intra.example.org', tmp_path/'out', cookie_file=str(path)))
    try:
        c.saved_cookies = [{'name':'site','value':'ok','domain':'.example.org','path':'/'},
                           {'name':'idp','value':'secret','domain':'identity.invalid','path':'/'}]
        c._save_cookie_file()
        assert [cookie['name'] for cookie in json.loads(path.read_text())] == ['site']
        assert len(c.saved_cookies) == 2
        assert c._load_cookie_file()
        assert [cookie['name'] for cookie in c.saved_cookies] == ['site']
        if os.name != 'nt':
            assert path.stat().st_mode & 0o077 == 0
        else:
            from ctypes import wintypes as wt
            advapi = ctypes.WinDLL('advapi32', use_last_error=True)
            advapi.GetFileSecurityW.argtypes = [wt.LPCWSTR,wt.DWORD,wt.LPVOID,wt.DWORD,ctypes.POINTER(wt.DWORD)]
            needed = wt.DWORD()
            advapi.GetFileSecurityW(str(path),4,None,0,ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            assert advapi.GetFileSecurityW(str(path),4,buffer,needed,ctypes.byref(needed))
            advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
                wt.LPVOID,wt.DWORD,wt.DWORD,ctypes.POINTER(wt.LPWSTR),ctypes.POINTER(wt.DWORD)]
            result = wt.LPWSTR()
            assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer,1,4,ctypes.byref(result),None)
            try:
                assert result.value.startswith('D:P')
                assert result.value.count('(A;') == 2
                assert ';;;OW)' in result.value and ';;;SY)' in result.value
            finally:
                kernel = ctypes.WinDLL('kernel32')
                kernel.LocalFree.argtypes = [wt.LPVOID]
                kernel.LocalFree(result)
    finally:
        c._close_log_handlers()


def test_private_cookie_write_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path/'cookies.json'
    runtime.write_private_json(str(path), ['previous'])
    def fail(*args):
        raise OSError('Simulerat atomiskt skrivfel')
    monkeypatch.setattr(runtime.os,'replace',fail)
    with pytest.raises(OSError):
        runtime.write_private_json(str(path),['new-secret'])
    assert json.loads(path.read_text()) == ['previous']
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('phone', [False, True])
def test_pii_distinguishes_unlabelled_mobile_numbers_and_real_person_numbers(phone):
    cfg = {'remove_phone':phone,'remove_pnr':True}
    for number in ('0701122334','0701234567','070112-2334'):
        assert clean_pii_text(cfg,number) == ('[TELEFON]' if phone else number)
    for number in ('850709-9805','198507099805','900101+0017','2001011234','0701122335'):
        assert clean_pii_text(cfg,number) == '[PERSONNUMMER]'
    assert clean_pii_text({'remove_phone':False,'remove_pnr':True}, 'Mobil: 0701234567') == 'Mobil: 0701234567'


def test_pii_uses_context_for_ambiguous_numbers_and_preserves_document_numbering():
    cfg = {'remove_pnr':True,'remove_phone':False,'remove_ip':True}
    assert clean_pii_text(cfg,'Personnummer: 0701122334') == 'Personnummer: [PERSONNUMMER]'
    assert clean_pii_text(cfg,'Telefon: 0701122335') == 'Telefon: 0701122335'
    for text in ('Avsnitt 1.2.3.4','§ 1.2.3.4','version: 10.20.30.40','999.888.777.666','8502301234'):
        assert clean_pii_text(cfg,text) == text
    assert clean_pii_text(cfg,'Server: 192.168.1.2') == 'Server: [IP-ADRESS]'


@needs_chromium
@pytest.mark.parametrize('length_header', [False,True])
def test_browser_page_limit_aborts_stream_before_full_download(tmp_path,length_header):
    class StreamSite(Site):
        sent = 0
        disconnected = None
        async def handle(self,request):
            headers = {'Content-Type':'text/html'}
            if length_header:
                headers['Content-Length'] = str(65536*100)
            response = web.StreamResponse(headers=headers)
            await response.prepare(request)
            try:
                for _ in range(100):
                    await response.write(b'x'*65536)
                    self.sent += 1
                    await asyncio.sleep(0.03)
                await response.write_eof()
            except ConnectionError:
                self.disconnected.set()
            return response
    async def go():
        site = StreamSite(); site.disconnected = asyncio.Event()
        runner,base = await serve(site)
        try:
            c = await run_crawl(base,tmp_path,use_hybrid=False,find_sitemap=False,respect_robots=False,
                               convert_docs_to_md=False,max_page_mb=0.1)
            assert c.delivery_status() == 'failed'
            assert not (tmp_path/'current.json').exists()
            await asyncio.wait_for(site.disconnected.wait(),3)
            assert site.sent < 15
        finally:
            await runner.cleanup()
    run(go())


@needs_chromium
@pytest.mark.parametrize('kind', ['dom','script','aggregate'])
def test_browser_limit_covers_generated_dom_and_subresources_and_preserves_generation(tmp_path,kind):
    class BrowserSite(Site):
        oversized = False
        async def handle(self,request):
            if request.path == '/script.js':
                return web.Response(body=b'/*'+b'x'*200000+b'*/',content_type='application/javascript')
            if request.path in ('/first.js','/second.js'):
                return web.Response(body=b'/*'+b'x'*35000+b'*/',content_type='application/javascript')
            if self.oversized and request.path == '/':
                script = ("<script>document.body.append('x'.repeat(200000))</script>" if kind == 'dom'
                          else '<script src="/script.js"></script>')
                if kind == 'aggregate':
                    script = '<script src="/first.js"></script><script src="/second.js"></script>'
                return web.Response(text=page('Källa','Text')+script,content_type='text/html')
            return await super().handle(request)
    async def go():
        site = BrowserSite(); site.pages['/'] = (page('Källa','Bevarad text'),None)
        runner,base = await serve(site)
        options = dict(use_hybrid=False,find_sitemap=False,respect_robots=False,
                       convert_docs_to_md=False,max_page_mb=0.05)
        try:
            await run_crawl(base,tmp_path,**options)
            old = published(tmp_path)
            site.oversized = True
            c = await run_crawl(base,tmp_path,**options)
            assert c.delivery_status() == 'failed'
            assert published(tmp_path) == old
        finally:
            await runner.cleanup()
    run(go())


@needs_chromium
def test_browser_bounded_transfer_preserves_gzip_html_and_javascript(tmp_path):
    class CompressedSite(Site):
        async def handle(self,request):
            if request.path == '/script.js':
                body = b"document.querySelector('p').append(' SCRIPT_RESULT');"
                kind = 'application/javascript'
            else:
                body = (page('Komprimerad','Läsbar text')+'<script src="/script.js"></script>').encode()
                kind = 'text/html; charset=utf-8'
            return web.Response(body=gzip.compress(body),headers={'Content-Encoding':'gzip','Content-Type':kind})
    async def go():
        runner,base = await serve(CompressedSite())
        try:
            c = await run_crawl(base,tmp_path,use_hybrid=False,find_sitemap=False,respect_robots=False,
                               convert_docs_to_md=False)
            assert c.delivery_status() == 'success'
            assert 'SCRIPT_RESULT' in (published(tmp_path)/'chunks.jsonl').read_text(encoding='utf-8')
        finally:
            await runner.cleanup()
    run(go())


@pytest.mark.skipif(os.name != 'nt' and not sys.platform.startswith('linux'), reason='Kräver Windows-jobb eller Linux prlimit')
def test_worker_memory_limit_applies_to_children_and_next_job_can_still_run(tmp_path):
    worker = tmp_path/'memory_worker.py'
    worker.write_text('''import json,pathlib,subprocess,sys
sys.stdin.buffer.read(1)
request=json.loads(pathlib.Path(sys.argv[1]).read_text())
if request['options'].get('allocate'):
    code="import sys\\ntry: value=bytearray(256*1024*1024)\\nexcept MemoryError: sys.exit(42)"
    child=subprocess.run([sys.executable,'-c',code])
    result={'path':None,'error':'minnesgränsen' if child.returncode==42 else 'gränsen fungerade inte'}
else:
    path=pathlib.Path(request['output'])/'texter'/'output_doc.md'
    path.parent.mkdir(parents=True)
    path.write_text('Text från nästa jobb',encoding='utf-8')
    result={'path':str(path),'error':''}
pathlib.Path(sys.argv[2]).write_text(json.dumps(result))
''',encoding='utf-8')
    async def go():
        source = tmp_path/'source.pdf'; source.write_bytes(b'%PDF-test')
        runner = runtime.ConversionProcessRunner(str(tmp_path/'jobs'),worker_script=str(worker),memory_mb=64,timeout=10)
        try:
            result = await runner.run(str(source),'https://test.invalid/doc',{}, {'allocate':True},'output_doc.md',10000)
            assert not result['text'] and result['error'] == 'minnesgränsen'
            result = await runner.run(str(source),'https://test.invalid/doc',{}, {},'output_doc.md',10000)
            assert result['text'] == 'Text från nästa jobb', result['error']
            assert not runner.active and not list((tmp_path/'jobs').iterdir())
        finally:
            await runner.close()
    run(go())


@pytest.mark.parametrize('value', [0,63,float('inf')])
def test_invalid_conversion_memory_budget_is_rejected(tmp_path,value):
    with pytest.raises(ValueError, match='conversion_memory_mb'):
        uwc.AsyncWebCrawler(config('https://test.invalid',tmp_path,conversion_memory_mb=value))
