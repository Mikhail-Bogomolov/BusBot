"""Yandex Maps stop-forecast source via a real (headless) Chromium.

How it works: one browser tab per watched stop is kept on the Yandex Maps stop page; the page itself
requests `.../masstransit/getStopInfo`, and we read that JSON response (no replaying of private API calls).
If no fresh response arrives within `refresh_s`, the tab is reloaded. Navigations are serialized
(one at a time, at most one per tick). A captcha page pauses the source; it is never solved or bypassed.
"""
import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..stop_engine import SourceBlocked
from ..stopinfo import StopSnapshot, extract_stop_id, parse_stop_info

log = logging.getLogger("yandex")

YANDEX_HOSTS = ("yandex.ru", "yandex.com", "yandex.eu", "yandex.kz", "yandex.by", "yandex.uz")


@dataclass(frozen=True)
class YandexConfig:
    chrome_path: str = ""            # '' = Playwright's bundled Chromium
    headless: bool = True
    profile_dir: str = "data/chromium"
    dump_dir: str = "data/yandex_dump"
    pattern: str = "masstransit/getStopInfo"
    refresh_s: float = 60.0
    page_timeout_s: float = 45.0
    max_stops: int = 6
    block_media: bool = True
    timezone: str = "Asia/Krasnoyarsk"
    user_agent: str = ""
    blocked_pause_s: float = 900.0
    allowed_hosts: tuple[str, ...] = YANDEX_HOSTS
    base_url: str = "https://yandex.ru"      # HTTP source: where stop/search pages are requested
    search_radius_m: float = 1500.0          # HTTP source: nearest stops farther than this are dropped
    line_ttl_s: float = 86400.0              # HTTP source: route pages (stop order + path) are cached this long


@dataclass
class _Watch:
    stop_id: str
    url: str
    page: Any = None
    last_data_at: float = 0.0
    last_nav_at: float = 0.0
    navs: int = 0


def check_url(url: str, hosts: tuple[str, ...]) -> None:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not any(host == h or host.endswith("." + h) for h in hosts):
        raise ValueError("нужна ссылка на Яндекс.Карты")
    if not u.path.startswith("/maps"):
        raise ValueError("ссылка должна вести на Яндекс.Карты (/maps/...)")


BLOCK_STATUSES = (403, 429)


def is_captcha(url: str) -> bool:
    u = url.lower()
    return "showcaptcha" in u or "/captcha" in u


