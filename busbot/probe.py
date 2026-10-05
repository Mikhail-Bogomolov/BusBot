"""Check Yandex stop data from the command line, without Telegram.

    python -m busbot.probe "https://yandex.ru/maps/-/XXXX"      # or a full stop URL / stop__123
    python -m busbot.probe 55.9980,92.7960                       # nearest stops to a point (lat,lon)
    python -m busbot.probe --browser "<url>"                     # the old headless-Chromium source
Prints the parsed forecast / found stops and the time it took.
"""
import asyncio
import dataclasses
import re
import sys
import time

from dotenv import load_dotenv

from .config import load_yandex
from .sources.yandex import YandexStopSource
from .sources.yandex_http import YandexHttpSource
from .stop_engine import StopMatcher
from .stopinfo import extract_stop_id

POINT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


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
        url = arg if arg.startswith("http") else f"https://yandex.ru/maps/?masstransit%5BstopId%5D={extract_stop_id(arg)}"
        snap, watch_url = await src.resolve(url)
        m = StopMatcher(use_scheduled=True)
        print(f"OK за {time.time() - t0:.1f} с\nОстановка: {snap.name} ({snap.stop_id})\nURL для опроса: {watch_url}\n")
        for rf in sorted(snap.routes.values(), key=lambda r: (r.type, len(r.name), r.name)):
            up = m.upcoming(snap, rf.key, time.time())
            etas = ", ".join(f"{e:.0f} мин{'' if a.live else ' (расп.)'}" for a, e in up[:3]) or "—"
            print(f"  {rf.title:<28} {etas:<30} {rf.frequency or ''}")
        live = sum(a.live for r in snap.routes.values() for a in r.arrivals)
        print(f"\nGPS-прогнозов: {live}.")
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


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--browser"]
    if len(args) != 1:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(args[0], "--browser" in sys.argv)))
