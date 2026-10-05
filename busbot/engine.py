import asyncio
import logging
import time
from typing import Awaitable, Callable

from .db import Database
from .matcher import Matcher
from .models import Hit
from .sources import Source
from .store import VehicleStore

log = logging.getLogger("engine")


class Engine:
    def __init__(self, source: Source, store: VehicleStore, db: Database, matcher: Matcher,
                 notify: Callable[[Hit], Awaitable[None]], *, interval: float = 20.0,
                 stale_after: float = 120.0, alert: Callable[[str], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.time, alert_after: int = 5):
        self.source, self.store, self.db, self.matcher = source, store, db, matcher
        self.notify, self.alert, self.clock = notify, alert, clock
        self.interval, self.stale_after, self.alert_after = interval, stale_after, alert_after
        self.fails = 0
        self._alerted = False

    async def tick(self) -> list[Hit]:
        vehicles = await self.source.fetch()
        self.store.update(vehicles)
        now = self.clock()
        triggers = await self.db.active_triggers()
        hits = self.matcher.check(triggers, self.store.fresh(now, self.stale_after), now)
        done_once: set[int] = set()
        sent: list[Hit] = []
        for h in hits:
            if h.trigger.id in done_once:
                continue
            await self.notify(h)
            sent.append(h)
            if not h.trigger.recurring:
                done_once.add(h.trigger.id)
                await self.db.deactivate(h.trigger.id)
        return sent

    async def run(self) -> None:
        while True:
            delay = self.interval
            try:
                await self.tick()
                if self._alerted and self.alert:
                    await self.alert("✅ Источник данных снова отвечает")
                self.fails, self._alerted = 0, False
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network, bad JSON, schema drift
                self.fails += 1
                delay = min(300.0, self.interval * 2 ** min(self.fails, 5))
                log.warning("tick failed (%d): %r, retry in %.0fs", self.fails, e, delay)
                if self.fails >= self.alert_after and not self._alerted and self.alert:
                    self._alerted = True
                    await self.alert(f"⚠️ Источник данных недоступен ({self.fails} ошибок подряд): {e!r}")
            await asyncio.sleep(delay)
