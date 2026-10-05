import asyncio

import pytest

from busbot.db import Database
from busbot.engine import Engine
from busbot.geo import angle_diff, bearing_deg, haversine_m
from busbot.matcher import Matcher
from busbot.models import Trigger, Vehicle, norm_route
from busbot.sources.kgt import KgtConfig, parse_payload
from busbot.sources.mock import MockSource
from busbot.store import VehicleStore

P = (56.0100, 92.8900)  # test point on the mock line


def trig(**kw):
    d = dict(id=1, chat_id=10, route="99", title="t", lat=P[0], lon=P[1],
             radius_m=800, bearing=None, recurring=False)
    d.update(kw)
    return Trigger(**d)


def veh(lon, heading, vid="b1", route="99", lat=P[0], speed=36.0, ts=0.0):
    return Vehicle(vid, route, lat, lon, heading, speed, ts)


# ---- geo
def test_haversine_known_distance():
    # 0.01 deg of latitude ~ 1111.95 m
    assert haversine_m(56.0, 92.0, 56.01, 92.0) == pytest.approx(1111.95, abs=1.5)


def test_bearing_cardinals():
    assert bearing_deg(56, 92, 57, 92) == pytest.approx(0, abs=0.01)
    assert bearing_deg(56, 92, 56, 93) == pytest.approx(90, abs=0.6)
    assert bearing_deg(56, 92, 55, 92) == pytest.approx(180, abs=0.01)


def test_angle_diff_wraparound():
    assert angle_diff(350, 10) == 20
    assert angle_diff(0, 180) == 180


def test_norm_route():
    assert norm_route(" Автобус 135 ") == "135"
    assert norm_route("А 55") == "а55"


# ---- matcher
def test_fires_when_approaching_in_radius():
    h = Matcher().check([trig()], [veh(92.8800, 90)], now=0)  # ~620 m west, heading east
    assert len(h) == 1 and 550 < h[0].dist_m < 700


def test_ignores_outside_radius():
    assert Matcher().check([trig()], [veh(92.8600, 90)], now=0) == []


def test_ignores_bus_moving_away():
    assert Matcher().check([trig()], [veh(92.8800, 270)], now=0) == []


def test_direction_filter_blocks_opposite_flow():
    # approaching the point from the east, but user wants eastbound (90)
    assert Matcher().check([trig(bearing=90)], [veh(92.9000, 270)], now=0) == []
    assert len(Matcher().check([trig(bearing=270)], [veh(92.9000, 270)], now=0)) == 1


def test_wrong_route_ignored():
    assert Matcher().check([trig()], [veh(92.8800, 90, route="7")], now=0) == []


def test_cooldown_and_expiry():
    m = Matcher(cooldown_s=900)
    v = veh(92.8800, 90)
    assert len(m.check([trig()], [v], 0)) == 1
    assert m.check([trig()], [v], 100) == []
    assert len(m.check([trig()], [v], 1000)) == 1


def test_unknown_heading_still_fires():
    assert len(Matcher().check([trig()], [veh(92.8800, None)], 0)) == 1


# ---- store: heading derived from movement
def test_store_derives_heading():
    s = VehicleStore()
    s.update([veh(92.880, None, ts=0)])
    s.update([veh(92.882, None, ts=20)])
    v = s.fresh(20, 120)[0]
    assert v.heading == pytest.approx(90, abs=1)
    s.update([veh(92.882, None, ts=40)])  # standing still keeps heading
    assert s.fresh(40, 120)[0].heading == pytest.approx(90, abs=1)


def test_store_staleness():
    s = VehicleStore()
    s.update([veh(92.88, 90, ts=0)])
    assert s.fresh(130, 120) == []


# ---- KGT parser
def test_parse_payload_nested_and_scaled():
    cfg = KgtConfig(list_path="data.items", f_id="vid", f_route="r", f_lat="la", f_lon="lo",
                    f_heading="az", f_speed="sp", f_ts="t", coord_scale=1e-6)
    payload = {"data": {"items": [
        {"vid": "A1", "r": "Автобус 135", "la": 56010000, "lo": 92890000, "az": "90,5", "sp": "32", "t": 1_700_000_000_000},
        {"vid": "bad", "r": "7", "la": None, "lo": 1},
        {"vid": "zero", "r": "7", "la": 0, "lo": 0},
        "junk",
    ]}}
    vs = parse_payload(payload, cfg, now=5)
    assert len(vs) == 1
    v = vs[0]
    assert (v.route, v.lat, v.lon, v.heading, v.speed, v.ts) == ("135", 56.01, 92.89, 90.5, 32.0, 1_700_000_000.0)


def test_parse_payload_wrong_shape_raises():
    with pytest.raises(ValueError):
        parse_payload({"x": 1}, KgtConfig(list_path="data"), 0)


# ---- end-to-end with mock source, real SQLite, simulated clock
def test_e2e_mock_one_shot_and_direction(tmp_path):
    async def run():
        db = Database(str(tmp_path / "t.sqlite3"))
        await db.open()
        now = [0.0]
        src = MockSource(clock=lambda: now[0])
        sent = []

        async def notify(h):
            sent.append((h.trigger.id, h.vehicle.vehicle_id))

        eng = Engine(src, VehicleStore(), db, Matcher(), notify, clock=lambda: now[0])
        # eastbound only (bearing 90): must fire for MOCK-99E and never for MOCK-99W
        await db.add_trigger(10, "99", "p", P[0], P[1], 800, 90.0, recurring=False)
        for _ in range(60):          # 60 * 20 s = 1200 s, > full 500 s cycle
            await eng.tick()
            now[0] += 20
        assert sent == [(1, "MOCK-99E")]
        assert await db.active_triggers() == []   # one-shot got deactivated
        await db.close()

    asyncio.run(run())


def test_e2e_recurring_fires_each_pass(tmp_path):
    async def run():
        db = Database(str(tmp_path / "t.sqlite3"))
        await db.open()
        now = [0.0]
        src = MockSource(clock=lambda: now[0])
        sent = []

        async def notify(h):
            sent.append(h.vehicle.vehicle_id)

        eng = Engine(src, VehicleStore(), db, Matcher(cooldown_s=300), notify, clock=lambda: now[0])
        await db.add_trigger(10, "99", "p", P[0], P[1], 800, 90.0, recurring=True)
        for _ in range(130):         # ~2600 s = ~5 cycles
            await eng.tick()
            now[0] += 20
        assert 4 <= len(sent) <= 6 and set(sent) == {"MOCK-99E"}
        await db.close()

    asyncio.run(run())


def test_engine_backoff_and_alert(tmp_path):
    async def run():
        db = Database(str(tmp_path / "t.sqlite3"))
        await db.open()

        class Broken:
            async def fetch(self):
                raise RuntimeError("boom")

        alerts = []

        async def alert(t):
            alerts.append(t)

        async def notify(h): ...

        eng = Engine(Broken(), VehicleStore(), db, Matcher(), notify, interval=0.001, alert=alert, alert_after=3)
        t = asyncio.create_task(eng.run())
        await asyncio.sleep(0.5)
        t.cancel()
        assert eng.fails >= 3 and len(alerts) == 1
        await db.close()

    asyncio.run(run())
