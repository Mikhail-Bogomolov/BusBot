import asyncio
import html
import logging

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from . import config
from .db import Database
from .engine import Engine
from .handlers import Access, bot_commands, router
from .matcher import Matcher
from .models import Hit
from .sources.kgt import KgtSource
from .sources.mock import MockSource
from .sources.mock_stops import MockStopSource
from .sources.yandex import YandexStopSource
from .sources.yandex_http import YandexHttpSource
from .stop_engine import StopEngine, StopHit, StopMatcher
from .store import VehicleStore

log = logging.getLogger("busbot")


def fmt_stop_hit(h: StopHit) -> str:
    t = h.trigger
    when = "прямо сейчас" if h.eta_min < 0.5 else f"через ~{round(h.eta_min)} мин"
    src = "по GPS" if h.arrival.live else "по расписанию"
    nxt = f"\nСледующий: через ~{round(h.next_eta_min)} мин" if h.next_eta_min is not None else ""
    return f"🚌 <b>{html.escape(t.route_title)}</b> будет на «{html.escape(t.stop_name)}» {when} ({src}){nxt}"


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()

    db = Database(cfg.db_path)
    await db.open()
    store = VehicleStore()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    client = httpx.AsyncClient(timeout=10, headers={"User-Agent": "busbot-personal/0.2 (5 testers)"})

    async def alert(text: str) -> None:
        if cfg.admin_id:
            try:
                await bot.send_message(cfg.admin_id, text)
            except TelegramAPIError:
                pass

    tasks: list[asyncio.Task] = []

    # ---- position mode (/track)
    if cfg.source == "kgt":
        source = KgtSource(cfg.kgt, client)
    elif cfg.source == "mock":
        source = MockSource()
    elif cfg.source == "none":
        source = None
    else:
        raise RuntimeError(f"unknown SOURCE={cfg.source!r} (mock|kgt|none)")

    async def notify(h: Hit) -> None:
        t, v = h.trigger, h.vehicle
        text = (f"🔔 <b>Автобус №{html.escape(t.route)} приближается</b>\n"
                f"Борт: <code>{html.escape(v.vehicle_id)}</code>\n"
                f"До точки: ~{round(h.dist_m, -1):.0f} м\n"
                f"Ожидание: ~{h.eta_min} мин")
        try:
            await bot.send_message(t.chat_id, text)
            await bot.send_location(t.chat_id, v.lat, v.lon)
        except TelegramAPIError as e:
            log.warning("send to %s failed: %r", t.chat_id, e)

    if source is not None:
        engine = Engine(source, store, db, Matcher(), notify, interval=cfg.poll_interval,
                        stale_after=cfg.stale_after, alert=alert)
        tasks.append(asyncio.create_task(engine.run()))

    # ---- stop forecast mode (/stop)
    if cfg.stop_source == "yandex":            # plain HTTP, ~1 s per stop
        stop_source = YandexHttpSource(cfg.yandex)
    elif cfg.stop_source == "yandex_browser":  # headless Chromium, fallback if the HTML stops carrying data
        stop_source = YandexStopSource(cfg.yandex)
    elif cfg.stop_source == "mock":
        stop_source = MockStopSource()
    elif cfg.stop_source == "none":
        stop_source = None
    else:
        raise RuntimeError(f"unknown STOP_SOURCE={cfg.stop_source!r} (yandex|yandex_browser|mock|none)")
    stop_matcher = StopMatcher(use_scheduled=cfg.use_scheduled)

    async def notify_stop(h: StopHit) -> None:
        try:
            await bot.send_message(h.trigger.chat_id, fmt_stop_hit(h))
        except TelegramAPIError as e:
            log.warning("send to %s failed: %r", h.trigger.chat_id, e)

    if stop_source is not None:
        await stop_source.start()
        stop_engine = StopEngine(stop_source, db, stop_matcher, notify_stop, interval=cfg.stop_interval,
                                 stale_after=cfg.stop_stale, alert=alert)
        tasks.append(asyncio.create_task(stop_engine.run()))

    dp = Dispatcher(storage=MemoryStorage())
    dp["db"], dp["store"], dp["stale_after"] = db, store, cfg.stale_after
    dp["stop_source"], dp["stop_matcher"] = stop_source, stop_matcher
    dp["track_enabled"] = track_enabled = cfg.source == "kgt"   # mock positions are for development only
    access = Access(cfg.allowed_users)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(router)

    log.info("started: source=%s stop_source=%s users=%s", cfg.source, cfg.stop_source,
             sorted(cfg.allowed_users) or "all")
    try:
        await bot.set_my_commands(bot_commands(track_enabled))
        await bot.set_my_short_description("Предупреждаю, когда нужный автобус подъезжает к остановке")
        await bot.set_my_description(
            "Отправь геометку или ссылку на остановку из Яндекс.Карт — покажу ближайшие остановки и прибытие "
            "автобусов, а когда нужный будет в N минутах — пришлю уведомление.")
    except TelegramAPIError as e:
        log.warning("set_my_commands failed: %r", e)
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        if stop_source is not None:
            await stop_source.close()
        await client.aclose()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
