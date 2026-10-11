import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from .db import Database
from .models import StopTrigger
from .stopinfo import Arrival, LineThread, RouteForecast, StopSnapshot

log = logging.getLogger("stop_engine")

POS_PREFIX = "kgt:"   # vehicle ids of estimates made from vehicle positions (not Yandex vehicleIds)


def bare_vehicle_id(vid: str) -> str:
    """'MKUKGT|1167' -> '1167', 'kgt:1167' -> '1167'."""
    return vid.removeprefix(POS_PREFIX).rsplit("|", 1)[-1]


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
    async def line(self, rf: RouteForecast, region: str | None) -> list[LineThread]: ...


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
        # (trigger id, vehicle id) -> (expires, announced arrival ts, how far that ts may drift for the same bus)
        self._fired: dict[tuple[int, str | None], tuple[float, float, float]] = {}

    def upcoming(self, snap: StopSnapshot, route_key: str, now: float,
                 extra: list[Arrival] | tuple = ()) -> list[tuple[Arrival, float]]:
        """Future arrivals at the stop; `extra` are estimates for buses Yandex doesn't show there yet."""
        rf = snap.routes.get(route_key)
        if rf is None and not extra:
            return []
        arrivals = rf.arrivals if rf is not None else []
        live = [a for a in arrivals if a.live]
        have = {a.vehicle_id for a in live if a.vehicle_id}
        live += [a for a in extra if a.vehicle_id not in have]
        pool = live if (live or not self.use_scheduled) else [a for a in arrivals if not a.live]
        res = [(a, snap.eta_min(a, now)) for a in pool]
        return sorted([(a, e) for a, e in res if e >= -1.0], key=lambda x: x[1])

    def _announced(self, trigger_id: int, a: Arrival) -> bool:
        """The bus was already announced under an id from the other source (positions vs Yandex)."""
        if a.vehicle_id is None:
            return False
        pos = a.vehicle_id.startswith(POS_PREFIX)
        for (tid, vid), (_, ts, tol) in self._fired.items():
            if tid != trigger_id or vid is None or vid.startswith(POS_PREFIX) == pos:
                continue
            if bare_vehicle_id(vid) == bare_vehicle_id(a.vehicle_id) or abs(ts - a.ts) <= tol:
                return True
        return False

    def check(self, triggers: list[StopTrigger], snaps: dict[str, StopSnapshot | None], now: float,
              stale_after: float, extra: dict[int, list[Arrival]] | None = None) -> list[StopHit]:
        self._fired = {k: v for k, v in self._fired.items() if v[0] > now}
        hits: list[StopHit] = []
        for t in triggers:
            snap = snaps.get(t.stop_id)
            if snap is None or now - snap.fetched_at > stale_after:
                continue
            up = self.upcoming(snap, t.route_key, now, (extra or {}).get(t.id, ()))
            for i, (a, eta) in enumerate(up):
                if eta > t.lead_min:
                    break
                key = (t.id, a.vehicle_id)
                if key in self._fired:
                    continue
                # a bus announced long in advance must not repeat before it has passed the stop
                ttl = self.vehicle_cooldown_s + max(eta, 0) * 60 if a.vehicle_id else (t.lead_min + 3) * 60
                seen = self._announced(t.id, a)
                self._fired[key] = (now + ttl, a.ts, min(420.0, max(120.0, eta * 15)))
                if seen:
                    continue
                hits.append(StopHit(t, snap, a, eta, up[i + 1][1] if i + 1 < len(up) else None))
                break
        return hits


class StopEngine:
    def __init__(self, source: StopSource, db: Database, matcher: StopMatcher,
                 notify: Callable[[StopHit], Awaitable[None]], *, interval: float = 10.0,
                 stale_after: float = 180.0, alert: Callable[[str], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.time, upstream=None):
        self.source, self.db, self.matcher, self.notify = source, db, matcher, notify
        self.interval, self.stale_after, self.alert, self.clock = interval, stale_after, alert, clock
        self.upstream = upstream   # upstream.Upstream: estimates beyond Yandex's horizon, optional
        self._alerted: str | None = None

    async def tick(self) -> list[StopHit]:
        triggers = await self.db.active_stop_triggers()
        wanted: dict[str, str] = {}
        for t in triggers:
            wanted.setdefault(t.stop_id, t.stop_url)
        if self.upstream is not None:   # targets first: the source refreshes in this order
            for sid, url in (await self.upstream.prepare(triggers)).items():
                wanted.setdefault(sid, url)
        await self.source.sync(wanted)
        await self.source.refresh()
        now = self.clock()
        snaps = {sid: self.source.snapshot(sid) for sid in wanted}
        extra: dict[int, list[Arrival]] = {}
        if self.upstream is not None:
            await self.upstream.observe(snaps, now)
            extra = self.upstream.project(triggers, snaps, now, self.stale_after)
        sent: list[StopHit] = []
        done_once: set[int] = set()
        for h in self.matcher.check(triggers, snaps, now, self.stale_after, extra):
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
