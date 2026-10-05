import asyncio
import json
from pathlib import Path

import pytest

from busbot.db import Database
from busbot.sources.mock_stops import HEADWAY, MockStopSource
from busbot.stop_engine import StopEngine, StopMatcher
from busbot.stopinfo import Arrival, RouteForecast, StopSnapshot, extract_stop_id, parse_stop_info, route_key
from busbot.models import StopTrigger

FIX = Path(__file__).parent / "fixtures"
T2019 = 1552240247  # 2019-03-10 20:50:47 MSK, the fixture's currentTime

V2 = {"data": {"properties": {"currentTime": 1_700_000_000_000, "StopMetaData": {
    "id": "stop__123", "name": "Тест", "Transport": [
        {"name": "135", "type": "bus", "threads": [{"BriefSchedule": {
            "Events": [{"estimated": {"value": "1700000300"}, "vehicleId": "v1"},
                       {"scheduled": {"value": "1700000900"}}],
            "Frequency": {"text": "12 мин"}}}]},
        {"name": "7", "type": "trolleybus", "threads": [
            {"BriefSchedule": {"Events": []}},
            {"BriefSchedule": {"Events": [{"Estimated": {"value": 1700000120}, "vehicleId": "t9"}]}}]},
        {"name": "5", "type": "tramway", "threads": []},
    ]}}}}


def test_parse_2019_format():
    snap = parse_stop_info(json.loads((FIX / "getStopInfo_2019.json").read_text(encoding="utf-8")), T2019)
    assert snap.stop_id == "stop__9647487" and snap.name == "Метро Марьино (северная)"
    r623 = snap.routes[route_key("623", "bus")]
    assert [a.live for a in r623.arrivals] == [True, True, False]
    assert snap.eta_min(r623.arrivals[0], T2019) == pytest.approx((1552241094 - T2019) / 60)
    assert r623.arrivals[0].vehicle_id.startswith("codd")
    assert snap.routes[route_key("Ашан - Братиславская", "bus")].arrivals == []      # frequency only
    assert snap.routes[route_key("Ашан - Братиславская", "bus")].frequency == "20 мин"
    assert not any(a.live for a in snap.routes[route_key("н5", "bus")].arrivals)    # timetable only


def test_parse_threads_format_lowercase_strings_ms():
    snap = parse_stop_info(V2, fetched_at=50.0)
    assert snap.server_time == 1_700_000_000
    b = snap.routes["135|bus"]
    assert b.arrivals[0] == Arrival(1700000300, True, "v1") and b.arrivals[1].live is False
    assert b.frequency == "12 мин" and b.title == "Автобус 135"
    assert snap.eta_min(b.arrivals[0], 50.0) == pytest.approx(5.0)       # server clock, not local
    assert snap.eta_min(b.arrivals[0], 110.0) == pytest.approx(4.0)
    assert snap.routes["7|trolleybus"].arrivals[0].vehicle_id == "t9"
    assert snap.routes["5|tramway"].arrivals == []


def test_parse_2026_live_krasnoyarsk():
    """Real response captured 2026-10-05 for «Студгородок» (stop__9901344)."""
    d = json.loads((FIX / "getStopInfo_2026_krsk.json").read_text(encoding="utf-8"))
    snap = parse_stop_info(d, fetched_at=0.0)
    assert snap.stop_id == "stop__9901344" and snap.name == "Студгородок"
    assert snap.server_time == pytest.approx(1791185222.203)
    assert (snap.lat, snap.lon) == pytest.approx((55.997413238, 92.795747321))
    assert set(snap.routes) == {"63|bus", "3|bus", "38|bus", "83|bus", "2|bus", "6|trolleybus"}
    b3 = snap.routes["3|bus"]
    assert b3.line_id == "2611970554" and b3.frequency == "11 мин"
    assert b3.arrivals == [Arrival(1791185336.0, True, "MKUKGT|116741330756")]
    assert snap.eta_min(b3.arrivals[0], 0.0) == pytest.approx(1.9, abs=0.05)       # by server clock
    assert snap.routes["63|bus"].arrivals == [] and snap.routes["63|bus"].frequency == "16 мин"
    t6 = snap.routes["6|trolleybus"]                                               # 3 threads merged
    assert len(t6.arrivals) == 3 and not any(a.live for a in t6.arrivals)
    m = StopMatcher()
    assert [round(e, 1) for _, e in m.upcoming(snap, "2|bus", 0.0)] == [6.4]
    assert m.upcoming(snap, "6|trolleybus", 0.0) == []                             # timetable ignored


