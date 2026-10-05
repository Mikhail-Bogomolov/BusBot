from .geo import angle_diff, bearing_deg, haversine_m
from .models import Hit, Trigger, Vehicle

DEFAULT_SPEED_KMH = 20.0
MIN_SPEED_KMH = 5.0
NEAR_M = 50.0  # inside this radius the "approaching" test is meaningless (GPS noise)


class Matcher:
    def __init__(self, cooldown_s: float = 900, bearing_tol: float = 60, approach_tol: float = 80):
        self.cooldown_s = cooldown_s
        self.bearing_tol = bearing_tol
        self.approach_tol = approach_tol
        self._fired: dict[tuple[int, str], float] = {}

    def _eta_min(self, dist_m: float, speed_kmh: float | None) -> int:
        s = speed_kmh if speed_kmh and speed_kmh >= MIN_SPEED_KMH else DEFAULT_SPEED_KMH
        return max(1, round(dist_m / (s / 3.6) / 60))

    def _ok(self, t: Trigger, v: Vehicle, dist: float) -> bool:
        if v.heading is None:
            return True  # nothing to filter on
        if dist > NEAR_M:
            # bus must be moving towards the point, not away from it
            if angle_diff(v.heading, bearing_deg(v.lat, v.lon, t.lat, t.lon)) > self.approach_tol:
                return False
        if t.bearing is not None and angle_diff(v.heading, t.bearing) > self.bearing_tol:
            return False  # opposite / wrong direction of travel
        return True

    def check(self, triggers: list[Trigger], vehicles: list[Vehicle], now: float) -> list[Hit]:
        self._fired = {k: ts for k, ts in self._fired.items() if now - ts < self.cooldown_s}
        hits: list[Hit] = []
        for t in triggers:
            for v in vehicles:
                if v.route != t.route:
                    continue
                dist = haversine_m(v.lat, v.lon, t.lat, t.lon)
                if dist > t.radius_m or not self._ok(t, v, dist):
                    continue
                key = (t.id, v.vehicle_id)
                if key in self._fired:
                    continue
                self._fired[key] = now
                hits.append(Hit(t, v, dist, self._eta_min(dist, v.speed)))
        return hits
