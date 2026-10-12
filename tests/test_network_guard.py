"""Lokala tester av utgående browser-trafik och dokumentens SSRF-skydd."""
import asyncio
import ipaddress
import queue

import pytest
from aiohttp import web

from test_crawler import Site, config, make_pdf, run, serve, uwc
from test_advanced import needs_chromium


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', '10.0.0.1',
                                  '169.254.169.254', '::1', 'fc00::1'])
def test_proxy_rejects_private_literals_without_connecting(monkeypatch, host):
    async def forbidden(*args, **kwargs):
        pytest.fail('Privata mål får aldrig öppnas')
    monkeypatch.setattr(uwc.asyncio, 'open_connection', forbidden)
    async def go():
        proxy = uwc.BrowserEgressProxy()
        try:
            with pytest.raises(uwc.BlockedHostError):
                await proxy._connect(host, 80)
        finally:
            await proxy.close()
    run(go())


def test_proxy_pins_connection_to_validated_dns_address(monkeypatch):
    calls = []
    async def connect(sock, address):
        calls.append(address)
    async def go():
        monkeypatch.setattr(asyncio.get_running_loop(), 'sock_connect', connect)
        proxy = uwc.BrowserEgressProxy()
        async def resolve(host, port, family):
            return [{'host': '93.184.215.14'}]
        monkeypatch.setattr(proxy.resolver, 'resolve', resolve)
        try:
            reader, writer = await proxy._connect('public.test', 443)
            assert isinstance(reader, uwc._ProxySocketReader)
            writer.close()
            assert calls == [('93.184.215.14', 443)]
        finally:
            await proxy.close()
    run(go())


@pytest.mark.parametrize('addresses', [['10.1.2.3'], ['93.184.215.14', '127.0.0.1']])
def test_proxy_rejects_private_and_mixed_dns_results(monkeypatch, addresses):
    async def forbidden(*args, **kwargs):
        pytest.fail('Privata DNS-resultat får aldrig öppnas')
    monkeypatch.setattr(uwc.asyncio, 'open_connection', forbidden)
    async def go():
        proxy = uwc.BrowserEgressProxy()
        async def resolve(host, port, family):
            return [{'host': addr} for addr in addresses]
        monkeypatch.setattr(proxy.resolver, 'resolve', resolve)
        try:
            with pytest.raises(uwc.BlockedHostError):
                await proxy._connect('public.test', 443)
        finally:
            await proxy.close()
    run(go())


def test_socks_protocol_blocks_loopback_and_closes_connections():
    async def go():
        proxy = uwc.BrowserEgressProxy()
        await proxy.start()
        port = uwc.urlparse(proxy.url).port
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            writer.write(b'\x05\x01\x00')
            await writer.drain()
            assert await reader.readexactly(2) == b'\x05\x00'
            writer.write(b'\x05\x01\x00\x01' + ipaddress.ip_address('127.0.0.1').packed
                         + (80).to_bytes(2, 'big'))
            await writer.drain()
            reply = await reader.readexactly(10)
            assert reply[1] != 0
        finally:
            writer.close()
            await writer.wait_closed()
            await proxy.close()
        assert not proxy.tasks
    run(go())


def test_proxy_close_handles_client_whose_handler_has_not_started(monkeypatch):
    async def go():
        proxy = uwc.BrowserEgressProxy()
        gate = asyncio.Event()
        original = proxy._handle
        async def delayed(reader, writer):
            await gate.wait()
            await original(reader, writer)
        monkeypatch.setattr(proxy, '_handle', delayed)
        await proxy.start()
        reader, writer = await asyncio.open_connection('127.0.0.1', uwc.urlparse(proxy.url).port)
        try:
            await asyncio.sleep(0.01)
            await asyncio.wait_for(proxy.close(), timeout=1)
            assert await asyncio.wait_for(reader.read(), timeout=1) == b''
            assert not proxy.tasks
        finally:
            gate.set()
            writer.close()
            await writer.wait_closed()
    run(go())


