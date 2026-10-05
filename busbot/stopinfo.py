"""Stop forecast model + tolerant parser for Yandex masstransit getStopInfo JSON.

Known formats:
  2019:  data.properties.StopMetaData.Transport[].BriefSchedule.Events[].Estimated.value (int unix s)
  2019+: data.properties.StopMetaData.Transport[].threads[].BriefSchedule.Events[].estimated.value ("str")
  2026:  data.transports[].threads[].BriefSchedule.{Events[], Frequencies[]}; data.currentTime (ms),
         data.coordinates [lon, lat]  (verified on a live Krasnoyarsk response)
Keys are matched case-insensitively; timestamps may be int/str, seconds/milliseconds.
"""
import re
from dataclasses import dataclass, field
from typing import Any

from .models import norm_route

STOP_ID_RE = re.compile(r"stop__\d+")
TYPE_RU = {"bus": "Автобус", "trolleybus": "Троллейбус", "tramway": "Трамвай", "tram": "Трамвай",
           "minibus": "Маршрутка", "suburban": "Электричка", "underground": "Метро"}


@dataclass(slots=True, frozen=True)
class Arrival:
    ts: float                 # unix seconds (server clock)
    live: bool                # True = GPS estimate, False = timetable
    vehicle_id: str | None


@dataclass(slots=True)
class RouteForecast:
    key: str                  # "<norm name>|<type>"
    name: str
    type: str
    arrivals: list[Arrival] = field(default_factory=list)
    frequency: str | None = None
    line_id: str | None = None

    @property
    def title(self) -> str:
        return f"{TYPE_RU.get(self.type, self.type or 'Маршрут')} {self.name}"


@dataclass(slots=True)
class StopSnapshot:
    stop_id: str | None
    name: str
    fetched_at: float          # local clock
    server_time: float | None  # server clock at fetch, if provided
    routes: dict[str, RouteForecast] = field(default_factory=dict)
    lat: float | None = None
    lon: float | None = None

    def now_server(self, now_local: float) -> float:
        base = self.server_time if self.server_time is not None else self.fetched_at
        return base + (now_local - self.fetched_at)

    def eta_min(self, a: Arrival, now_local: float) -> float:
        return (a.ts - self.now_server(now_local)) / 60.0


@dataclass(slots=True, frozen=True)
class NearStop:
    stop_id: str
    name: str
    lat: float
    lon: float
    dist_m: float
    bearing: float            # from the user's point to the stop, degrees


def route_key(name: str, type_: str) -> str:
    return f"{norm_route(name)}|{type_ or ''}"


def extract_stop_id(text: str) -> str | None:
    m = STOP_ID_RE.search(text or "")
    return m.group(0) if m else None


def _ci(d: Any, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = low.get(k.lower())
        if v is not None:
            return v
    return None


def _find(obj: Any, key: str, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(obj, dict):
        v = _ci(obj, key)
        if v is not None:
            return v
        for x in obj.values():
            r = _find(x, key, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = _find(x, key, depth + 1)
            if r is not None:
                return r
    return None


def _ts(x: Any) -> float | None:
    if isinstance(x, dict):
        x = _ci(x, "value")
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(str(x).strip())
    except ValueError:
        return None
    if v > 1e12:          # milliseconds
        v /= 1000
    return v if v > 1e9 else None


def _events(brief: Any) -> tuple[list[Arrival], str | None]:
    out: list[Arrival] = []
    if not isinstance(brief, dict):
        return out, None
    for e in _ci(brief, "Events") or []:
        if not isinstance(e, dict):
            continue
        est, sch = _ts(_ci(e, "Estimated")), _ts(_ci(e, "Scheduled"))
        vid = _ci(e, "vehicleId")
        if est is not None:
            out.append(Arrival(est, True, str(vid) if vid else None))
        elif sch is not None:
            out.append(Arrival(sch, False, str(vid) if vid else None))
    freq = _ci(brief, "Frequency", "Frequencies")
    if isinstance(freq, list):
        freq = next((f for f in freq if isinstance(f, dict)), None)
    text = _ci(freq, "text") if isinstance(freq, dict) else None
    return out, (str(text).replace("\xa0", " ") if text else None)


def _container(obj: Any, depth: int = 0) -> dict | None:
    """First dict (depth-first) holding the list of routes: 'Transport' or 'transports'."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        if isinstance(_ci(obj, "Transport", "transports"), list):
            return obj
        for x in obj.values():
            r = _container(x, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = _container(x, depth + 1)
            if r is not None:
                return r
    return None


def parse_stop_info(payload: Any, fetched_at: float) -> StopSnapshot:
    if isinstance(payload, dict) and _ci(payload, "error") and not _ci(payload, "data"):
        raise ValueError(f"yandex error: {_ci(payload, 'error')}")
    meta = _container(payload)
    if meta is None:
        raise ValueError("в ответе нет списка маршрутов (Transport/transports)")
    transports = _ci(meta, "Transport", "transports")
    props = _find(payload, "properties") if isinstance(payload, dict) else None
    if not isinstance(props, dict):
        props = None
    server_time = _ts(_ci(meta, "currentTime")) or (_ts(_ci(props, "currentTime")) if props else None)
    snap = StopSnapshot(
        stop_id=str(_ci(meta, "id")) if _ci(meta, "id") else None,
        name=str(_ci(meta, "name") or _ci(props, "name") or "Остановка"),
        fetched_at=fetched_at,
        server_time=server_time,
    )
    coords = _ci(meta, "coordinates")
    if not coords:
        geom = _find(payload, "geometry")
        coords = _ci(geom, "coordinates") if isinstance(geom, dict) else None
    if isinstance(coords, list) and len(coords) == 2:
        try:
            snap.lon, snap.lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            pass
    for t in transports:
        if not isinstance(t, dict) or not _ci(t, "name"):
            continue
        name, type_ = str(_ci(t, "name")), str(_ci(t, "type") or "")
        key = route_key(name, type_)
        rf = snap.routes.get(key) or RouteForecast(key, name, type_)
        rf.line_id = rf.line_id or (str(_ci(t, "lineId")) if _ci(t, "lineId") else None)
        briefs = [_ci(t, "BriefSchedule")] + [_ci(th, "BriefSchedule") for th in (_ci(t, "threads") or [])
                                              if isinstance(th, dict)]
        seen = {(a.ts, a.vehicle_id) for a in rf.arrivals}
        for b in briefs:
            evs, freq = _events(b)
            rf.frequency = rf.frequency or freq
            for a in evs:
                if (a.ts, a.vehicle_id) not in seen:
                    seen.add((a.ts, a.vehicle_id))
                    rf.arrivals.append(a)
        rf.arrivals.sort(key=lambda a: a.ts)
        snap.routes[key] = rf
    return snap
