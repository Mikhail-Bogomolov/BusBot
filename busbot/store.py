from dataclasses import replace

from .geo import bearing_deg, haversine_m
from .models import Vehicle

MIN_MOVE_M = 10.0


class VehicleStore:
    """In-memory latest positions. Derives heading from movement if the source has none."""

    def __init__(self) -> None:
        self._v: dict[str, Vehicle] = {}

    def update(self, vehicles: list[Vehicle]) -> None:
        for v in vehicles:
            prev = self._v.get(v.vehicle_id)
            if v.heading is None and prev is not None:
                if haversine_m(prev.lat, prev.lon, v.lat, v.lon) >= MIN_MOVE_M:
                    v = replace(v, heading=bearing_deg(prev.lat, prev.lon, v.lat, v.lon))
                else:
                    v = replace(v, heading=prev.heading)  # standing: keep last known
            self._v[v.vehicle_id] = v
        # drop anything older than an hour
        if vehicles:
            newest = max(v.ts for v in vehicles)
            for k in [k for k, x in self._v.items() if newest - x.ts > 3600]:
                del self._v[k]

    def fresh(self, now: float, max_age: float) -> list[Vehicle]:
        return [v for v in self._v.values() if now - v.ts <= max_age]

    def routes(self, now: float | None = None, max_age: float = 300) -> set[str]:
        if now is None:
            return {v.route for v in self._v.values()}
        return {v.route for v in self.fresh(now, max_age)}

    def by_route(self, route: str, now: float, max_age: float) -> list[Vehicle]:
        return [v for v in self.fresh(now, max_age) if v.route == route]
