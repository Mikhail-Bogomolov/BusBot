"""Check Yandex stop data from the command line, without Telegram.

    python -m busbot.probe "https://yandex.ru/maps/-/XXXX"      # or a full stop URL / stop__123
    python -m busbot.probe 55.9980,92.7960                       # nearest stops to a point (lat,lon)
    python -m busbot.probe --browser "<url>"                     # the old headless-Chromium source
    python -m busbot.probe --line "<url>" 3 [30]                 # alerts beyond ~15 min: route scheme + upstream stops
Prints the parsed forecast / found stops and the time it took.
"""
import asyncio
import dataclasses
import re
import sys
import time

from dotenv import load_dotenv

from .config import load_upstream, load_yandex
from .models import StopTrigger, norm_route
from .sources.yandex import YandexStopSource
from .sources.yandex_http import YandexHttpSource
from .stop_engine import StopMatcher
from .stopinfo import extract_stop_id
from .upstream import Upstream

POINT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def _stop_url(arg: str) -> str:
    return arg if arg.startswith("http") else f"https://yandex.ru/maps/?masstransit%5BstopId%5D={extract_stop_id(arg)}"


async def main(arg: str, browser: bool) -> int:
    load_dotenv()
    cfg = load_yandex()
    if browser:
        # separate browser profile: the running bot may hold the main one
        src = YandexStopSource(dataclasses.replace(cfg, profile_dir=cfg.profile_dir.rstrip("/") + "-probe"))
    else:
        src = YandexHttpSource(cfg)
    await src.start()
    code = 0
    try:
        t0 = time.time()
        pt = POINT_RE.match(arg)
        if pt:
            if browser:
                raise ValueError("поиск по точке есть только в HTTP-источнике")
            near = await src.nearby(float(pt.group(1)), float(pt.group(2)))
            print(f"OK за {time.time() - t0:.1f} с, найдено {len(near)}:")
            for s in near:
                print(f"  {s.stop_id:<16} {s.name:<32} {s.dist_m:6.0f} м  {s.bearing:3.0f}°")
            return 0
        snap, watch_url = await src.resolve(_stop_url(arg))
        m = StopMatcher(use_scheduled=True)
        print(f"OK за {time.time() - t0:.1f} с\nОстановка: {snap.name} ({snap.stop_id})\nURL для опроса: {watch_url}\n")
        for rf in sorted(snap.routes.values(), key=lambda r: (r.type, len(r.name), r.name)):
            up = m.upcoming(snap, rf.key, time.time())
            etas = ", ".join(f"{e:.0f} мин{'' if a.live else ' (расп.)'}" for a, e in up[:3]) or "—"
            print(f"  {rf.title:<28} {etas:<30} {rf.frequency or ''}")
        live = [snap.eta_min(a, time.time()) for r in snap.routes.values() for a in r.arrivals if a.live]
        far = f" Самый дальний — через {max(live):.0f} мин (горизонт Яндекса, YANDEX_HORIZON)." if live else ""
        print(f"\nGPS-прогнозов: {len(live)}.{far}")
    except Exception as e:
        code = 1
        print(f"ОШИБКА: {type(e).__name__}: {e}")
    finally:
        if getattr(src, "seen", None):
            print("\nAPI-запросы страницы (последние):")
            for s in list(src.seen)[-25:]:
                print("  ", s)
        await src.close()
    return code


async def line_main(arg: str, route: str, lead: int) -> int:
    """Route scheme, upstream stops for `lead` and one look at them now (1 + 1 + up to 4 page loads)."""
    load_dotenv()
    src = YandexHttpSource(load_yandex())
    await src.start()
    try:
        t0 = time.time()
        snap, url = await src.resolve(_stop_url(arg))
        rf = next((r for r in snap.routes.values() if r.key.split("|")[0] == norm_route(route)), None)
        if rf is None:
            print(f"На «{snap.name}» нет маршрута {route}. Есть: {', '.join(r.title for r in snap.routes.values())}")
            return 1
        up = Upstream(src, load_upstream())
        p = await up.plan_for(snap.stop_id, rf.key, lead, url)
        print(f"{rf.title} → «{snap.name}» ({snap.stop_id}), упреждение {lead} мин, за {time.time() - t0:.1f} с")
        if p.error:
            print(f"ОШИБКА: {p.error}")
            return 1
        for th, i in p.threads:
            print(f"  направление {th.thread_id}: {th.stops[0].name} → {th.stops[-1].name}; "
                  f"остановка {i + 1}-я из {len(th.stops)}, {th.stops[i].at_m / 1000:.1f} км от начала")
        print("Контрольные остановки:" if p.anchors else "Контрольных остановок нет.")
        for a in sorted(p.anchors, key=lambda a: a.dist_m):
            sec, basis = up.travel.seconds(a.stop_id, snap.stop_id, a.dist_m, time.time())
            print(f"  {a.name:<34} {a.stop_id:<16} {a.dist_m / 1000:4.1f} км  ≈{sec / 60:.0f} мин ({basis})")
        print(f"Видно заранее: ~{p.cover_min:.0f} мин; по координатам КГТ — до ~{p.route_min:.0f} мин")
        wanted = {snap.stop_id: url, **{a.stop_id: up.stop_url(a.stop_id) for a in p.anchors}}
        await src.sync(wanted)
        await src.refresh()
        now = time.time()
        snaps = {sid: src.snapshot(sid) for sid in wanted}
        trig = StopTrigger(0, 0, snap.stop_id, url, snap.name, rf.key, rf.title, lead, False)
        est = up.project([trig], snaps, now, 600).get(0, [])
        ahead = StopMatcher().upcoming(snaps[snap.stop_id], rf.key, now, est)
        print("Сейчас: " + (", ".join(f"{'≈' if a.via else ''}{e:.0f} мин" + (f" ({a.via})" if a.via else " (GPS)")
                                      for a, e in ahead[:5]) or "автобусов не видно"))
        far = [s.eta_min(a, now) for s in snaps.values() if s for r in s.routes.values() for a in r.arrivals if a.live]
        if far:
            print(f"Самый дальний GPS-прогноз Яндекса на этих остановках: {max(far):.0f} мин "
                  f"(YANDEX_HORIZON={load_upstream().horizon_min:.0f})")
        if src.problem():
            print(f"Проблема источника: {src.problem()}")
        return 0
    except Exception as e:
        print(f"ОШИБКА: {type(e).__name__}: {e}")
        return 1
    finally:
        await src.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a not in ("--browser", "--line")]
    if "--line" in sys.argv and len(args) in (2, 3):
        sys.exit(asyncio.run(line_main(args[0], args[1], int(args[2]) if len(args) == 3 else 30)))
    if "--line" in sys.argv or len(args) != 1:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(args[0], "--browser" in sys.argv)))
