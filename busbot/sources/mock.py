"""Simulated source for end-to-end testing without the real feed.

Route '99': two buses on a straight W-E line at lat 56.0100 (lon 92.85..92.93), one each way.
Route '7': one eastbound bus, offset in phase. Test point on the line: 56.0100, 92.8900.
"""
import math
import time
from typing import Callable

from ..models import Vehicle

LAT = 56.0100
LON_A, LON_B = 92.85, 92.93
M_PER_DEG_LON = 111320 * math.cos(math.radians(LAT))


class MockSource:
    def __init__(self, speed_mps: float = 10.0, clock: Callable[[], float] = time.time):
        self.speed = speed_mps
        self.clock = clock
        self.length = (LON_B - LON_A) * M_PER_DEG_LON

    def _frac(self, t: float, phase: float = 0.0) -> float:
        return ((t * self.speed / self.length) + phase) % 1.0

    async def fetch(self) -> list[Vehicle]:
        t = self.clock()
        kmh = self.speed * 3.6
        east = LON_A + self._frac(t) * (LON_B - LON_A)
        west = LON_B - self._frac(t, 0.25) * (LON_B - LON_A)
        east7 = LON_A + self._frac(t, 0.5) * (LON_B - LON_A)
        return [
            Vehicle("MOCK-99E", "99", LAT, east, 90.0, kmh, t),
            Vehicle("MOCK-99W", "99", LAT + 0.0002, west, 270.0, kmh, t),
            Vehicle("MOCK-7E", "7", LAT, east7, 90.0, kmh, t),
        ]
