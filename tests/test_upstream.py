"""Alerts beyond Yandex's ~15 min horizon: route page parser, upstream stops (A), vehicle positions (B)."""
import asyncio
import json
from pathlib import Path

import pytest

from busbot.db import Database
from busbot.geo import bearing_deg
from busbot.handlers import _long_lead_note
from busbot.models import StopTrigger, Vehicle
from busbot.stop_engine import StopEngine, StopMatcher
from busbot.stopinfo import STOP_ID_RE, Arrival, RouteForecast, StopSnapshot, parse_line
from busbot.store import VehicleStore
from busbot.upstream import Upstream, UpstreamConfig

FIX = Path(__file__).parent / "fixtures"
THREADS = parse_line(json.loads((FIX / "line_page_krsk.json").read_text(encoding="utf-8")))   # bus 3, real
TH = next(t for t in THREADS if t.thread_id == "5189728876")    # Академгородок -> Автовокзал Восточный
STUD = "stop__9901344"                                           # «Студгородок», 8th stop of TH
DEEP = "stop__9901801"                                           # «Агентство Аэрофлота», 16.6 km along TH
FIZ = "stop__9901277"                                            # «Физкультурный техникум», 6.5 km before DEEP


class World:
    """Buses driving TH: vehicle -> (time at the thread start, speed m/s). Forecasts at a stop look like
    Yandex's: a bus is listed from 15 min before it arrives until it arrives."""

    def __init__(self):
        self.buses: dict[str, tuple[float, float]] = {}
        self.at = {s.stop_id: s.at_m for s in TH.stops}

    def snap(self, sid: str, now: float) -> StopSnapshot:
        s = StopSnapshot(sid, sid, now, now, region="62/krasnoyarsk")
        rf = RouteForecast("3|bus", "3", "bus", line_id="2611970554", thread_ids=[TH.thread_id])
        if sid in self.at:
            for vid, (t0, v) in self.buses.items():
                arr = t0 + self.at[sid] / v
                if 0 <= arr - now <= 900:
                    rf.arrivals.append(Arrival(arr, True, vid, TH.thread_id))
        rf.arrivals.sort(key=lambda a: a.ts)
        s.routes[rf.key] = rf
        return s


class Src:
    """StopSource double: every refresh re-reads World; the route page is the real fixture."""

    def __init__(self, world: World, clock):
        self.world, self.clock = world, clock
        self.snaps: dict[str, StopSnapshot] = {}
        self.wanted: dict[str, str] = {}
        self.line_calls = 0

    async def sync(self, wanted):
        self.wanted = dict(wanted)

    async def refresh(self):
        for sid in self.wanted:
            self.snaps[sid] = self.world.snap(sid, self.clock())

    def snapshot(self, sid):
        return self.snaps.get(sid)

    async def get(self, sid, url=None, max_age=20.0):
        self.snaps[sid] = self.world.snap(sid, self.clock())
        return self.snaps[sid]

    async def line(self, rf, region):
        self.line_calls += 1
        assert region == "62/krasnoyarsk" and rf.line_id == "2611970554"
        return THREADS

    def stop_url(self, sid):
        return f"https://yandex.ru/maps/62/krasnoyarsk/stops/{sid}/"

    def problem(self):
        return None


def plan(target, lead, positions=None, clock=lambda: 0.0):
    src = Src(World(), clock)
    up = Upstream(src, UpstreamConfig(), positions=positions, clock=clock)
    return src, up, asyncio.run(up.plan_for(target, "3|bus", lead))


def point_on(at: float) -> tuple[float, float, float]:
    """(lat, lon, heading) of the point `at` metres along TH."""
    for (la, lo, a), (lb, lob, b) in zip(TH.path, TH.path[1:]):
        if a <= at <= b and b > a:
            f = (at - a) / (b - a)
            return la + f * (lb - la), lo + f * (lob - lo), bearing_deg(la, lo, lb, lob)
    raise AssertionError(at)


def test_parse_line_real_krasnoyarsk():
    assert {t.thread_id for t in THREADS} == {"5189668206", "5189728876"}
    assert TH.line_id == "2611970554" and TH.name == "3"
    assert TH.stops[0].name == "Академгородок – конечная" and TH.stops[-1].name == "Автовокзал Восточный"
    i = TH.index(STUD)
    assert i == 7 and TH.stops[i].name == "Студгородок" and TH.stops[i].at_m == pytest.approx(3939, abs=5)
    assert all(a.at_m < b.at_m for a, b in zip(TH.stops, TH.stops[1:]))
    assert TH.path[-1][2] == pytest.approx(30_790, abs=50) and len(TH.path) > 1000
    other = next(t for t in THREADS if t is not TH)
    assert other.index(STUD) is None                    # the opposite direction stops across the road