class YandexStopSource:
    def __init__(self, cfg: YandexConfig, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self._pw = None
        self._ctx = None
        self._lock = asyncio.Lock()
        self._watches: dict[str, _Watch] = {}
        self._snaps: dict[str, StopSnapshot] = {}
        self._blocked_until = 0.0
        self._problem: str | None = None
        self._tasks: set[asyncio.Task] = set()
        self.seen: deque[str] = deque(maxlen=80)   # recent API-ish response URLs, for diagnostics

    # ---------- lifecycle
    async def start(self) -> None:
        from playwright.async_api import async_playwright
        prof = Path(self.cfg.profile_dir)
        prof.mkdir(parents=True, exist_ok=True)
        Path(self.cfg.dump_dir).mkdir(parents=True, exist_ok=True)
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):   # stale after container restart
            try:
                (prof / lock).unlink()
            except FileNotFoundError:
                pass
        self._pw = await async_playwright().start()
        try:
            self._ctx = await self._launch(self.cfg.user_agent or None)
            if not self.cfg.user_agent:
                ua = await self._user_agent()
                if "Headless" in ua:   # report a regular Chrome UA of the same version
                    await self._ctx.close()
                    self._ctx = await self._launch(ua.replace("HeadlessChrome", "Chrome"))
            if self.cfg.block_media:
                await self._ctx.route("**/*", self._route)
        except Exception:
            await self._pw.stop()
            self._pw = self._ctx = None
            raise
        log.info("browser ready")

    async def _launch(self, user_agent: str | None):
        kw: dict[str, Any] = dict(headless=self.cfg.headless, locale="ru-RU", timezone_id=self.cfg.timezone,
                                  viewport={"width": 1366, "height": 850}, args=["--disable-dev-shm-usage"])
        if self.cfg.chrome_path:
            kw["executable_path"] = self.cfg.chrome_path
        if user_agent:
            kw["user_agent"] = user_agent
        return await self._pw.chromium.launch_persistent_context(self.cfg.profile_dir, **kw)

    async def _user_agent(self) -> str:
        page = await self._ctx.new_page()
        try:
            return await page.evaluate("navigator.userAgent")
        finally:
            await page.close()

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()
            self._pw = self._ctx = None

    @staticmethod
    async def _route(route) -> None:
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    # ---------- helpers
    def _note(self, resp) -> None:
        u = resp.url
        if "/maps/api/" in u:
            self.seen.append(f"{resp.status} {u[:200]}")

    def _dump(self, name: str, data: Any) -> str:
        p = Path(self.cfg.dump_dir) / f"{name}.json"
        try:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        return str(p)

    def _block(self, why: str = "Яндекс показал капчу") -> None:
        mins = int(self.cfg.blocked_pause_s // 60)
        self._blocked_until = self.clock() + self.cfg.blocked_pause_s
        self._problem = f"{why} — опрос приостановлен на {mins} мин"
        log.warning(self._problem)

    def problem(self) -> str | None:
        if self.clock() < self._blocked_until:
            return self._problem or "источник приостановлен"
        return self._problem

    # ---------- one-shot load (used when a user adds a stop)
    async def resolve(self, url: str) -> tuple[StopSnapshot, str]:
        check_url(url, self.cfg.allowed_hosts)
        if self.clock() < self._blocked_until:
            raise SourceBlocked(self.problem())
        async with self._lock:
            page = await self._ctx.new_page()
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            extra: list = []   # other masstransit API responses (vehicles etc.), saved for diagnostics

            def on_resp(resp):
                self._note(resp)
                if self.cfg.pattern in resp.url:
                    if not fut.done():
                        fut.set_result(resp)
                elif "/maps/api/masstransit/" in resp.url:
                    extra.append(resp)

            page.on("response", on_resp)
            try:
                main = None
                try:
                    main = await page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.page_timeout_s * 1000)
                except Exception as e:   # slow page: the XHR may still arrive
                    log.info("goto: %r", e)
                if main is not None and main.status in BLOCK_STATUSES:
                    self._block(f"Яндекс отклонил запрос (HTTP {main.status})")
                    raise SourceBlocked(self._problem)
                if main is not None and main.status >= 400:
                    raise ValueError(f"страница вернула HTTP {main.status}, проверь ссылку")
                if is_captcha(page.url):
                    self._block()
                    raise SourceBlocked(self._problem)
                try:
                    resp = await asyncio.wait_for(fut, timeout=self.cfg.page_timeout_s)
                except asyncio.TimeoutError:
                    if is_captcha(page.url):
                        self._block()
                        raise SourceBlocked(self._problem)
                    raise TimeoutError("страница не запросила прогноз по остановке") from None
                data = await resp.json()
                final_url = page.url
                extras = {}
                for r in extra:
                    method = urlparse(r.url).path.rsplit("/", 1)[-1]
                    try:
                        extras[method] = await r.json()
                    except Exception:
                        pass
            finally:
                await page.close()
        now = self.clock()
        sid_hint = extract_stop_id(final_url) or extract_stop_id(url) or "unknown"
        path = self._dump(sid_hint, data)
        for method, body in extras.items():
            self._dump(f"{sid_hint}_{method}", body)
        try:
            snap = parse_stop_info(data, now)
        except ValueError as e:
            raise ValueError(f"{e}; сырой ответ: {path}") from None
        snap.stop_id = snap.stop_id or sid_hint
        if snap.stop_id == "unknown":
            raise ValueError("в ответе нет id остановки")
        self._snaps[snap.stop_id] = snap
        watch_url = final_url if extract_stop_id(final_url) else url
        return snap, watch_url

    # ---------- continuous watching
    async def sync(self, wanted: dict[str, str]) -> None:
        for sid in list(self._watches):
            if sid not in wanted:
                w = self._watches.pop(sid)
                if w.page is not None and not w.page.is_closed():
                    await w.page.close()
        over = 0
        for sid, url in wanted.items():
            if sid in self._watches:
                continue
            if len(self._watches) >= self.cfg.max_stops:
                over += 1
                continue
            self._watches[sid] = _Watch(sid, url)
        if over:
            self._problem = f"превышен лимит YANDEX_MAX_STOPS={self.cfg.max_stops}, не отслеживается: {over}"

    def _on_watch_response(self, w: _Watch, resp) -> None:
        self._note(resp)
        if self.cfg.pattern in resp.url:
            t = asyncio.create_task(self._handle_watch(w, resp))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _handle_watch(self, w: _Watch, resp) -> None:
        try:
            data = await resp.json()
        except Exception as e:
            self._problem = f"getStopInfo вернул не JSON (HTTP {resp.status}): {e!r}"[:300]
            return
        now = self.clock()
        path = self._dump(w.stop_id, data)
        try:
            snap = parse_stop_info(data, now)
        except ValueError as e:
            self._problem = f"формат ответа не распознан: {e}; сырой ответ: {path}"
            return
        if snap.stop_id and snap.stop_id != w.stop_id:
            return   # the map fetched some other stop
        snap.stop_id = w.stop_id
        self._snaps[w.stop_id] = snap
        w.last_data_at = now
        if self.clock() >= self._blocked_until:
            self._problem = None

    async def _navigate(self, w: _Watch) -> None:
        async with self._lock:
            if w.page is None or w.page.is_closed() or w.navs >= 100:   # recycle tabs to cap memory
                if w.page is not None and not w.page.is_closed():
                    await w.page.close()
                w.page = await self._ctx.new_page()
                w.page.on("response", lambda r, w=w: self._on_watch_response(w, r))
                w.navs = 0
            w.last_nav_at = self.clock()
            w.navs += 1
            main = None
            try:
                main = await w.page.goto(w.url, wait_until="domcontentloaded", timeout=self.cfg.page_timeout_s * 1000)
            except Exception as e:
                self._problem = f"ошибка загрузки страницы: {e!r}"[:300]
            blocked = main is not None and main.status in BLOCK_STATUSES
            if blocked or is_captcha(w.page.url):
                self._block(f"Яндекс отклонил запрос (HTTP {main.status})" if blocked else "Яндекс показал капчу")
                for x in self._watches.values():
                    if x.page is not None and not x.page.is_closed():
                        await x.page.close()
                    x.page = None
                raise SourceBlocked(self._problem)

    async def refresh(self) -> None:
        now = self.clock()
        if now < self._blocked_until:
            return
        for w in list(self._watches.values()):
            stale = now - w.last_data_at > self.cfg.refresh_s
            if w.page is None or (stale and now - w.last_nav_at > self.cfg.refresh_s):
                try:
                    await self._navigate(w)
                except SourceBlocked:
                    pass
                return   # at most one navigation per tick

    def snapshot(self, stop_id: str) -> StopSnapshot | None:
        return self._snaps.get(stop_id)

    async def line(self, rf, region: str | None) -> list:
        raise ValueError("схемы маршрутов загружаются только при STOP_SOURCE=yandex")

    async def get(self, stop_id: str, url: str | None = None, max_age: float = 60.0) -> StopSnapshot:
        snap = self._snaps.get(stop_id)
        if snap is not None and self.clock() - snap.fetched_at <= max_age:
            return snap
        if not url:
            raise ValueError("нет ссылки на остановку")
        snap, _ = await self.resolve(url)
        return snap
