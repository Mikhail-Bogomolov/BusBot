"""Runs the real Playwright source against a local fake of Yandex Maps.
Needs a Chromium: Playwright's bundled one, or CHROME_PATH=/path/to/chrome."""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

pw = pytest.importorskip("playwright.async_api")

from busbot.sources.yandex import YandexConfig, YandexStopSource  # noqa: E402
from busbot.stop_engine import SourceBlocked  # noqa: E402

CALLS = {"stopinfo": 0}

PAGE = """<html><body><div id=map>map</div><script>
const sid = new URLSearchParams(location.search).get('masstransit[stopId]');
fetch('/maps/api/masstransit/getVehiclesInfoWithRegion?ajax=1&lineId=1').then(r => r.json());
setTimeout(() => fetch('/maps/api/masstransit/getStopInfo?ajax=1&id=' + sid).then(r => r.json()), 700);
</script></body></html>"""


def stop_json(sid):
    return {"data": {"properties": {"currentTime": 1_700_000_000_000, "StopMetaData": {
        "id": sid, "name": "Локальная", "Transport": [{"name": "135", "type": "bus", "threads": [
            {"BriefSchedule": {"Events": [{"Estimated": {"value": "1700000240"}, "vehicleId": "v1"}]}}]}]}}}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): ...

    def _send(self, code, body, ctype="text/html", headers=None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/maps/-/short":
            return self._send(302, "", headers={"Location": "/maps/62/krasnoyarsk/?masstransit%5BstopId%5D=stop__123&z=19"})
        if u.path == "/maps/cap":
            return self._send(302, "", headers={"Location": "/showcaptcha?retpath=x"})
        if u.path == "/showcaptcha":
            return self._send(200, "<html>captcha</html>")
        if u.path == "/maps/forbidden/":
            return self._send(403, "<html>forbidden</html>")
        if u.path == "/maps/missing/":
            return self._send(404, "<html>nf</html>")
        if u.path == "/maps/silent/":
            return self._send(200, "<html>no data here</html>")
        if u.path == "/maps/api/masstransit/getVehiclesInfoWithRegion":
            return self._send(200, json.dumps({"data": {"vehicles": []}}), "application/json")
        if u.path == "/maps/api/masstransit/getStopInfo":
            CALLS["stopinfo"] += 1
            sid = parse_qs(u.query)["id"][0]
            return self._send(200, json.dumps(stop_json(sid)), "application/json")
        if u.path.startswith("/maps/"):
            return self._send(200, PAGE)
        if u.path.endswith(".png"):
            return self._send(200, "x", "image/png")
        return self._send(404, "nf")


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def cfg(tmp_path, **kw):
    d = dict(chrome_path=os.environ.get("CHROME_PATH", ""), profile_dir=str(tmp_path / "prof"),
             dump_dir=str(tmp_path / "dump"), page_timeout_s=5, refresh_s=1.0, blocked_pause_s=600,
             allowed_hosts=("127.0.0.1",))
    d.update(kw)
    return YandexConfig(**d)


async def _start(c):
    src = YandexStopSource(c)
    try:
        await src.start()
    except Exception as e:  # no browser available
        pytest.skip(f"browser unavailable: {e}")
    return src


def test_resolve_watch_refresh(server, tmp_path):
    async def run():
        src = await _start(cfg(tmp_path))
        try:
            ua = await src._user_agent()
            assert "Headless" not in ua
            snap, watch_url = await src.resolve(server + "/maps/-/short")      # short link -> redirect
            assert snap.stop_id == "stop__123" and "stop__123" in watch_url
            assert snap.routes["135|bus"].arrivals[0].vehicle_id == "v1"
            assert (tmp_path / "dump" / "stop__123.json").exists()
            assert (tmp_path / "dump" / "stop__123_getVehiclesInfoWithRegion.json").exists()

            with pytest.raises(ValueError):
                await src.resolve("https://evil.example.com/maps/x")

            await src.sync({"stop__777": server + "/maps/62/krasnoyarsk/?masstransit[stopId]=stop__777"})
            await src.refresh()                                                # opens tab
            for _ in range(50):
                if src.snapshot("stop__777"):
                    break
                await asyncio.sleep(0.1)
            s1 = src.snapshot("stop__777")
            assert s1 is not None and s1.name == "Локальная"
            n = CALLS["stopinfo"]
            await asyncio.sleep(1.2)
            await src.refresh()                                                # stale -> reload
            for _ in range(50):
                if CALLS["stopinfo"] > n and src.snapshot("stop__777").fetched_at > s1.fetched_at:
                    break
                await asyncio.sleep(0.1)
            assert src.snapshot("stop__777").fetched_at > s1.fetched_at
            await src.sync({})                                                 # unwatch closes tab
            assert src._watches == {}
            assert src.problem() is None
        finally:
            await src.close()

    asyncio.run(run())


def test_captcha_and_timeout(server, tmp_path):
    async def run():
        src = await _start(cfg(tmp_path, page_timeout_s=2))
        try:
            with pytest.raises(TimeoutError):
                await src.resolve(server + "/maps/silent/")
            with pytest.raises(ValueError):
                await src.resolve(server + "/maps/missing/")             # 404: bad link, no pause
            assert src.problem() is None
            with pytest.raises(SourceBlocked):
                await src.resolve(server + "/maps/cap")
            assert "капчу" in src.problem()
            with pytest.raises(SourceBlocked):          # paused: no new navigation at all
                await src.resolve(server + "/maps/-/short")
        finally:
            await src.close()

    asyncio.run(run())


def test_http_403_pauses_source_fast(server, tmp_path):
    async def run():
        src = await _start(cfg(tmp_path, page_timeout_s=20))
        try:
            t0 = asyncio.get_running_loop().time()
            with pytest.raises(SourceBlocked):
                await src.resolve(server + "/maps/forbidden/")
            assert asyncio.get_running_loop().time() - t0 < 5          # no waiting for the full timeout
            assert "HTTP 403" in src.problem()
        finally:
            await src.close()
        # stale profile lock from a previous container must not break the next start
        lock = tmp_path / "prof" / "SingletonLock"
        if lock.is_symlink() or lock.exists():
            lock.unlink()
        try:
            lock.symlink_to("other-host-12345")      # Linux: Chromium's lock is a symlink
        except OSError:                              # Windows without symlink rights
            lock.write_text("other-host-12345")
        src2 = await _start(cfg(tmp_path))
        await src2.close()

    asyncio.run(run())
