"""busbot.tgproxy: VPN keys from Happ / v2rayN -> Xray config for the tg-proxy container.
The tests with a real Xray run only if it is installed (`xray` in PATH or XRAY_PATH), like the Chromium ones."""
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from aiogram.client.telegram import TelegramAPIServer

from busbot import tgproxy
from busbot.__main__ import make_bot
from test_proxy import TOKEN, fake_telegram

UUID = "3f8a4c2e-9b1d-4e7a-8c5f-2d6b0a9e1f34"
PBK = "Z84J2IelR9ch3k8VtlVhhs5ycBUlXA7wHBWcBrjqnAw"
REALITY = (f"vless://{UUID}@nl1.example.net:443?type=tcp&security=reality&pbk={PBK}&fp=chrome"
           f"&sni=www.microsoft.com&sid=6ba85179e30d4fc2&spx=%2F&flow=xtls-rprx-vision&encryption=none"
           "#%F0%9F%87%B3%F0%9F%87%B1%20Нидерланды")
WS_TLS = (f"vless://{UUID}@cdn.example.net:443?type=ws&security=tls&path=%2Fws%3Fed%3D2048&host=cdn.example.net"
          "&sni=cdn.example.net&fp=firefox&alpn=h2%2Chttp%2F1.1#ws")
XHTTP = (f"vless://{UUID}@x.example.net:443?type=xhttp&security=reality&pbk={PBK}&sni=x.example.net&sid=ab"
         "&path=%2Fxh&mode=stream-one&extra=%7B%22xPaddingBytes%22%3A%22100-1000%22%7D#xhttp")
TROJAN = "trojan://p%40ss@t.example.net:443?type=grpc&serviceName=tg&mode=multi&sni=t.example.net#trojan"
VMESS = "vmess://" + base64.b64encode(json.dumps({
    "v": "2", "ps": "vmess", "add": "v.example.net", "port": "443", "id": UUID, "aid": "0", "scy": "auto",
    "net": "ws", "type": "none", "host": "v.example.net", "path": "/vm", "tls": "tls", "sni": "v.example.net",
}).encode()).decode()
SS_B64 = "ss://" + base64.urlsafe_b64encode(b"chacha20-ietf-poly1305:s3cr3t").decode().rstrip("=") \
         + "@1.2.3.4:8388/?outline=1#outline"
SS_2022 = "ss://2022-blake3-aes-128-gcm:%2B%2B8wMTIzNDU2Nzg5YWJjZA%3D%3D@5.6.7.8:443#ss2022"
SS_LEGACY = "ss://" + base64.b64encode(b"aes-256-gcm:p@ss:w@9.9.9.9:8443").decode() + "#legacy"
ALL = [REALITY, WS_TLS, XHTTP, TROJAN, VMESS, SS_B64, SS_2022, SS_LEGACY]


