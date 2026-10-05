"""Fake stop forecasts for testing the /stop flow without Yandex.
Routes at every stop: bus 99, trolleybus 7, bus 135; headway 10 min, phase-shifted.
nearby() returns three fake stops ~60/150/400 m north-east of the point."""
import math
import time

from ..geo import bearing_deg, haversine_m
from ..stopinfo import Arrival, NearStop, RouteForecast, StopSnapshot, extract_stop_id, route_key

HEADWAY = 600
ROUTES = [("99", "bus", 0), ("7", "trolleybus", 240), ("135", "bus", 420)]
NEAR = [("stop__9001", "Тестовая остановка 9001", 0.0004, 0.0006), ("stop__9002", "Тестовая остановка 9002", 0.001, 0.0015),
        ("stop__9003", "Тестовая остановка 9003", 0.0028, 0.004)]


class MockStopSource:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._wanted: dict[str, str] = {}

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def _snap(self, sid: str) -> StopSnapshot:
        now = self.clock()
        snap = StopSnapshot(sid, f"Тестовая остановка {sid[-4:]}", now, now)
        for name, typ, phase in ROUTES:
            rf = RouteForecast(route_key(name, typ), name, typ, frequency="10 мин")
            n0 = math.floor((now - phase) / HEADWAY) + 1
            rf.arrivals = [Arrival(n * HEADWAY + phase, True, f"{name}-{n}") for n in range(n0, n0 + 3)]
            snap.routes[rf.key] = rf
        return snap

    def stop_url(self, stop_id: str) -> str:
        return f"https://yandex.ru/maps/?masstransit%5BstopId%5D={stop_id}"

    async def resolve(self, url: str) -> tuple[StopSnapshot, str]:
        sid = extract_stop_id(url) or "stop__0000"
        return self._snap(sid), url

    async def get(self, stop_id: str, url: str | None = None, max_age: float = 20.0) -> StopSnapshot:
        return self._snap(stop_id)

    async def nearby(self, lat: float, lon: float, limit: int = 6) -> list[NearStop]:
        out = []
        for sid, name, dlat, dlon in NEAR:
            slat, slon = lat + dlat, lon + dlon
            out.append(NearStop(sid, name, slat, slon, haversine_m(lat, lon, slat, slon), bearing_deg(lat, lon, slat, slon)))
        return out[:limit]

    async def sync(self, wanted: dict[str, str]) -> None:
        self._wanted = dict(wanted)

    async def refresh(self) -> None: ...

    def snapshot(self, stop_id: str) -> StopSnapshot | None:
        return self._snap(stop_id) if stop_id in self._wanted else None

    def problem(self) -> str | None:
        return None