def test_anchors_are_spaced_within_yandex_horizon():
    src, up, p = plan(DEEP, 40)
    assert p.error is None and [(th.thread_id, i) for th, i in p.threads] == [(TH.thread_id, TH.index(DEEP))]
    anchors = sorted(p.anchors, key=lambda a: a.dist_m)
    assert [a.name for a in anchors] == ["Театр имени Пушкина", "Физкультурный техникум", "Николаевская Слобода"]
    mins = [a.dist_m / 5 / 60 for a in anchors]          # default 18 km/h = 5 m/s
    assert all(0 < b - a <= 12 for a, b in zip([0] + mins, mins))      # horizons overlap: no blind zone
    assert p.cover_min >= 40 and all(STOP_ID_RE.fullmatch(a.stop_id) for a in anchors)
    assert all(TH.index(a.stop_id) < TH.index(DEEP) for a in anchors)
    assert len(plan(DEEP, 20)[2].anchors) == 1 and len(plan(DEEP, 30)[2].anchors) == 2
    asyncio.run(up.plan_for(DEEP, "3|bus", 40))
    assert src.line_calls == 1                           # plans are cached


def test_near_route_start_cover_is_limited():
    _, _, p = plan(STUD, 30)
    assert [a.name for a in p.anchors] == ["Институт"]   # the two stops above it have no stop__ id
    assert p.cover_min == pytest.approx(22.6, abs=0.1) and p.route_min == pytest.approx(13.1, abs=0.1)


def test_plan_errors_degrade_to_yandex_horizon():
    src, up, _ = plan(DEEP, 30)

    async def broken(rf, region):
        raise ValueError("на странице маршрута нет схемы (masstransitLine)")

    src.line = broken
    p = asyncio.run(up.plan_for(DEEP, "3|bus", 25))
    assert p.error and p.anchors == [] and p.cover_min == 15
    assert asyncio.run(up.prepare([StopTrigger(1, 1, DEEP, "u", "S", "3|bus", "Автобус 3", 25, True)])) == {}


def test_long_lead_note_texts():
    d = dict(stop_id=DEEP, route_key="3|bus", stop_url="u", lead=30)
    _, up, _ = plan(DEEP, 30)
    text = asyncio.run(_long_lead_note(up, d))
    assert "по 2 остановкам" in text and "«Театр имени Пушкина»" in text and "⚠️" not in text
    _, up, _ = plan(STUD, 30)
    assert "за 22 мин, а не за 30" in asyncio.run(_long_lead_note(up, dict(d, stop_id=STUD)))
    assert asyncio.run(_long_lead_note(up, dict(d, lead=10))) == ""
    assert "раньше предупредить не получится" in asyncio.run(_long_lead_note(None, d))
    _, up, _ = plan(DEEP, 40, positions=VehicleStore())
    text = asyncio.run(_long_lead_note(up, dict(d, lead=40)))
    assert "координатам автобусов КГТ" in text and "⚠️" not in text


def test_engine_warns_early_learns_travel_time_and_never_repeats(tmp_path):
    async def run():
        db = Database(str(tmp_path / "u.sqlite3"))
        await db.open()
        now = [0.0]
        clock = lambda: now[0]   # noqa: E731
        world = World()
        target = world.at[DEEP]
        world.buses = {"V": (0.0, 4.0), "W": (2400.0, 4.0)}      # 14.4 km/h: slower than the 18 km/h default
        src = Src(world, clock)
        up = Upstream(src, UpstreamConfig(), db=db, clock=clock)
        got = []

        async def notify(h):
            got.append((h.arrival.vehicle_id, h.eta_min, h.arrival.via, now[0]))

        eng = StopEngine(src, db, StopMatcher(), notify, clock=clock, upstream=up)
        await db.add_stop_trigger(1, DEEP, "u", "Агентство Аэрофлота", "3|bus", "Автобус 3", 30, recurring=True)
        while now[0] < 2400 + target / 4 + 60:
            await eng.tick()
            now[0] += 10
        assert [g[0] for g in got] == ["V", "W"]                 # one alert per bus, even when Yandex shows it
        (_, eta_v, via_v, t_v), (_, eta_w, via_w, t_w) = got
        assert 29.5 <= eta_v <= 30 and via_v == "по GPS у «Физкультурный техникум»"
        assert (target / 4 - t_v) / 60 > 33                      # nothing learned yet: the default speed is optimistic
        assert 29.5 <= eta_w <= 30
        assert (2400 + target / 4 - t_w) / 60 == pytest.approx(30, abs=0.5)   # learned from bus V: on time

        dist = target - world.at[FIZ]
        up2 = Upstream(src, UpstreamConfig(), db=db, clock=clock)  # learned times survive a restart
        await up2.load()
        sec, basis = up2.travel.seconds(FIZ, DEEP, dist, now[0])
        assert basis == "live" and sec == pytest.approx(dist / 4, abs=1)
        v, basis = up2.travel.speed_to(DEEP, now[0])            # used for stops that weren't anchors before
        assert basis == "live" and v == pytest.approx(4.0, abs=0.01)
        await db.close()

    asyncio.run(run())


