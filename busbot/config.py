import json
import os
from dataclasses import dataclass

from .sources.kgt import KgtConfig
from .sources.yandex import YandexConfig
from .upstream import UpstreamConfig


def _ids(s: str) -> frozenset[int]:
    return frozenset(int(x) for x in s.replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Config:
    bot_token: str
    telegram_proxy: str
    db_path: str
    source: str
    poll_interval: float
    stale_after: float
    allowed_users: frozenset[int]
    admin_id: int | None
    kgt: KgtConfig
    stop_source: str
    stop_interval: float
    stop_stale: float
    use_scheduled: bool
    yandex: YandexConfig
    upstream: UpstreamConfig


def _value(v: str) -> str:
    """`KEY=   # comment` is read by older python-dotenv as the value '# comment': treat it as empty."""
    v = v.strip()
    return "" if v.startswith("#") else v


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def load() -> Config:
    e = os.environ
    token = e.get("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")
    body = e.get("KGT_BODY", "")
    kgt = KgtConfig(
        url=e.get("KGT_URL", ""),
        method=e.get("KGT_METHOD", "GET").upper(),
        body=json.loads(body) if body else None,
        headers=json.loads(e.get("KGT_HEADERS", "") or "{}"),
        list_path=e.get("KGT_LIST_PATH", ""),
        f_id=e.get("KGT_F_ID", "id"),
        f_route=e.get("KGT_F_ROUTE", "route"),
        f_lat=e.get("KGT_F_LAT", "lat"),
        f_lon=e.get("KGT_F_LON", "lon"),
        f_heading=e.get("KGT_F_HEADING", ""),
        f_speed=e.get("KGT_F_SPEED", ""),
        f_ts=e.get("KGT_F_TS", ""),
        coord_scale=float(e.get("KGT_COORD_SCALE", "1")),
    )
    admin = _value(e.get("ADMIN_ID", ""))
    return Config(
        bot_token=token,
        telegram_proxy=_value(e.get("TELEGRAM_PROXY", "")),
        db_path=e.get("DB_PATH", "data/bot.sqlite3"),
        source=e.get("SOURCE", "mock").lower(),
        poll_interval=float(e.get("POLL_INTERVAL", "20")),
        stale_after=float(e.get("STALE_AFTER", "120")),
        allowed_users=_ids(_value(e.get("ALLOWED_USERS", ""))),
        admin_id=int(admin) if admin else None,
        kgt=kgt,
        stop_source=e.get("STOP_SOURCE", "none").lower(),
        stop_interval=float(e.get("STOP_INTERVAL", "10")),
        stop_stale=float(e.get("STOP_STALE", "180")),
        use_scheduled=_bool(e.get("STOP_USE_SCHEDULED", "0")),
        yandex=load_yandex(),
        upstream=load_upstream(),
    )


def load_upstream() -> UpstreamConfig:
    e = os.environ
    return UpstreamConfig(
        enabled=_bool(e.get("UPSTREAM", "1")),
        horizon_min=float(e.get("YANDEX_HORIZON", "15")),
        speed_kmh=float(e.get("UPSTREAM_SPEED", "18")),
        max_anchors=int(e.get("UPSTREAM_MAX_STOPS", "4")),
        positions=_bool(e.get("UPSTREAM_KGT", "1")),
    )


def load_yandex() -> YandexConfig:
    e = os.environ
    return YandexConfig(
        chrome_path=e.get("CHROME_PATH", ""),
        headless=_bool(e.get("YANDEX_HEADLESS", "1")),
        profile_dir=e.get("YANDEX_PROFILE_DIR", "data/chromium"),
        dump_dir=e.get("YANDEX_DUMP_DIR", "data/yandex_dump"),
        pattern=e.get("YANDEX_STOP_PATTERN", "masstransit/getStopInfo"),
        refresh_s=float(e.get("YANDEX_REFRESH", "60")),
        page_timeout_s=float(e.get("YANDEX_PAGE_TIMEOUT", "45")),
        max_stops=int(e.get("YANDEX_MAX_STOPS", "6")),
        block_media=_bool(e.get("YANDEX_BLOCK_MEDIA", "1")),
        timezone=e.get("YANDEX_TZ", "Asia/Krasnoyarsk"),
        user_agent=e.get("YANDEX_USER_AGENT", ""),
        blocked_pause_s=float(e.get("YANDEX_BLOCKED_PAUSE", "900")),
        line_ttl_s=float(e.get("YANDEX_LINE_TTL", "86400")),
    )