async def local_public_browser(base, out, monkeypatch):
    """Simulerar en publik startvärd helt lokalt. Andra mål släpps aldrig igenom."""
    port = uwc.urlparse(base).port
    public = f'http://public.test:{port}'
    c = uwc.AsyncWebCrawler(config(public, out, respect_robots=False, find_sitemap=False), queue.Queue())
    c._dns_allowed_cache['public.test'] = True
    c._browser_proxy = uwc.BrowserEgressProxy()
    await c._browser_proxy.start()
    checked = []
    async def connect(host, target_port):
        checked.append(host)
        if host == 'public.test' and target_port == port:
            sock = uwc.socket.socket(uwc.socket.AF_INET, uwc.socket.SOCK_STREAM)
            sock.setblocking(False)
            try:
                await asyncio.get_running_loop().sock_connect(sock, ('127.0.0.1', port))
                return uwc._ProxySocketReader(sock), uwc._ProxySocketWriter(sock)
            except BaseException:
                sock.close()
                raise
        raise uwc.BlockedHostError('Alla andra testmål nekas')
    monkeypatch.setattr(c._browser_proxy, '_connect', connect)
    return c, public, checked


@needs_chromium
def test_browser_redirect_cannot_bypass_egress_guard(tmp_path, monkeypatch):
    class RedirectSite(Site):
        async def handle(self, request):
            self.hits.append(request.path)
            if request.path == '/redirect':
                raise web.HTTPFound(f'http://localhost:{request.url.port}/private')
            return web.Response(text='HEMLIG TESTTEXT', content_type='text/html')
    async def go():
        site = RedirectSite()
        runner, base = await serve(site)
        c, public, checked = await local_public_browser(base, tmp_path, monkeypatch)
        try:
            html, _ = await c._render_with_playwright(public + '/redirect')
            assert html is None
            assert '/redirect' in site.hits and '/private' not in site.hits
            # IP-/localhost-mål stoppas redan före sändning; publika namn går genom proxyn.
            assert 'public.test' in checked
            assert 'localhost' not in checked
        finally:
            await c._shutdown()
            c._close_log_handlers()
            await runner.cleanup()
    run(go())


@needs_chromium
def test_subresource_redirect_and_popup_use_guarded_proxy(tmp_path, monkeypatch):
    class ResourceSite(Site):
        async def handle(self, request):
            self.hits.append(request.path)
            if request.path == '/':
                html = ("<html><script>window.finished=false;fetch('/redirect').catch(()=>{}).finally(()=>window.finished=true);"
                        f"window.open('http://127.0.0.1:{request.url.port}/private');"
                        "</script><body>Publikt innehåll</body></html>")
                return web.Response(text=html, content_type='text/html')
            if request.path == '/redirect':
                raise web.HTTPFound(f'http://localhost:{request.url.port}/private')
            return web.Response(text='Privat innehåll')
    async def go():
        site = ResourceSite()
        runner, base = await serve(site)
        c, public, checked = await local_public_browser(base, tmp_path, monkeypatch)
        try:
            ctx = await c.get_playwright_context()
            page = await ctx.new_page()
            await page.goto(public + '/', timeout=10000)
            await page.wait_for_function('window.finished', timeout=10000)
            assert '/redirect' in site.hits
            assert '/private' not in site.hits
            assert 'localhost' in checked and '127.0.0.1' in checked
        finally:
            await asyncio.wait_for(c._shutdown(), timeout=10)
            c._close_log_handlers()
            await runner.cleanup()
    run(go())


@needs_chromium
def test_browser_attachment_download_uses_guarded_proxy(tmp_path, monkeypatch):
    data = make_pdf('Dokument via skyddad browser')
    class DownloadSite(Site):
        async def handle(self, request):
            self.hits.append(request.path)
            return web.Response(body=data, content_type='application/pdf',
                                headers={'Content-Disposition': 'attachment; filename="test.pdf"'})
    async def go():
        site = DownloadSite()
        runner, base = await serve(site)
        c, public, checked = await local_public_browser(base, tmp_path, monkeypatch)
        target = tmp_path / 'download.part'
        try:
            assert await c._download_via_playwright(public + '/file.pdf', str(target))
            assert target.read_bytes() == data
            assert 'public.test' in checked
        finally:
            await c._shutdown()
            c._close_log_handlers()
            await runner.cleanup()
    run(go())


def test_private_aiohttp_document_target_is_blocked_without_browser(tmp_path, monkeypatch):
    async def no_browser(*args):
        pytest.fail('SSRF-blockering får inte utlösa en oskyddad reservväg')
    monkeypatch.setattr(uwc.AsyncWebCrawler, '_download_via_playwright', no_browser)
    async def go():
        c = uwc.AsyncWebCrawler(config('https://public.test', tmp_path), queue.Queue())
        await c.db.connect()
        try:
            await c.download_document('http://127.0.0.1/secret.pdf')
            assert c.stats.documents_downloaded == 0
            assert not list((tmp_path / 'dokument').glob('*.pdf'))
            assert c.report['download_failures']
        finally:
            await c._shutdown()
            c._close_log_handlers()
    run(go())