def test_positions_estimate_buses_far_upstream():
    now = 1000.0
    store = VehicleStore()
    src, up, _ = plan(DEEP, 30, positions=store, clock=lambda: now)
    target = TH.stops[TH.index(DEEP)].at_m
    lat, lon, hd = point_on(target - 6000)
    store.update([
        Vehicle("11", "3", lat, lon, hd, None, now - 20),             # on the way, 6 km before the stop
        Vehicle("12", "3", lat, lon, (hd + 180) % 360, None, now),    # same place, opposite direction
        Vehicle("13", "3", lat + 0.003, lon, hd, None, now),          # 330 m off the route
        Vehicle("14", "3", *point_on(target + 2000), None, now),      # already past the stop
        Vehicle("15", "38", *point_on(target - 9000), None, now),     # another route
        Vehicle("16", "3", *point_on(target - 3000), None, now),      # Yandex shows it (other id, same time)
        Vehicle("77", "3", *point_on(target - 4000), None, now),      # Yandex shows it (same id, other time)
    ])
    snap = StopSnapshot(DEEP, "S", now, now)
    snap.routes["3|bus"] = RouteForecast("3|bus", "3", "bus", [
        Arrival(now + 610, True, "MKUKGT|500", TH.thread_id), Arrival(now + 300, True, "MKUKGT|77", TH.thread_id)])
    t = StopTrigger(1, 1, DEEP, "u", "S", "3|bus", "Автобус 3", 30, True)
    est = up.project([t], {DEEP: snap}, now, 180)[1]
    assert [a.vehicle_id for a in est] == ["kgt:11"]
    assert est[0].ts - now == pytest.approx(6000 / 5 - 20, abs=3) and est[0].via == "по координатам автобуса (КГТ)"
    assert up.last == {1: est}


# ---- matcher: one alert per bus
def snap_at(*arrivals, fetched=0.0):
    s = StopSnapshot("stop__1", "S", fetched, fetched)
    s.routes["3|bus"] = RouteForecast("3|bus", "3", "bus", list(arrivals))
    return s


def trig30():
    return StopTrigger(1, 1, "stop__1", "u", "S", "3|bus", "Автобус 3", 30, True)


def test_matcher_same_bus_announced_by_position_is_not_repeated_by_yandex():
    m = StopMatcher()
    hits = m.check([trig30()], {"stop__1": snap_at()}, 0, 180, {1: [Arrival(1500, True, "kgt:7", via="x")]})
    assert len(hits) == 1 and hits[0].arrival.via == "x"
    same = snap_at(Arrival(1300, True, "MKUKGT|555"), fetched=600)          # 200 s off: the same bus
    assert m.check([trig30()], {"stop__1": same}, 600, 180) == []
    nxt = snap_at(Arrival(1300, True, "MKUKGT|555"), Arrival(2400, True, "MKUKGT|556"), fetched=600)
    hits = m.check([trig30()], {"stop__1": nxt}, 600, 180)                  # the next bus still fires
    assert [h.arrival.vehicle_id for h in hits] == ["MKUKGT|556"]


def test_matcher_bus_announced_early_does_not_repeat_when_late():
    m = StopMatcher()
    assert len(m.check([trig30()], {"stop__1": snap_at(Arrival(1800, True, "A"))}, 0, 180)) == 1
    late = snap_at(Arrival(1980, True, "A"), fetched=1860)                  # 31 min later still 2 min away
    assert m.check([trig30()], {"stop__1": late}, 1860, 180) == []
