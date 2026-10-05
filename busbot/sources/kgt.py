"""Adapter for the Krasnoyarsk feed (mu-kgt.ru).

The endpoint and JSON field names are NOT hard-coded: find the XHR request that the live map
page makes (DevTools -> Network -> Fetch/XHR) and put its URL + field names into .env.
"""
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..models import Vehicle, norm_route


@dataclass(frozen=True)
class KgtConfig:
    url: str = ""
    method: str = "GET"
    body: dict | None = None
    headers: dict = field(default_factory=dict)
    list_path: str = ""          # dotted path to the list of vehicles, '' = payload itself
    f_id: str = "id"
    f_route: str = "route"
    f_lat: str = "lat"
    f_lon: str = "lon"
    f_heading: str = ""          # '' = not provided, heading is derived from movement
    f_speed: str = ""
    f_ts: str = ""               # unix seconds or ms; '' = use fetch time
    coord_scale: float = 1.0     # e.g. 1e-6 if coordinates come as integer micro-degrees


def _get(d: dict, path: str) -> Any:
    cur: Any = d
    for p in path.split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur


def _num(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(str(x).replace(",", "."))
    except ValueError:
        return None


def parse_payload(payload: Any, cfg: KgtConfig, now: float) -> list[Vehicle]:
    items = _get(payload, cfg.list_path) if cfg.list_path and isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError(f"expected a list at '{cfg.list_path or '<root>'}', got {type(items).__name__}")
    out: list[Vehicle] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        lat, lon = _num(it.get(cfg.f_lat)), _num(it.get(cfg.f_lon))
        route, vid = it.get(cfg.f_route), it.get(cfg.f_id)
        if lat is None or lon is None or route in (None, "") or vid in (None, ""):
            continue
        lat, lon = lat * cfg.coord_scale, lon * cfg.coord_scale
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            continue
        ts = _num(it.get(cfg.f_ts)) if cfg.f_ts else None
        if ts is not None and ts > 1e12:
            ts /= 1000
        out.append(Vehicle(
            vehicle_id=str(vid),
            route=norm_route(str(route)),
            lat=lat, lon=lon,
            heading=_num(it.get(cfg.f_heading)) if cfg.f_heading else None,
            speed=_num(it.get(cfg.f_speed)) if cfg.f_speed else None,
            ts=ts if ts is not None else now,
        ))
    return out


class KgtSource:
    def __init__(self, cfg: KgtConfig, client: httpx.AsyncClient):
        if not cfg.url:
            raise RuntimeError("KGT_URL is empty: set it in .env (see README, 'Подключение KGT')")
        self.cfg, self.client = cfg, client

    async def fetch(self) -> list[Vehicle]:
        c = self.cfg
        r = await self.client.request(c.method, c.url, json=c.body, headers=c.headers or None)
        r.raise_for_status()
        return parse_payload(r.json(), c, time.time())
