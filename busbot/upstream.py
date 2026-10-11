"""Warnings earlier than Yandex shows a bus at the stop (its GPS forecasts reach only ~15 min ahead).

A. Upstream stops. The route page lists every thread's stops in order. For an alert whose lead exceeds the horizon
   a few stops upstream of the target are polled too ("anchors", at most `step` minutes of travel apart, so their
   horizons overlap) and each GPS arrival there is moved onto the target: eta_T = eta_U + travel(U -> T).
   travel(U -> T) is learned from real passages of the same vehicleId at U and then at T: the last buses
   (today's traffic) first, then the same hour on earlier days, then path length / speed.
B. Vehicle positions (SOURCE=kgt). A bus is placed on the thread's path (close to it, moving along it); the distance
   left to the target over the effective speed gives its ETA. Sees the whole thread, not just the last stops.
Estimates are Arrival(live=True, via=<what it is based on>) and are made only for alerts with lead > horizon.
"""
import logging
import statistics
import time
from dataclasses import dataclass, field

from .geo import locate_on_path
from .models import StopTrigger
from .stop_engine import POS_PREFIX, bare_vehicle_id
from .stopinfo import STOP_ID_RE, Arrival, LineThread, StopSnapshot

log = logging.getLogger("upstream")


@dataclass(frozen=True)
class UpstreamConfig:
    enabled: bool = True
    horizon_min: float = 15.0      # how far ahead Yandex shows GPS forecasts at a stop
    speed_kmh: float = 18.0        # bus speed incl. stops, used until real travel times are learned
    max_anchors: int = 4           # extra stops polled per alert, all directions together
    positions: bool = True         # variant B when vehicle positions exist (SOURCE=kgt)
    replan_s: float = 1800.0       # anchors are re-chosen this often (learned times change spacing)


@dataclass(slots=True, frozen=True)
class Anchor:
    stop_id: str
    name: str
    thread_id: str
    dist_m: float                  # along the route to the target


@dataclass(slots=True)
class Plan:
    stop_id: str
    route_key: str
    lead: int
    made_at: float
    threads: list[tuple[LineThread, int]] = field(default_factory=list)   # direction + target's index in it
    anchors: list[Anchor] = field(default_factory=list)
    cover_min: float = 0.0         # how far ahead a bus is seen with Yandex + anchors (worst direction)
    route_min: float = 0.0         # travel time from the start of the shortest direction (what positions see)
    error: str | None = None


