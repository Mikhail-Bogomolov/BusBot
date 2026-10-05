import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from .db import Database
from .models import StopTrigger
from .stopinfo import Arrival, StopSnapshot

log = logging.getLogger("stop_engine")


class SourceBlocked(RuntimeError):
    """Source refused service (captcha / ban). Do not retry aggressively."""


class StopSource(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def resolve(self, url: str) -> tuple[StopSnapshot, str]: ...
    async def get(self, stop_id: str, url: str | None = None, max_age: float = 20.0) -> StopSnapshot: ...
    async def sync(self, wanted: dict[str, str]) -> None: ...
    async def refresh(self) -> None: ...
    def snapshot(self, stop_id: str) -> StopSnapshot | None: ...
    def problem(self) -> str | None: ...


@dataclass(slots=True, frozen=True)
class StopHit:
    trigger: StopTrigger
    snapshot: StopSnapshot
    arrival: Arrival
    eta_min: float
    next_eta_min: float | None


class StopMatcher:
    def __init__(self, use_scheduled: bool = False, vehicle_cooldown_s: float = 1800):
        self.use_scheduled = use_scheduled
        self.vehicle_cooldown_s = vehicle_cooldown_s
        self._fired: dict[tuple[int, str | None], float] = {}

    def upcoming(self, snap: StopSnapshot, route_key: str, now: float) -> list[tuple[Arrival, float]]:
        rf = snap.routes.get(route_key)
        if rf is None:
            return []
        live = [a for a in rf.arrivals if a.live]
        pool = live if (live or not self.use_scheduled) else [a for a in rf.arrivals if not a.live]
        res = [(a, snap.eta_min(a, now)) for a in pool]
        return sorted([(a, e) for a, e in res if e >= -1.0], key=lambda x: x[1])

    def check(self, triggers: list[StopTrigger], snaps: dict[str, StopSnapshot | None],
              now: float, stale_after: float) -> list[StopHit]:
        self._fired = {k: exp for k, exp in self._fired.items() if exp > now}
        hits: list[StopHit] = []
        for t in triggers:
            snap = snaps.get(t.stop_id)
            if snap is None or now - snap.fetched_at > stale_after:
                continue
            up = self.upcoming(snap, t.route_key, now)
            if not up:
                continue
            a, eta = up[0]
            if eta > t.lead_min:
                continue
            key = (t.id, a.vehicle_id)
            if key in self._fired:
                continue
            ttl = self.vehicle_cooldown_s if a.vehicle_id else (t.lead_min + 3) * 60
            self._fired[key] = now + ttl
            hits.append(StopHit(t, snap, a, eta, up[1][1] if len(up) > 1 else None))
        return hits


class StopEngine:
    def __init__(self, source: StopSource, db: Database, matcher: StopMatcher,
                 notify: Callable[[StopHit], Awaitable[None]], *, interval: float = 10.0,
                 stale_after: float = 180.0, alert: Callable[[str], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.time):
        self.source, self.db, self.matcher, self.notify = source, db, matcher, notify
        self.interval, self.stale_after, self.alert, self.clock = interval, stale_after, alert, clock
        self._alerted: str | None = None

    async def tick(self) -> list[StopHit]:
        triggers = await self.db.active_stop_triggers()
        wanted: dict[str, str] = {}
        for t in triggers:
            wanted.setdefault(t.stop_id, t.stop_url)
        await self.source.sync(wanted)
        await self.source.refresh()
        now = self.clock()
        snaps = {sid: self.source.snapshot(sid) for sid in wanted}
        sent: list[StopHit] = []
        done_once: set[int] = set()
        for h in self.matcher.check(triggers, snaps, now, self.stale_after):
            if h.trigger.id in done_once:
                continue
            await self.notify(h)
            sent.append(h)
            if not h.trigger.recurring:
                done_once.add(h.trigger.id)
                await self.db.deactivate_stop(h.trigger.id)
        return sent

    async def _check_alert(self) -> None:
        p = self.source.problem()
        if p and p != self._alerted and self.alert:
            self._alerted = p
            await self.alert(f"⚠️ Источник остановок: {p}")
        elif not p and self._alerted and self.alert:
            self._alerted = None
            await self.alert("✅ Источник остановок снова работает")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("stop tick failed: %r", e)
            try:
                await self._check_alert()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.interval)
