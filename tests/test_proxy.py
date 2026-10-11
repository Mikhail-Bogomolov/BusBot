"""TELEGRAM_PROXY: Telegram traffic must go through the proxy and nowhere else."""
import asyncio

import pytest
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web

from busbot.__main__ import make_bot, proxy_label

TOKEN = "123456:TESTTOKEN"
# a name that cannot resolve: a direct connection attempt fails, so success proves the proxy was used
FAKE_API = TelegramAPIServer(base="http://telegram.invalid/bot{token}/{method}",
                             file="http://telegram.invalid/file/bot{token}/{path}")


async def fake_telegram():
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "result": {"id": 123456, "is_bot": True, "first_name": "t",
                                                         "username": "t_bot"}})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def connect_proxy(target_port: int):
    """Minimal HTTP CONNECT proxy: records the requested target and credentials, then tunnels the bytes
    to the local fake Telegram instead of the real host."""
    seen: list[tuple[str, str]] = []

    async def pipe(r, w):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except ConnectionError:
            pass
        finally:
            w.close()

    async def client(r, w):
        head = await r.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        method, target, _ = lines[0].split(" ")
        auth = next((x.split(": ", 1)[1] for x in lines if x.lower().startswith("proxy-authorization")), "")
        seen.append((f"{method} {target}", auth))
        ur, uw = await asyncio.open_connection("127.0.0.1", target_port)
        w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await w.drain()
        await asyncio.gather(pipe(r, uw), pipe(ur, w))

    srv = await asyncio.start_server(client, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], seen


def test_telegram_requests_go_through_proxy():
    async def run():
        runner, tg_port = await fake_telegram()
        srv, port, seen = await connect_proxy(tg_port)
        try:
            bot = make_bot(TOKEN, f"http://user:secret@127.0.0.1:{port}", api=FAKE_API)
            assert (await bot.get_me()).username == "t_bot"
            assert seen and seen[0][0] == "CONNECT telegram.invalid:80"
            assert seen[0][1].startswith("Basic ")                      # proxy credentials were sent
            await bot.session.close()
            direct = make_bot(TOKEN, "", api=FAKE_API)                  # no proxy: telegram.invalid is unreachable
            with pytest.raises(Exception):
                await direct.get_me()
            await direct.session.close()
        finally:
            srv.close()
            await runner.cleanup()

    asyncio.run(run())


def test_proxy_validation_and_no_secrets_in_label():
    for bad in ("127.0.0.1:8888", "ftp://h:21", "https://p.example:443", "http://h", "socks5://:1080"):
        with pytest.raises(RuntimeError, match="TELEGRAM_PROXY"):
            make_bot(TOKEN, bad)
    assert proxy_label("socks5://user:pass@10.0.0.5:1080") == "socks5://10.0.0.5:1080"
    for ok in ("socks5://tg-proxy:1080", "http://vpn:8888", "socks4://10.0.0.1:1080"):
        assert make_bot(TOKEN, ok).session.proxy == ok
    with pytest.raises(RuntimeError) as e:
        make_bot(TOKEN, "http://u:topsecret@host")
    assert "topsecret" not in str(e.value)


def test_empty_value_with_inline_comment_is_empty(monkeypatch):
    """Older python-dotenv reads `KEY=   # note` as '# note'; the config must not treat that as a value."""
    from busbot import config
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_PROXY", "# none")
    monkeypatch.setenv("ADMIN_ID", "# кому слать алерты")
    monkeypatch.setenv("ALLOWED_USERS", "# id через запятую")
    c = config.load()
    assert c.telegram_proxy == "" and c.admin_id is None and c.allowed_users == frozenset()
    monkeypatch.setenv("TELEGRAM_PROXY", " socks5://tg-proxy:1080 ")
    monkeypatch.setenv("ALLOWED_USERS", "1, 2")
    c = config.load()
    assert c.telegram_proxy == "socks5://tg-proxy:1080" and c.allowed_users == {1, 2}