def test_vless_reality_vision():
    name, ob = tgproxy.outbound(REALITY)
    assert name == "🇳🇱 Нидерланды"
    assert ob == {"protocol": "vless",
                  "settings": {"vnext": [{"address": "nl1.example.net", "port": 443, "users": [
                      {"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
                  "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
                      "serverName": "www.microsoft.com", "fingerprint": "chrome", "publicKey": PBK,
                      "shortId": "6ba85179e30d4fc2", "spiderX": "/"}}}


def test_transports_and_protocols():
    st = tgproxy.outbound(WS_TLS)[1]["streamSettings"]
    assert st["wsSettings"] == {"path": "/ws?ed=2048", "host": "cdn.example.net"}
    assert st["tlsSettings"] == {"serverName": "cdn.example.net", "fingerprint": "firefox", "alpn": ["h2", "http/1.1"]}
    st = tgproxy.outbound(XHTTP)[1]["streamSettings"]
    assert st["xhttpSettings"] == {"path": "/xh", "mode": "stream-one", "extra": {"xPaddingBytes": "100-1000"}}
    ob = tgproxy.outbound(TROJAN)[1]
    assert ob["settings"]["servers"][0] == {"address": "t.example.net", "port": 443, "password": "p@ss"}
    assert ob["streamSettings"]["security"] == "tls"                 # trojan defaults to TLS
    assert ob["streamSettings"]["grpcSettings"] == {"serviceName": "tg", "multiMode": True}
    name, ob = tgproxy.outbound(VMESS)
    assert name == "vmess" and ob["settings"]["vnext"][0]["users"] == [{"id": UUID, "security": "auto"}]
    assert ob["streamSettings"]["wsSettings"] == {"path": "/vm", "host": "v.example.net"}
    servers = [tgproxy.outbound(x)[1]["settings"]["servers"][0] for x in (SS_B64, SS_2022, SS_LEGACY)]
    assert servers == [
        {"address": "1.2.3.4", "port": 8388, "method": "chacha20-ietf-poly1305", "password": "s3cr3t"},
        {"address": "5.6.7.8", "port": 443, "method": "2022-blake3-aes-128-gcm", "password": "++8wMTIzNDU2Nzg5YWJjZA=="},
        {"address": "9.9.9.9", "port": 8443, "method": "aes-256-gcm", "password": "p@ss:w"}]


def test_unsupported_links_are_explained():
    for link, msg in (("hysteria2://pw@h.example.net:443#hy2", "не поддерживается"),
                      ("happ://crypt3/AbCdEf", "happ://"),
                      (f"vless://{UUID}@h.example.net:443?type=kcp", "kcp"),
                      (f"vless://{UUID}@h.example.net:443?security=reality&sni=a", "pbk"),
                      ("ss://YWVzLTI1Ni1nY206cA@1.2.3.4:1?plugin=v2ray-plugin", "plugin")):
        with pytest.raises(ValueError, match=msg):
            tgproxy.outbound(link)


def test_subscription_and_cli(tmp_path, capsys):
    body = base64.b64encode("\n".join(["hysteria2://x@h.example.net:443#hy2", REALITY, TROJAN]).encode()).decode()

    class Sub(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Sub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/sub/abc"
        out = tmp_path / "xray" / "config.json"
        assert tgproxy.main([url, "3", f"--out={out}"]) == 0          # pick the 3rd: trojan
        cfg = json.loads(out.read_text(encoding="utf-8"))
        assert cfg["outbounds"][0]["protocol"] == "trojan" and cfg["outbounds"][0]["tag"] == "proxy"
        assert cfg["inbounds"][0] == {"tag": "telegram", "listen": "0.0.0.0", "port": 1080, "protocol": "socks",
                                      "settings": {"auth": "noauth", "udp": False}}
        assert tgproxy.main([url, "1", f"--out={out}"]) == 1          # 1 is hysteria2: not usable
        assert tgproxy.main([url, f"--out={out}"]) == 0               # default: first usable (reality)
        assert json.loads(out.read_text(encoding="utf-8"))["outbounds"][0]["protocol"] == "vless"
    finally:
        srv.shutdown()
    printed = capsys.readouterr().out
    assert "пропущен" in printed and "nl1.example.net:443" in printed
    assert UUID not in printed and "p@ss" not in printed                # secrets never printed


# ---- real Xray (skipped without it)

XRAY = os.environ.get("XRAY_PATH") or shutil.which("xray")
needs_xray = pytest.mark.skipif(not XRAY, reason="xray not installed (set XRAY_PATH)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@needs_xray
@pytest.mark.parametrize("link", ALL)
def test_xray_accepts_generated_config(link, tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps(tgproxy.config(tgproxy.outbound(link)[1])), encoding="utf-8")
    r = subprocess.run([XRAY, "run", "-test", "-c", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_xray
def test_bot_reaches_telegram_through_xray(tmp_path):
    """bot -> SOCKS5 -> Xray client (our config) -> VLESS/ws -> Xray server -> fake Telegram.
    tg.test resolves only inside the Xray server, so a direct connection cannot succeed."""
    sp, cp = _free_port(), _free_port()
    procs = []

    async def wait_port(port: int) -> None:
        for _ in range(100):
            try:
                _, w = await asyncio.open_connection("127.0.0.1", port)
                w.close()
                return
            except OSError:
                await asyncio.sleep(0.05)
        raise TimeoutError(f"xray did not listen on {port}")

    async def run():
        runner, tg_port = await fake_telegram()
        server = {"log": {"loglevel": "warning"}, "dns": {"hosts": {"tg.test": "127.0.0.1"}},
                  "inbounds": [{"listen": "127.0.0.1", "port": sp, "protocol": "vless",
                                "settings": {"clients": [{"id": UUID}], "decryption": "none"},
                                "streamSettings": {"network": "ws", "wsSettings": {"path": "/tg"}}}],
                  # Xray 26 freedom blocks loopback behind a VLESS inbound unless allowed explicitly
                  "outbounds": [{"protocol": "freedom", "settings": {
                      "domainStrategy": "UseIP", "finalRules": [{"action": "allow", "ip": ["127.0.0.1"]}]}}]}
        client = tgproxy.config(tgproxy.outbound(f"vless://{UUID}@127.0.0.1:{sp}?type=ws&path=%2Ftg#t")[1])
        client["inbounds"][0] |= {"listen": "127.0.0.1", "port": cp}
        for n, c in (("server", server), ("client", client)):
            f = tmp_path / f"{n}.json"
            f.write_text(json.dumps(c), encoding="utf-8")
            procs.append(subprocess.Popen([XRAY, "run", "-c", str(f)], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL))
        try:
            await wait_port(sp)
            await wait_port(cp)
            api = TelegramAPIServer.from_base(f"http://tg.test:{tg_port}")
            bot = make_bot(TOKEN, f"socks5://127.0.0.1:{cp}", api=api)
            assert (await bot.get_me()).username == "t_bot"
            await bot.session.close()
            direct = make_bot(TOKEN, "", api=api)
            with pytest.raises(Exception):
                await direct.get_me()
            await direct.session.close()
        finally:
            await runner.cleanup()

    try:
        asyncio.run(run())
    finally:
        for p in procs:
            p.kill()
            p.wait()