def test_parse_rejects_garbage():
    for bad in ({}, {"data": {"x": 1}}, {"error": {"message": "Invalid CSRF Token"}}, [1, 2]):
        with pytest.raises(ValueError):
            parse_stop_info(bad, 0)


def test_extract_stop_id():
    assert extract_stop_id("https://yandex.ru/maps/62/k/?masstransit%5BstopId%5D=stop__9680782&z=19") == "stop__9680782"
    assert extract_stop_id("no id") is None


# ---- matcher
def snap_with(*arrivals, fetched=0.0):
    s = StopSnapshot("stop__1", "S", fetched, fetched)
    rf = RouteForecast("135|bus", "135", "bus", list(arrivals))
    s.routes[rf.key] = rf
    return s


def trig(**kw):
    d = dict(id=1, chat_id=1, stop_id="stop__1", stop_url="u", stop_name="S", route_key="135|bus",
             route_title="Автобус 135", lead_min=5, recurring=True)
    d.update(kw)
    return StopTrigger(**d)


def test_matcher_fires_once_per_vehicle_then_next_vehicle():
    m = StopMatcher()
    s = snap_with(Arrival(240, True, "A"), Arrival(900, True, "B"))
    h = m.check([trig()], {"stop__1": s}, now=0, stale_after=180)
    assert len(h) == 1 and h[0].arrival.vehicle_id == "A" and h[0].eta_min == pytest.approx(4)
    assert h[0].next_eta_min == pytest.approx(15)
    assert m.check([trig()], {"stop__1": s}, now=60, stale_after=180) == []          # same bus
    s2 = snap_with(Arrival(900, True, "B"), fetched=600)
    assert len(m.check([trig()], {"stop__1": s2}, now=600, stale_after=180)) == 1    # 5.0 min, next bus


def test_matcher_respects_lead_and_staleness_and_scheduled():
    s = snap_with(Arrival(600, True, "A"))
    assert StopMatcher().check([trig()], {"stop__1": s}, 0, 180) == []                # 10 min > 5
    s = snap_with(Arrival(200, True, "A"))
    assert StopMatcher().check([trig()], {"stop__1": s}, 500, 180) == []              # stale data
    sch = snap_with(Arrival(120, False, None))
    assert StopMatcher().check([trig()], {"stop__1": sch}, 0, 180) == []              # timetable ignored
    assert len(StopMatcher(use_scheduled=True).check([trig()], {"stop__1": sch}, 0, 180)) == 1


def test_matcher_ignores_departed_bus():
    s = snap_with(Arrival(-300, True, "gone"), Arrival(200, True, "B"))
    h = StopMatcher().check([trig()], {"stop__1": s}, 0, 180)
    assert h[0].arrival.vehicle_id == "B"


# ---- engine end-to-end with mock source + SQLite + simulated clock
def test_stop_engine_mock_e2e(tmp_path):
    async def run():
        db = Database(str(tmp_path / "s.sqlite3"))
        await db.open()
        now = [10_300.0]   # bus 99 just left (-1.7 min); trolleybus 7 already 2.3 min away
        src = MockStopSource(clock=lambda: now[0])
        got = []

        async def notify(h):
            got.append((h.trigger.id, h.arrival.vehicle_id, round(h.eta_min, 1)))

        eng = StopEngine(src, db, StopMatcher(), notify, clock=lambda: now[0])
        await db.add_stop_trigger(1, "stop__42", "u", "S", "99|bus", "Автобус 99", 5, recurring=True)
        await db.add_stop_trigger(1, "stop__42", "u", "S", "7|trolleybus", "Троллейбус 7", 3, recurring=False)
        for _ in range(int(3 * HEADWAY / 10)):     # 30 min in 10 s ticks
            await eng.tick()
            now[0] += 10
        rec = [g for g in got if g[0] == 1]
        once = [g for g in got if g[0] == 2]
        assert len(rec) == 3 and len({g[1] for g in rec}) == 3 and all(4.8 <= g[2] <= 5.0 for g in rec)
        assert len(once) == 1 and once[0][2] == pytest.approx(2.3, abs=0.05)   # fires at once, inside lead
        assert [t.id for t in await db.active_stop_triggers()] == [1]
        await db.close()

    asyncio.run(run())
