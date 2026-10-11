"""HTTP stop source against a local fake of Yandex Maps, built from real Krasnoyarsk responses."""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from busbot.sources.yandex import YandexConfig
from busbot.sources.yandex_http import YandexHttpSource, found_stops
from busbot.stop_engine import SourceBlocked

FIX = Path(__file__).parent / "fixtures"
STOP = json.loads((FIX / "getStopInfo_2026_krsk.json").read_text(encoding="utf-8"))["data"]
SEARCH = json.loads((FIX / "search_stops_krsk.json").read_text(encoding="utf-8"))
LINE = json.loads((FIX / "line_page_krsk.json").read_text(encoding="utf-8"))   # route page state-view, bus 3
HITS = {"stop": 0, "routes": 0}


def page(state: dict) -> str:
    return ('<html><head></head><body><div id="root"></div>'
            f'<script type="application/json" class="state-view">{json.dumps(state, ensure_ascii=False)}</script>'
            '</body></html>')


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): ...

    def _send(self, code, body="", headers=None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/maps/-/short":
            return self._send(302, headers={"Location": "/maps/?masstransit%5BstopId%5D=" + STOP["id"]})
        if u.path == "/maps/-/evil":
            return self._send(302, headers={"Location": "http://evil.example/maps/"})
        if u.path == "/maps/cap":
            return self._send(302, headers={"Location": "/showcaptcha?retpath=x"})
        if u.path == "/showcaptcha":
            return self._send(200, "<html>captcha</html>")
        if u.path == "/maps/forbidden/":
            return self._send(403, "forbidden")
        if u.path == "/maps/home/":
            return self._send(200, page({"config": {}, "stack": []}))
        if u.path == "/maps/" and "text" in q:
            return self._send(200, page(SEARCH))
        if u.path == "/maps/" and q.get("masstransit[stopId]") == [STOP["id"]] or u.path.endswith(f"/stops/{STOP['id']}/"):
            HITS["stop"] += 1
            return self._send(200, page({"config": {"masstransitStop": STOP}}))
        if "/routes/" in u.path:
            HITS["routes"] += 1
            *_, slug, hexuri, _ = u.path.split("/")
            uri = bytes.fromhex(hexuri).decode()
            if u.path.startswith("/maps/62/krasnoyarsk/routes/") and slug == "bus_3" and "line?id=2611970554&" in uri:
                return self._send(200, page(LINE))
            if slug == "bus_38":
                return self._send(200, page({"config": {}, "stack": []}))
        return self._send(404, "nf")


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def make(base, tmp_path, clock=time.time):
    cfg = YandexConfig(dump_dir=str(tmp_path / "dump"), allowed_hosts=("127.0.0.1",), base_url=base,
                       refresh_s=30, blocked_pause_s=900)
    client = httpx.AsyncClient(follow_redirects=True, timeout=5, trust_env=False)   # no system proxy for 127.0.0.1
    return YandexHttpSource(cfg, client=client, clock=clock)


def test_found_stops_on_real_search_result():
    got = found_stops(SEARCH)
    assert "stop__9901344" in got and "stop__9901345" in got
    assert all(sid.startswith("stop__") for sid in got)          # non-stop transit ids are skipped
    title, lat, lon = got["stop__9901344"]
    assert abs(lat - 55.997413) < 1e-5 and abs(lon - 92.795747) < 1e-5 and title


def test_resolve_nearby_get_and_refresh(server, tmp_path):
    async def run():
        t = [1000.0]
        src = make(server, tmp_path, clock=lambda: t[0])
        await src.start()
        try:
            snap, url = await src.resolve(server + "/maps/-/short")       # redirect is followed
            assert snap.stop_id == STOP["id"] and snap.routes
            assert url == f"{server}/maps/{STOP['region']['id']}/{STOP['region']['seoname']}/stops/{STOP['id']}/"
            assert snap.lat and snap.lon

            near = await src.nearby(55.9980, 92.7960)
            assert [s.stop_id for s in near[:2]] == ["stop__9901345", "stop__9901344"]   # sorted by distance
            assert near[0].dist_m < 40 and len(near) <= 6 and all(s.dist_m <= 1500 for s in near)

            n = HITS["stop"]
            await src.get(STOP["id"])                       # fresh in cache -> no request
            assert HITS["stop"] == n
            t[0] += 60
            await src.get(STOP["id"])                       # stale -> refetched
            assert HITS["stop"] == n + 1

            await src.sync({STOP["id"]: url})
            await src.refresh()                             # just fetched -> nothing
            assert HITS["stop"] == n + 1
            t[0] += 31
            await src.refresh()
            assert HITS["stop"] == n + 2 and src.snapshot(STOP["id"]).fetched_at == t[0]
            assert src.problem() is None
        finally:
            await src._client.aclose()

    asyncio.run(run())


def test_errors_and_blocking(server, tmp_path):
    async def run():
        t = [1000.0]
        src = make(server, tmp_path, clock=lambda: t[0])
        await src.start()
        try:
            with pytest.raises(ValueError, match="не остановка"):
                await src.resolve(server + "/maps/home/")
            with pytest.raises(ValueError, match="HTTP 404"):
                await src.resolve(server + "/maps/missing/")
            with pytest.raises(ValueError):
                await src.resolve(server + "/maps/-/evil")          # redirect off Yandex is refused
            with pytest.raises(ValueError):
                await src.resolve("https://example.com/maps/")      # not an allowed host
            with pytest.raises(SourceBlocked):
                await src.resolve(server + "/maps/cap")
            assert "капч" in src.problem()
            n = HITS["stop"]
            with pytest.raises(SourceBlocked):                      # paused: no requests at all
                await src.get(STOP["id"])
            await src.sync({STOP["id"]: src.stop_url(STOP["id"])})
            await src.refresh()
            assert HITS["stop"] == n
            t[0] += 901
            assert (await src.get(STOP["id"])).stop_id == STOP["id"]
            assert src.problem() is None
            with pytest.raises(SourceBlocked):
                await src.resolve(server + "/maps/forbidden/")
            assert "403" in src.problem()
        finally:
            await src._client.aclose()

    asyncio.run(run())


def test_route_page_cached_and_errors_not_hammered(server, tmp_path):
    async def run():
        t = [1000.0]
        src = make(server, tmp_path, clock=lambda: t[0])
        await src.start()
        try:
            snap = await src.get(STOP["id"])
            n = HITS["routes"]
            ths = await src.line(snap.routes["3|bus"], snap.region)       # uri hex-encoded in the URL
            assert {th.thread_id for th in ths} == {"5189668206", "5189728876"} and HITS["routes"] == n + 1
            await src.line(snap.routes["3|bus"], snap.region)
            assert HITS["routes"] == n + 1                                 # cached
            t[0] += 86401
            await src.line(snap.routes["3|bus"], snap.region)
            assert HITS["routes"] == n + 2                                 # expired after line_ttl_s
            with pytest.raises(ValueError, match="нет схемы"):
                await src.line(snap.routes["38|bus"], snap.region)
            with pytest.raises(ValueError, match="HTTP 404"):
                await src.line(snap.routes["63|bus"], snap.region)
            with pytest.raises(ValueError, match="HTTP 404"):
                await src.line(snap.routes["63|bus"], snap.region)       # failure remembered: no new request
            assert HITS["routes"] == n + 4
            with pytest.raises(ValueError, match="город"):
                await src.line(snap.routes["3|bus"], None)
            assert src.problem() is None                                   # route pages don't flag the source
        finally:
            await src._client.aclose()

    asyncio.run(run())


def test_network_error_is_reported(tmp_path):
    async def run():
        src = make("http://127.0.0.1:9", tmp_path)
        await src.start()
        try:
            with pytest.raises((ConnectionError, TimeoutError)):
                await src.get("stop__1")
        finally:
            await src._client.aclose()

    asyncio.run(run())
