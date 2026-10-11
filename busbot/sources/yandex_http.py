"""Yandex Maps stop forecasts over plain HTTP — no browser, ~1 s per stop.

The stop page (/maps/<region>/<city>/stops/stop__N/) is server-rendered: its HTML carries
<script type="application/json" class="state-view"> whose config.masstransitStop is exactly what
getStopInfo returns (the page's own getStopInfo XHR only fires ~30 s later, as a refresh).
Nearest stops: the regular search page for «остановка» around a point; its state-view lists results
with uri ymapsbm1://transit/stop?id=stop__N, title and coordinates.
Route page (/maps/<region>/<city>/routes/<type>_<name>/<hex of the line uri>/): config.masstransitLine holds
every thread's stops in order + the path; it changes rarely and is cached for line_ttl_s.
Only ordinary page loads are made. Captcha / HTTP 403/429 pauses the source; it is never solved or bypassed.
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from ..geo import bearing_deg, haversine_m
from ..models import norm_route
from ..stop_engine import SourceBlocked
from ..stopinfo import LineThread, NearStop, RouteForecast, StopSnapshot, parse_line, parse_stop_info
from .yandex import BLOCK_STATUSES, YandexConfig, check_url, is_captcha

log = logging.getLogger("yandex_http")

STATE_RE = re.compile(r'<script[^>]*class="state-view"[^>]*>(.*?)</script>', re.S)
TRANSIT_STOP_RE = re.compile(r"transit/stop\?id=(stop__\d+)")
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36")


def _host_ok(url: str, hosts: tuple[str, ...]) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == h or host.endswith("." + h) for h in hosts)


def page_state(html: str) -> dict:
    m = STATE_RE.search(html)
    if not m:
        raise ValueError("на странице нет данных карты (state-view)")
    return json.loads(m.group(1))


def stop_payload(state: dict) -> dict | None:
    cfg = state.get("config")
    p = cfg.get("masstransitStop") if isinstance(cfg, dict) else None
    return p if isinstance(p, dict) and p.get("transports") is not None else None


def found_stops(state: Any) -> dict[str, tuple[str, float, float]]:
    """stop_id -> (title, lat, lon) for every search result that is a transit stop."""
    out: dict[str, tuple[str, float, float]] = {}

    def walk(o: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(o, dict):
            m = TRANSIT_STOP_RE.search(str(o.get("uri") or ""))
            c = o.get("coordinates")
            if m and isinstance(c, list) and len(c) == 2 and o.get("title"):
                try:
                    out.setdefault(m.group(1), (str(o["title"]), float(c[1]), float(c[0])))
                except (TypeError, ValueError):
                    pass
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)

    walk(state)
    return out


class YandexHttpSource:
    def __init__(self, cfg: YandexConfig, client: httpx.AsyncClient | None = None, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self._client = client
        self._own_client = client is None
        self._wanted: dict[str, str] = {}
        self._snaps: dict[str, StopSnapshot] = {}
        self._urls: dict[str, str] = {}            # stop_id -> canonical page URL
        self._last_try: dict[str, float] = {}
        self._blocked_until = 0.0
        self._problem: str | None = None
        self._gate = asyncio.Semaphore(2)          # never more than 2 requests to Yandex at once
        self._lines: dict[str, tuple[float, list[LineThread]]] = {}
        self._line_err: dict[str, tuple[float, str]] = {}   # failed route pages are not retried for 10 min

    # ---------- lifecycle
    async def start(self) -> None:
        Path(self.cfg.dump_dir).mkdir(parents=True, exist_ok=True)
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=min(self.cfg.page_timeout_s, 20),
                headers={"User-Agent": self.cfg.user_agent or DEFAULT_UA,
                         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                         "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"})

    async def close(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- helpers
    def _block(self, why: str) -> None:
        self._blocked_until = self.clock() + self.cfg.blocked_pause_s
        self._problem = f"{why} — запросы приостановлены на {int(self.cfg.blocked_pause_s // 60)} мин"
        log.warning(self._problem)

    def problem(self) -> str | None:
        if self.clock() < self._blocked_until:
            return self._problem or "источник приостановлен"
        return self._problem

    def _dump(self, name: str, data: Any) -> str:
        p = Path(self.cfg.dump_dir) / f"{name}.json"
        try:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        return str(p)

    async def _get(self, url: str) -> dict:
        if self.clock() < self._blocked_until:
            raise SourceBlocked(self.problem())
        async with self._gate:
            try:
                for _ in range(6):   # redirects by hand: every hop must stay on an allowed host
                    r = await self._client.get(url, follow_redirects=False)
                    if not r.is_redirect:
                        break
                    url = str(r.url.join(r.headers["location"]))
                    if not _host_ok(url, self.cfg.allowed_hosts):
                        raise ValueError("ссылка уводит с Яндекс.Карт")
                else:
                    raise ValueError("слишком много перенаправлений")
            except httpx.TimeoutException:
                raise TimeoutError("Яндекс не ответил вовремя") from None
            except httpx.HTTPError as e:
                raise ConnectionError(f"сеть: {e!r}") from None
        final = str(r.url)
        if r.status_code in BLOCK_STATUSES:
            self._block(f"Яндекс отклонил запрос (HTTP {r.status_code})")
            raise SourceBlocked(self._problem)
        if is_captcha(final):
            self._block("Яндекс показал капчу")
            raise SourceBlocked(self._problem)
        check_url(final, self.cfg.allowed_hosts)   # a redirect must not lead off Yandex Maps
        if r.status_code >= 400:
            raise ValueError(f"страница вернула HTTP {r.status_code}, проверь ссылку")
        return page_state(r.text)

    def stop_url(self, stop_id: str) -> str:
        return self._urls.get(stop_id) or f"{self.cfg.base_url}/maps/?masstransit%5BstopId%5D={stop_id}"

    async def _fetch(self, url: str, expect_id: str | None = None) -> StopSnapshot:
        state = await self._get(url)
        payload = stop_payload(state)
        if payload is None:
            raise ValueError("по ссылке открывается не остановка — нужна страница остановки с табло прибытия")
        try:
            snap = parse_stop_info({"data": payload}, self.clock())
        except ValueError as e:
            raise ValueError(f"{e}; сырой ответ: {self._dump(expect_id or 'unknown', payload)}") from None
        if not snap.stop_id:
            raise ValueError("в ответе нет id остановки")
        if expect_id and snap.stop_id != expect_id:
            raise ValueError(f"Яндекс вернул другую остановку ({snap.stop_id} вместо {expect_id})")
        reg = payload.get("region") if isinstance(payload.get("region"), dict) else {}
        if reg.get("id") and reg.get("seoname"):
            self._urls[snap.stop_id] = f"{self.cfg.base_url}/maps/{reg['id']}/{reg['seoname']}/stops/{snap.stop_id}/"
        self._snaps[snap.stop_id] = snap
        if self.clock() >= self._blocked_until:
            self._problem = None
        return snap

    # ---------- user actions (fast path, ~1 s)
    async def resolve(self, url: str) -> tuple[StopSnapshot, str]:
        """Link pasted by a user (short /maps/-/… links are redirects and are followed)."""
        check_url(url, self.cfg.allowed_hosts)
        snap = await self._fetch(url)
        return snap, self.stop_url(snap.stop_id)

    async def get(self, stop_id: str, url: str | None = None, max_age: float = 20.0) -> StopSnapshot:
        """Forecast for a known stop: cached if fresher than max_age, else fetched now."""
        snap = self._snaps.get(stop_id)
        if snap is not None and self.clock() - snap.fetched_at <= max_age:
            return snap
        if url:
            self._urls.setdefault(stop_id, url)
        return await self._fetch(self.stop_url(stop_id), expect_id=stop_id)

    async def nearby(self, lat: float, lon: float, limit: int = 6) -> list[NearStop]:
        url = f"{self.cfg.base_url}/maps/?ll={lon:.6f}%2C{lat:.6f}&z=17&text={quote('остановка')}"
        found = found_stops(await self._get(url))
        res = [NearStop(sid, title, slat, slon, haversine_m(lat, lon, slat, slon), bearing_deg(lat, lon, slat, slon))
               for sid, (title, slat, slon) in found.items()]
        res = sorted((s for s in res if s.dist_m <= self.cfg.search_radius_m), key=lambda s: s.dist_m)
        return res[:limit]

    async def line(self, rf: RouteForecast, region: str | None) -> list[LineThread]:
        """All threads (directions) of a route, from its route page. Cached for line_ttl_s."""
        key = rf.line_id or rf.uri
        if not key or not region:
            raise ValueError("Яндекс не сообщил id маршрута или город")
        now = self.clock()
        hit = self._lines.get(key)
        if hit is not None and now - hit[0] < self.cfg.line_ttl_s:
            return hit[1]
        err = self._line_err.get(key)
        if err is not None and now - err[0] < 600:
            raise ValueError(err[1])
        uri = rf.uri or f"ymapsbm1://transit/line?id={rf.line_id}"
        slug = quote(f"{rf.type or 'bus'}_{rf.seoname or norm_route(rf.name)}", safe="")
        url = f"{self.cfg.base_url}/maps/{quote(region)}/routes/{slug}/{uri.encode().hex()}/"
        try:
            state = await self._get(url)
            cfg = state.get("config")
            ml = cfg.get("masstransitLine") if isinstance(cfg, dict) else None
            if not isinstance(ml, dict):
                raise ValueError("на странице маршрута нет схемы (masstransitLine)")
            threads = parse_line(ml)
            if not threads:
                raise ValueError(f"схема маршрута не распознана; сырой ответ: {self._dump('line_' + key, ml)}")
        except ValueError as e:
            self._line_err[key] = (now, str(e))
            raise
        self._lines[key] = (now, threads)
        self._line_err.pop(key, None)
        return threads

    # ---------- continuous watching (StopEngine)
    async def sync(self, wanted: dict[str, str]) -> None:
        self._wanted = dict(wanted)
        for sid, url in wanted.items():
            self._urls.setdefault(sid, url)

    async def refresh(self) -> None:
        """Re-fetch stops whose data is older than refresh_s; one attempt per stop per refresh_s."""
        for sid in list(self._wanted):
            now = self.clock()
            if now < self._blocked_until:
                return
            snap = self._snaps.get(sid)
            if snap is not None and now - snap.fetched_at < self.cfg.refresh_s:
                continue
            if now - self._last_try.get(sid, 0.0) < self.cfg.refresh_s:
                continue
            self._last_try[sid] = now
            try:
                await self._fetch(self.stop_url(sid), expect_id=sid)
            except SourceBlocked:
                return
            except Exception as e:
                self._problem = f"{sid}: {e}"[:300]
                log.warning("refresh %s: %r", sid, e)

    def snapshot(self, stop_id: str) -> StopSnapshot | None:
        return self._snaps.get(stop_id)