def _hour(ts: float) -> int:
    return int(ts // 3600) % 24    # UTC hour: one city, fixed offset, so "same hour" comparisons hold


class Travel:
    """Travel times between stops: learned from bus passages, else distance / speed on the way to the stop."""
    RECENT_S = 7200
    HISTORY_S = 14 * 86400

    def __init__(self, speed_kmh: float, db=None):
        self.speed = speed_kmh / 3.6
        self.db = db
        self._obs: dict[tuple[str, str], list[tuple[float, float, float]]] = {}   # (from, to) -> [(at, s, dist_m)]

    async def load(self, now: float) -> None:
        if self.db is not None:
            for a, b, dist, sec, at in await self.db.segment_times(now - self.HISTORY_S):
                self._obs.setdefault((a, b), []).append((at, sec, dist))

    async def add(self, a: str, b: str, dist_m: float, seconds: float, at: float) -> None:
        obs = [x for x in self._obs.get((a, b), []) if at - x[0] <= self.HISTORY_S]
        self._obs[(a, b)] = obs + [(at, seconds, dist_m)]
        if self.db is not None:
            await self.db.add_segment_time(a, b, dist_m, seconds, at)

    def _pick(self, obs: list[tuple[float, float, float]], now: float) -> tuple[list[tuple[float, float, float]], str]:
        recent = [x for x in obs if now - x[0] <= self.RECENT_S]
        if recent:
            return recent[-3:], "live"
        h = _hour(now)
        same = [x for x in obs if now - x[0] <= self.HISTORY_S and min((h - _hour(x[0])) % 24, (_hour(x[0]) - h) % 24) <= 1]
        return (same, "history") if len(same) >= 2 else ([], "")

    def speed_to(self, b: str, now: float) -> tuple[float, str]:
        """Speed of the last buses on their way to stop b (m/s, basis); the default speed if nothing is learned."""
        obs, basis = self._pick(sorted(x for (_, to), xs in self._obs.items() if to == b for x in xs), now)
        d, s = sum(x[2] for x in obs), sum(x[1] for x in obs)
        return (d / s, basis) if d > 0 and s > 0 else (self.speed, "path")

    def seconds(self, a: str, b: str, dist_m: float, now: float) -> tuple[float, str]:
        """(seconds, basis): 'live' = last buses within 2 h, 'history' = same hour on earlier days,
        otherwise dist_m over the speed towards b (which itself may be learned on other stops)."""
        obs, basis = self._pick(self._obs.get((a, b), []), now)
        if obs:
            return statistics.median(x[1] for x in obs), basis
        v, basis = self.speed_to(b, now)
        return dist_m / v, basis


class Upstream:
    def __init__(self, source, cfg: UpstreamConfig, db=None, positions=None, pos_max_age: float = 120.0,
                 clock=time.time):
        self.source, self.cfg, self.clock = source, cfg, clock
        self.positions = positions if cfg.positions else None   # store.VehicleStore
        self.pos_max_age = pos_max_age
        self.travel = Travel(cfg.speed_kmh, db)
        self._plans: dict[tuple[str, str, int], Plan] = {}
        self._fetched: dict[str, float] = {}                     # stop -> fetched_at of the last observed snapshot
        self._last: dict[str, dict[str, float]] = {}             # stop -> {vehicle: predicted arrival}
        self._passed: dict[tuple[str, str], tuple[float, float]] = {}   # (stop, vehicle) -> (server ts, local now)
        self.last: dict[int, list[Arrival]] = {}                 # trigger id -> estimates of the last tick (/now)

    async def load(self) -> None:
        await self.travel.load(self.clock())

    def long(self, lead: float) -> bool:
        return self.cfg.enabled and lead > self.cfg.horizon_min

    # ---------- planning
    async def plan_for(self, stop_id: str, route_key: str, lead: int, url: str | None = None) -> Plan:
        """Plan for an alert, loading the stop if it isn't cached (used when the alert is created)."""
        snap = self.source.snapshot(stop_id)
        if snap is None:
            try:
                snap = await self.source.get(stop_id, url, max_age=600)
            except Exception as e:
                return Plan(stop_id, route_key, lead, self.clock(), error=str(e) or type(e).__name__)
        return await self._plan(stop_id, route_key, lead, snap)

    async def _plan(self, stop_id: str, route_key: str, lead: int, snap: StopSnapshot) -> Plan:
        now = self.clock()
        key = (stop_id, route_key, lead)
        p = self._plans.get(key)
        if p is not None and now - p.made_at < (self.cfg.replan_s if p.error is None else 600):
            return p
        p = Plan(stop_id, route_key, lead, now)
        try:
            await self._build(p, snap, now)
        except Exception as e:   # blocked / network / format: the alert still works within Yandex's horizon
            p.threads, p.anchors, p.error = [], [], str(e) or type(e).__name__
            p.cover_min = self.cfg.horizon_min
            log.info("plan %s %s: %s", stop_id, route_key, p.error)
        self._plans[key] = p
        return p

    async def _build(self, p: Plan, snap: StopSnapshot, now: float) -> None:
        rf = snap.routes.get(p.route_key)
        if rf is None:
            raise ValueError("маршрута нет в ответе остановки")
        threads = await self.source.line(rf, snap.region)
        own = [th for th in threads if th.thread_id in rf.thread_ids and th.index(p.stop_id) is not None]
        own = own or [th for th in threads if th.index(p.stop_id) is not None]
        if not own:
            raise ValueError("остановки нет на схеме маршрута")
        p.threads = [(th, th.index(p.stop_id)) for th in own]
        hor = self.cfg.horizon_min * 60
        step = max(hor - 180, hor / 2)          # anchors' horizons overlap: no blind zone between them
        need = (p.lead + 2) * 60                # see the bus a bit earlier than `lead` (polling lag)
        taken: dict[tuple[str, str], Anchor] = {}
        covers, starts = [], []
        for th, idx in p.threads:
            target = th.stops[idx]
            ups: list[tuple[float, object]] = []
            for s in reversed(th.stops[:idx]):
                if s.stop_id == p.stop_id:      # loop thread: the previous visit of the target
                    break
                ups.append((self.travel.seconds(s.stop_id, p.stop_id, target.at_m - s.at_m, now)[0], s))
            ups.sort(key=lambda x: x[0])
            starts.append(ups[-1][0] if ups else 0.0)
            prev = 0.0
            while prev + hor < need and len({k[0] for k in taken}) < self.cfg.max_anchors:
                best = None
                for sec, s in ups:              # the farthest stop within one step (or the next one, if none is)
                    if sec <= prev or not STOP_ID_RE.fullmatch(s.stop_id):
                        continue
                    if sec - prev > step:
                        best = best or (sec, s)
                        break
                    best = (sec, s)
                if best is None:
                    break
                prev, s = best
                taken.setdefault((s.stop_id, th.thread_id), Anchor(s.stop_id, s.name, th.thread_id, target.at_m - s.at_m))
            covers.append(prev + hor)
        p.anchors = list(taken.values())
        p.cover_min = min(covers) / 60
        p.route_min = min(starts) / 60

    def stop_url(self, stop_id: str) -> str:
        f = getattr(self.source, "stop_url", None)
        return f(stop_id) if f else f"https://yandex.ru/maps/?masstransit%5BstopId%5D={stop_id}"

    async def prepare(self, triggers: list[StopTrigger]) -> dict[str, str]:
        """Plans for the long-lead alerts. Returns the anchor stops to poll: stop_id -> url."""
        keep: set[tuple[str, str, int]] = set()
        want: dict[str, str] = {}
        for t in triggers:
            if not self.long(t.lead_min):
                continue
            keep.add((t.stop_id, t.route_key, t.lead_min))
            snap = self.source.snapshot(t.stop_id)
            if snap is None:                    # the target itself is fetched first; plan on the next tick
                continue
            p = await self._plan(t.stop_id, t.route_key, t.lead_min, snap)
            for a in p.anchors:
                want.setdefault(a.stop_id, self.stop_url(a.stop_id))
        for k in [k for k in self._plans if k not in keep]:
            del self._plans[k]
        return want

    # ---------- learning travel times
    async def observe(self, snaps: dict[str, StopSnapshot | None], now: float) -> None:
        """A bus that was about to arrive and vanished from a stop's forecast has passed it. A passage at a target
        after a passage at one of its anchors is a real travel time anchor -> target."""
        for sid, snap in snaps.items():
            if snap is None or self._fetched.get(sid) == snap.fetched_at:
                continue
            self._fetched[sid] = snap.fetched_at
            t_srv = snap.now_server(snap.fetched_at)
            cur = {a.vehicle_id: a.ts for rf in snap.routes.values() for a in rf.arrivals if a.live and a.vehicle_id}
            for vid, ts in self._last.get(sid, {}).items():
                if vid not in cur and ts <= t_srv + 90:
                    await self._passage(sid, vid, min(ts, t_srv), now)
            self._last[sid] = cur
        self._passed = {k: v for k, v in self._passed.items() if now - v[1] <= 3 * 3600}

    async def _passage(self, sid: str, vid: str, t: float, now: float) -> None:
        self._passed[(sid, vid)] = (t, now)
        done: set[str] = set()
        for p in list(self._plans.values()):
            if p.stop_id != sid:
                continue
            for a in p.anchors:
                u = self._passed.get((a.stop_id, vid))
                if u is None or a.stop_id in done:
                    continue
                sec, path = t - u[0], a.dist_m / self.travel.speed
                if 0.3 * path <= sec <= 3 * path + 600:      # sanity: not another trip of the same bus
                    done.add(a.stop_id)
                    await self.travel.add(a.stop_id, sid, a.dist_m, sec, now)
                    log.info("travel %s -> %s: %.0f s (path estimate %.0f s), vehicle %s", a.stop_id, sid, sec, path, vid)

    # ---------- estimates
    def project(self, triggers: list[StopTrigger], snaps: dict[str, StopSnapshot | None], now: float,
                stale_after: float) -> dict[int, list[Arrival]]:
        """Estimated arrivals at the target for buses Yandex doesn't show there yet, per long-lead trigger."""
        out: dict[int, list[Arrival]] = {}
        for t in triggers:
            if not self.long(t.lead_min):
                continue
            p = self._plans.get((t.stop_id, t.route_key, t.lead_min))
            snap = snaps.get(t.stop_id)
            if p is None or not p.threads or snap is None or now - snap.fetched_at > stale_after:
                continue
            rf = snap.routes.get(t.route_key)
            direct = [a for a in (rf.arrivals if rf else []) if a.live]
            ids = {a.vehicle_id for a in direct if a.vehicle_id}
            est = [a for a in self._from_anchors(p, snaps, now, stale_after) if a.vehicle_id not in ids]
            if self.positions is not None:
                ref, srv = direct + est, snap.now_server(now)
                for a in self._from_positions(p, snap, now):
                    tol = min(420.0, max(120.0, (a.ts - srv) * 0.25))
                    if not any(abs(a.ts - b.ts) <= tol or (b.vehicle_id and bare_vehicle_id(b.vehicle_id) ==
                                                           bare_vehicle_id(a.vehicle_id)) for b in ref):
                        est.append(a)           # a bus Yandex doesn't know about (yet)
            out[t.id] = est
        self.last = out
        return out

    def _from_anchors(self, p: Plan, snaps: dict[str, StopSnapshot | None], now: float,
                      stale_after: float) -> list[Arrival]:
        best: dict[str, Arrival] = {}
        for a in sorted(p.anchors, key=lambda x: x.dist_m):     # the nearest anchor gives the best estimate
            snap = snaps.get(a.stop_id)
            if snap is None or now - snap.fetched_at > stale_after:
                continue
            rf = snap.routes.get(p.route_key)
            if rf is None:
                continue
            sec, _ = self.travel.seconds(a.stop_id, p.stop_id, a.dist_m, now)
            for x in rf.arrivals:
                if not x.live or not x.vehicle_id or x.vehicle_id in best:
                    continue
                if x.thread_id and x.thread_id != a.thread_id:   # another direction of the route
                    continue
                if snap.eta_min(x, now) < -0.5:                  # already past the anchor
                    continue
                best[x.vehicle_id] = Arrival(x.ts + sec, True, x.vehicle_id, a.thread_id, f"по GPS у «{a.name}»")
        return list(best.values())

    def _from_positions(self, p: Plan, snap: StopSnapshot, now: float) -> list[Arrival]:
        vs = self.positions.by_route(p.route_key.split("|")[0], now, self.pos_max_age)
        if not vs:
            return []
        speed, _ = self.travel.speed_to(p.stop_id, now)
        srv = snap.now_server(now)
        best: dict[str, Arrival] = {}
        for v in vs:
            if v.heading is None:               # direction unknown: could be the opposite side of the road
                continue
            for th, idx in p.threads:
                target = th.stops[idx].at_m
                start = max((s.at_m for s in th.stops[:idx] if s.stop_id == p.stop_id), default=0.0)
                cands = [(off, at) for at, off in locate_on_path(th.path, v.lat, v.lon, v.heading)
                         if start <= at < target - 30]
                if not cands:
                    continue
                at = min(cands)[1]
                ts = srv + max(0.0, (target - at) / speed - max(0.0, now - v.ts))
                vid = POS_PREFIX + v.vehicle_id
                if vid not in best or ts < best[vid].ts:
                    best[vid] = Arrival(ts, True, vid, th.thread_id, "по координатам автобуса (КГТ)")
        return list(best.values())
