import asyncio
import datetime as dt
import html
import re
import time

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .db import Database
from .models import SavedStop, norm_route
from .stop_engine import SourceBlocked, StopMatcher
from .stopinfo import RouteForecast, StopSnapshot, extract_stop_id
from .store import VehicleStore

router = Router()

DIRS = [("⬆️ С", 0), ("↗️ СВ", 45), ("➡️ В", 90), ("↘️ ЮВ", 135),
        ("⬇️ Ю", 180), ("↙️ ЮЗ", 225), ("⬅️ З", 270), ("↖️ СЗ", 315)]
RADII = [("Близко ~400 м", 400), ("Стандартно ~800 м", 800), ("Заранее ~1500 м", 1500)]
DIR_NAME = {b: n for n, b in DIRS}


class Track(StatesGroup):
    route = State()
    location = State()
    direction = State()
    radius = State()
    mode = State()


LEADS = [3, 5, 7, 10, 15]
URL_RE = re.compile(r"https?://\S+")
YANDEX_LINK_RE = re.compile(r"yandex\.\w+/maps|stop__\d+", re.I)


class StopFlow(StatesGroup):
    pick = State()       # waiting for a location / link / saved stop
    near = State()       # choosing among the nearest stops
    route = State()
    lead = State()
    mode = State()
    naming = State()     # user types a name for the stop being saved


class SavedFlow(StatesGroup):
    rename = State()


class Access(BaseMiddleware):
    def __init__(self, allowed: frozenset[int]):
        self.allowed = allowed

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if self.allowed and (user is None or user.id not in self.allowed):
            if isinstance(event, Message):
                await event.answer(f"Доступ закрыт. Твой id: {user.id if user else '?'}")
            return None
        return await handler(event, data)


STOP_COMMANDS = [
    ("stop", "Новое оповещение о подходе автобуса"),
    ("stops", "Мои остановки и табло прибытия"),
    ("list", "Мои оповещения"),
    ("now", "Прогноз по моим оповещениям"),
    ("cancel", "Отменить текущее действие"),
    ("help", "Что умеет бот"),
]
TRACK_COMMANDS = [
    ("track", "Оповещение по точке на карте (геозона)"),
    ("where", "Где сейчас автобусы маршрута"),
]


def bot_commands(track_enabled: bool) -> list[BotCommand]:
    """The command menu shown by Telegram next to the input field."""
    cmds = STOP_COMMANDS[:-2] + (TRACK_COMMANDS if track_enabled else []) + STOP_COMMANDS[-2:]
    return [BotCommand(command=c, description=d) for c, d in cmds]


def help_text(track_enabled: bool) -> str:
    lines = [f"/{c} — {d[0].lower()}{d[1:]}" for c, d in STOP_COMMANDS[:-1]]
    if track_enabled:
        lines += ["/track — оповещение по точке на карте (геозона)", "/where &lt;маршрут&gt; — где сейчас автобусы"]
    return ("🚌 Бот предупреждает, когда нужный автобус подъезжает к остановке (прогноз Яндекс.Карт).\n\n"
            "<b>Быстро:</b> отправь геометку 📍 (кнопка внизу или 📎 → Геопозиция → метка на карте) "
            "или ссылку на остановку из Яндекс.Карт — покажу ближайшие остановки и прибытие.\n\n"
            + "\n".join(lines))


LOC_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Моя геопозиция", request_location=True)]],
                             resize_keyboard=True, one_time_keyboard=True)


@router.message(Command("start", "help"))
async def cmd_start(m: Message, state: FSMContext, track_enabled: bool = False):
    await state.clear()
    await m.answer(help_text(track_enabled), parse_mode="HTML", reply_markup=LOC_KB)


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("track"))
async def cmd_track(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Track.route)
    await m.answer("Номер маршрута? Например: 135")


@router.message(Track.route, F.text)
async def got_route(m: Message, state: FSMContext, store: VehicleStore):
    route = norm_route(m.text)
    if not route or len(route) > 12:
        await m.answer("Не похоже на номер маршрута, попробуй ещё раз.")
        return
    await state.update_data(route=route)
    await state.set_state(Track.location)
    known = store.routes(time.time())
    warn = "" if (route in known or not known) else "\n⚠️ Сейчас этого маршрута нет в данных — оповещение всё равно создам."
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Моя геопозиция", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True)
    await m.answer(
        f"Маршрут {html.escape(route)}.{warn}\nОтправь точку: кнопка ниже или 📎 → Геопозиция → выбери любую точку на карте.",
        reply_markup=kb)


@router.message(Track.location, F.location)
async def got_location(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)
    await state.set_state(Track.direction)
    b = InlineKeyboardBuilder()
    for name, deg in DIRS:
        b.button(text=name, callback_data=f"dir:{deg}")
    b.button(text="Любое / не знаю", callback_data="dir:any")
    b.adjust(4, 4, 1)
    await m.answer("Куда едет нужный автобус у этой точки (сторона света)?", reply_markup=b.as_markup())


@router.callback_query(Track.direction, F.data.startswith("dir:"))
async def got_direction(c: CallbackQuery, state: FSMContext):
    v = c.data.split(":")[1]
    await state.update_data(bearing=None if v == "any" else float(v))
    await state.set_state(Track.radius)
    b = InlineKeyboardBuilder()
    for name, r in RADII:
        b.button(text=name, callback_data=f"rad:{r}")
    b.adjust(1)
    await c.message.edit_text("За какое расстояние предупредить?", reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(Track.radius, F.data.startswith("rad:"))
async def got_radius(c: CallbackQuery, state: FSMContext):
    await state.update_data(radius=int(c.data.split(":")[1]))
    await state.set_state(Track.mode)
    b = InlineKeyboardBuilder()
    b.button(text="Один раз", callback_data="mode:once")
    b.button(text="Постоянно", callback_data="mode:rec")
    b.adjust(2)
    await c.message.edit_text("Сколько раз срабатывать?", reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(Track.mode, F.data.startswith("mode:"))
async def got_mode(c: CallbackQuery, state: FSMContext, db: Database):
    d = await state.get_data()
    await state.clear()
    rec = c.data.endswith("rec")
    title = f"{d['lat']:.4f}, {d['lon']:.4f}"
    tid = await db.add_trigger(c.message.chat.id, d["route"], title, d["lat"], d["lon"],
                               d["radius"], d["bearing"], rec)
    direction = DIR_NAME.get(d["bearing"], "любое") if d["bearing"] is not None else "любое"
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отключить", callback_data=f"del:{tid}")
    await c.message.edit_text(
        f"✅ Оповещение #{tid}\nМаршрут: {html.escape(d['route'])}\nТочка: {title}\n"
        f"Направление: {direction}\nРадиус: {d['radius']} м\nРежим: {'постоянно' if rec else 'один раз'}",
        reply_markup=b.as_markup())
    await c.message.answer("Готово.", reply_markup=ReplyKeyboardRemove())
    await c.answer()


@router.message(Command("list"))
async def cmd_list(m: Message, db: Database):
    ts = await db.user_triggers(m.chat.id)
    ss = await db.user_stop_triggers(m.chat.id)
    if not ts and not ss:
        await m.answer("Активных оповещений нет. /stop или /track — создать.")
        return
    b = InlineKeyboardBuilder()
    lines = []
    for t in ss:
        lines.append(f"🚏 О{t.id} · {html.escape(t.route_title)} · «{html.escape(t.stop_name)}» · за {t.lead_min} мин · "
                     f"{'постоянно' if t.recurring else 'один раз'}")
        b.button(text=f"❌ О{t.id}", callback_data=f"sdel:{t.id}")
    for t in ts:
        d = DIR_NAME.get(t.bearing, "любое") if t.bearing is not None else "любое"
        lines.append(f"📍 #{t.id} · маршрут {html.escape(t.route)} · {t.title} · {t.radius_m} м · {d} · "
                     f"{'постоянно' if t.recurring else 'один раз'}")
        b.button(text=f"❌ #{t.id}", callback_data=f"del:{t.id}")
    b.adjust(4)
    await m.answer("\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(c: CallbackQuery, db: Database):
    ok = await db.deactivate(int(c.data.split(":")[1]), c.message.chat.id)
    await c.answer("Отключено" if ok else "Уже отключено")
    if ok:
        await c.message.edit_reply_markup(reply_markup=None)


@router.message(Command("where"))
async def cmd_where(m: Message, store: VehicleStore, stale_after: float):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование: /where 135")
        return
    route = norm_route(parts[1])
    vs = store.by_route(route, time.time(), stale_after)
    if not vs:
        await m.answer(f"Маршрут {html.escape(route)}: свежих данных нет.")
        return
    lines = [f"<code>{html.escape(v.vehicle_id)}</code> {v.lat:.5f}, {v.lon:.5f}"
             + (f" · {v.heading:.0f}°" if v.heading is not None else "")
             + f" · {int(time.time() - v.ts)} с назад" for v in vs[:8]]
    await m.answer(f"Маршрут {html.escape(route)}, на линии: {len(vs)}\n" + "\n".join(lines), parse_mode="HTML")


# ---------------- stop (forecast) alarms ----------------

ARROW_LIST = ["⬆️", "↗️", "➡️", "↘️", "⬇️", "↙️", "⬅️", "↖️"]


def _fmt_eta(e: float) -> str:
    return "сейчас" if e < 0.5 else f"{round(e)} мин"


def _fmt_dist(m: float) -> str:
    return f"{round(m, -1):.0f} м" if m < 1000 else f"{m / 1000:.1f} км"


def _arrow(bearing: float) -> str:
    return ARROW_LIST[round(bearing / 45) % 8]


def _sorted_routes(snap: StopSnapshot) -> list[RouteForecast]:
    return sorted(snap.routes.values(), key=lambda r: (r.type, len(r.name), r.name))[:40]


def _board_line(snap: StopSnapshot, rf: RouteForecast, now: float) -> str:
    live = [(a, snap.eta_min(a, now)) for a in rf.arrivals if a.live]
    live = [x for x in live if x[1] >= -1.0]
    if live:
        return ", ".join(_fmt_eta(e) for _, e in live[:3])
    sch = [(a, snap.eta_min(a, now)) for a in rf.arrivals if not a.live]
    sch = [x for x in sch if x[1] >= -1.0]
    if sch:
        return ", ".join(_fmt_eta(e) for _, e in sch[:2]) + " (расп.)"
    return f"интервал {rf.frequency}" if rf.frequency else "нет прогноза"


async def _load(wait: Message, coro) -> StopSnapshot | None:
    """Await a source call; on failure turn `wait` into a readable error and return None."""
    try:
        return await coro
    except SourceBlocked as e:
        await _edit(wait, f"⛔ {html.escape(str(e))}. Попробуй позже.")
    except (TimeoutError, asyncio.TimeoutError, ConnectionError):
        await _edit(wait, "Яндекс не ответил вовремя. Попробуй ещё раз через минуту.")
    except ValueError as e:
        await _edit(wait, f"Не получилось: {html.escape(str(e))}")
    return None


async def _edit(msg: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:   # "message is not modified" on a refresh within the same second
        if "not modified" not in str(e):
            raise


async def _wait_msg(m: Message, text: str) -> Message:
    return await m.answer(text)


async def _show_stop(target: Message, state: FSMContext, db: Database, stop_matcher: StopMatcher,
                     snap: StopSnapshot, url: str, *, edit: bool = True, back_to_near: bool = False) -> None:
    """Stop card with one button per route (+ save / back). Puts the dialog into StopFlow.route."""
    saved = next((s for s in await db.saved_stops(target.chat.id) if s.stop_id == snap.stop_id), None)
    head = (f"🚏 <b>{html.escape(saved.title)}</b> · «{html.escape(snap.name)}»" if saved and saved.title != snap.name
            else f"🚏 «{html.escape(snap.name)}»")
    b = InlineKeyboardBuilder()
    if not snap.routes:
        text = f"{head}\nНет маршрутов в ответе Яндекса."
    else:
        now = time.time()
        opts = []
        for i, rf in enumerate(_sorted_routes(snap)):
            up = stop_matcher.upcoming(snap, rf.key, now)
            tail = f" · {_fmt_eta(up[0][1])}" if up else ""
            opts.append((rf.key, rf.title))
            b.button(text=f"{rf.title}{tail}", callback_data=f"sr:{i}")
        await state.update_data(opts=opts)
        live = any(a.live for r in snap.routes.values() for a in r.arrivals)
        note = "" if live else "\n⚠️ Сейчас нет GPS-прогнозов — возможно, ночь или Яндекс не получает данные."
        text = f"{head}\nКакой маршрут ждём? (на кнопке — ближайшее прибытие){note}"
    b.adjust(2)
    extra = []
    if not saved:
        extra.append(("⭐ Сохранить остановку", "ssave"))
    if back_to_near:
        extra.append(("‹ Другие остановки", "snb"))
    for t, cbd in extra:
        b.row(InlineKeyboardButton(text=t, callback_data=cbd))
    await state.update_data(stop_id=snap.stop_id, stop_name=snap.name, stop_url=url,
                            stop_lat=snap.lat, stop_lon=snap.lon, back_to_near=back_to_near)
    await state.set_state(StopFlow.route)
    if edit:
        await _edit(target, text, b.as_markup())
    else:
        await target.answer(text, reply_markup=b.as_markup())


PICK_TEXT = ("Где ждёшь автобус?\n"
             "📍 Нажми «Моя геопозиция» внизу или 📎 → Геопозиция → поставь метку на карте — покажу ближайшие остановки.\n"
             "🔗 Или пришли ссылку на остановку из Яндекс.Карт (нажми на остановку → Поделиться).")


async def _ask_pick(m: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    await state.set_state(StopFlow.pick)
    saved = await db.saved_stops(m.chat.id)
    if saved:
        b = InlineKeyboardBuilder()
        for s in saved:
            b.button(text=f"⭐ {s.title}", callback_data=f"spk:{s.id}")
        b.adjust(2)
        await m.answer("Мои остановки:", reply_markup=b.as_markup())
    await m.answer(PICK_TEXT, reply_markup=LOC_KB)


@router.message(Command("stop"))
async def cmd_stop(m: Message, state: FSMContext, db: Database, stop_source):
    await state.clear()
    if stop_source is None:
        await m.answer("Режим остановок выключен (STOP_SOURCE=none).")
        return
    await _ask_pick(m, state, db)


def _near_kb(near: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, (_sid, name, dist, bearing) in enumerate(near):
        b.button(text=f"{_arrow(bearing)} {name} · {_fmt_dist(dist)}", callback_data=f"sn:{i}")
    b.adjust(1)
    return b.as_markup()


NEAR_TEXT = ("Ближайшие остановки (стрелка — направление от метки).\n"
             "Одинаковые названия — обычно разные стороны дороги: смотри маршруты внутри.")


@router.message(StateFilter(StopFlow.pick, None), F.location)
async def stop_by_location(m: Message, state: FSMContext, stop_source):
    if stop_source is None:
        await m.answer("Режим остановок выключен (STOP_SOURCE=none).")
        return
    if not hasattr(stop_source, "nearby"):
        await m.answer("Поиск по геометке работает с STOP_SOURCE=yandex. Пришли ссылку на остановку.")
        return
    wait = await _wait_msg(m, "🔎 Ищу остановки рядом с меткой…")
    near = await _load(wait, stop_source.nearby(m.location.latitude, m.location.longitude))
    if near is None:
        return
    if not near:
        await _edit(wait, "Рядом с меткой (до 1.5 км) остановок не нашёл. Поставь метку ближе к остановке "
                          "или пришли ссылку из Яндекс.Карт.")
        await state.set_state(StopFlow.pick)
        return
    data = [(s.stop_id, s.name, s.dist_m, s.bearing) for s in near]
    await state.clear()
    await state.update_data(near=data)
    await state.set_state(StopFlow.near)
    await _edit(wait, NEAR_TEXT, _near_kb(data))


@router.callback_query(F.data == "snb")
async def cb_back_to_near(c: CallbackQuery, state: FSMContext):
    near = (await state.get_data()).get("near")
    if not near:
        await c.answer("Список устарел — отправь метку ещё раз")
        return
    await state.set_state(StopFlow.near)
    await _edit(c.message, NEAR_TEXT, _near_kb(near))
    await c.answer()


@router.callback_query(StopFlow.near, F.data.startswith("sn:"))
async def cb_near_pick(c: CallbackQuery, state: FSMContext, db: Database, stop_source, stop_matcher: StopMatcher):
    near = (await state.get_data()).get("near") or []
    i = int(c.data.split(":")[1])
    if i >= len(near):
        await c.answer("Список устарел")
        return
    sid, name = near[i][0], near[i][1]
    await c.answer()
    await _edit(c.message, f"⏳ Загружаю «{html.escape(name)}»…")
    snap = await _load(c.message, stop_source.get(sid))
    if snap is not None:
        await _show_stop(c.message, state, db, stop_matcher, snap, stop_source.stop_url(sid), back_to_near=True)


async def _stop_by_text(m: Message, state: FSMContext, db: Database, stop_source, stop_matcher: StopMatcher) -> None:
    found = URL_RE.search(m.text)
    if found:
        url = found.group(0)
    elif extract_stop_id(m.text):
        url = f"https://yandex.ru/maps/?masstransit%5BstopId%5D={extract_stop_id(m.text)}"
    else:
        await m.answer("Не вижу ссылки. Отправь геометку 📍, ссылку на остановку из Яндекс.Карт или /cancel.")
        return
    wait = await _wait_msg(m, "🔗 Открываю остановку…")
    res = await _load(wait, stop_source.resolve(url))
    if res is not None:
        snap, watch_url = res
        await state.clear()
        await _show_stop(wait, state, db, stop_matcher, snap, watch_url)


@router.message(StopFlow.pick, F.text)
async def got_stop_link(m: Message, state: FSMContext, db: Database, stop_source, stop_matcher: StopMatcher):
    await _stop_by_text(m, state, db, stop_source, stop_matcher)


@router.message(StateFilter(None), F.text.regexp(YANDEX_LINK_RE, mode="search"))
async def link_without_command(m: Message, state: FSMContext, db: Database, stop_source, stop_matcher: StopMatcher):
    if stop_source is None:
        return
    await _stop_by_text(m, state, db, stop_source, stop_matcher)


@router.callback_query(F.data.startswith("spk:"))
async def cb_saved_pick(c: CallbackQuery, state: FSMContext, db: Database, stop_source, stop_matcher: StopMatcher):
    s = await db.saved_stop(int(c.data.split(":")[1]), c.message.chat.id)
    if s is None or stop_source is None:
        await c.answer("Остановка удалена" if s is None else "Режим остановок выключен")
        return
    await c.answer()
    await _edit(c.message, f"⏳ Загружаю «{html.escape(s.title)}»…")
    snap = await _load(c.message, stop_source.get(s.stop_id, s.stop_url))
    if snap is not None:
        await state.clear()
        await _show_stop(c.message, state, db, stop_matcher, snap, s.stop_url)


# ---- saving a stop under the user's own name

@router.callback_query(StopFlow.route, F.data == "ssave")
async def cb_save_stop(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.set_state(StopFlow.naming)
    b = InlineKeyboardBuilder()
    b.button(text=f"Оставить «{d['stop_name']}»", callback_data="sname:keep")
    await _edit(c.message, f"Как назвать остановку «{html.escape(d['stop_name'])}»?\n"
                           "Напиши название, например: Дом, Универ, Работа.", b.as_markup())
    await c.answer()


async def _save_and_show(target: Message, state: FSMContext, db: Database, stop_source, stop_matcher: StopMatcher,
                         title: str, edit: bool) -> None:
    d = await state.get_data()
    await db.save_stop(target.chat.id, d["stop_id"], d["stop_url"], title, d["stop_name"],
                       d.get("stop_lat"), d.get("stop_lon"))
    note = f"⭐ Сохранено: «{html.escape(title)}». Она в /stops и первой кнопкой в /stop."
    if edit:
        await _edit(target, note)
    else:
        await target.answer(note)
    wait = await target.answer("⏳")
    snap = await _load(wait, stop_source.get(d["stop_id"], d["stop_url"], max_age=300))
    if snap is not None:
        await _show_stop(wait, state, db, stop_matcher, snap, d["stop_url"], back_to_near=d.get("back_to_near", False))


@router.callback_query(StopFlow.naming, F.data == "sname:keep")
async def cb_name_keep(c: CallbackQuery, state: FSMContext, db: Database, stop_source, stop_matcher: StopMatcher):
    await c.answer()
    await _save_and_show(c.message, state, db, stop_source, stop_matcher, (await state.get_data())["stop_name"],
                         edit=True)


@router.message(StopFlow.naming, F.text)
async def got_stop_name(m: Message, state: FSMContext, db: Database, stop_source, stop_matcher: StopMatcher):
    title = " ".join(m.text.split())[:40]
    if not title:
        await m.answer("Название пустое — напиши текстом.")
        return
    await _save_and_show(m, state, db, stop_source, stop_matcher, title, edit=False)


# ---- route → lead → mode → alarm

@router.callback_query(StopFlow.route, F.data.startswith("sr:"))
async def got_stop_route(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    key, title = d["opts"][int(c.data.split(":")[1])]
    await state.update_data(route_key=key, route_title=title)
    await state.set_state(StopFlow.lead)
    b = InlineKeyboardBuilder()
    for v in LEADS:
        b.button(text=f"за {v} мин", callback_data=f"sl:{v}")
    b.adjust(5)
    await c.message.edit_text(f"{html.escape(title)}. За сколько минут до прибытия предупредить?",
                              reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(StopFlow.lead, F.data.startswith("sl:"))
async def got_stop_lead(c: CallbackQuery, state: FSMContext):
    await state.update_data(lead=int(c.data.split(":")[1]))
    await state.set_state(StopFlow.mode)
    b = InlineKeyboardBuilder()
    b.button(text="Один раз", callback_data="sm:once")
    b.button(text="Постоянно", callback_data="sm:rec")
    b.adjust(2)
    await c.message.edit_text("Сколько раз срабатывать?", reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(StopFlow.mode, F.data.startswith("sm:"))
async def got_stop_mode(c: CallbackQuery, state: FSMContext, db: Database):
    d = await state.get_data()
    await state.clear()
    rec = c.data.endswith("rec")
    tid = await db.add_stop_trigger(c.message.chat.id, d["stop_id"], d["stop_url"], d["stop_name"],
                                    d["route_key"], d["route_title"], d["lead"], rec)
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отключить", callback_data=f"sdel:{tid}")
    await c.message.edit_text(
        f"✅ Оповещение О{tid}\n🚏 {html.escape(d['stop_name'])}\n{html.escape(d['route_title'])}, "
        f"за {d['lead']} мин\nРежим: {'постоянно' if rec else 'один раз'}\n\nТекущий прогноз: /now",
        reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("sdel:"))
async def cb_stop_delete(c: CallbackQuery, db: Database):
    ok = await db.deactivate_stop(int(c.data.split(":")[1]), c.message.chat.id)
    await c.answer("Отключено" if ok else "Уже отключено")
    if ok:
        await c.message.edit_reply_markup(reply_markup=None)


# ---------------- saved stops: /stops ----------------

def _saved_list(saved: list[SavedStop]) -> tuple[str, InlineKeyboardMarkup]:
    b = InlineKeyboardBuilder()
    if not saved:
        text = ("Сохранённых остановок пока нет.\n"
                "Отправь геометку 📍 или ссылку на остановку из Яндекс.Карт и нажми «⭐ Сохранить остановку».")
    else:
        text = "⭐ Мои остановки — нажми, чтобы открыть табло:"
        for s in saved:
            label = s.title if s.title == s.yandex_name else f"{s.title} · {s.yandex_name}"
            b.button(text=label, callback_data=f"sv:{s.id}")
    b.button(text="➕ Добавить остановку", callback_data="sadd")
    b.adjust(1)
    return text, b.as_markup()


@router.message(Command("stops"))
async def cmd_stops(m: Message, state: FSMContext, db: Database):
    await state.clear()
    text, kb = _saved_list(await db.saved_stops(m.chat.id))
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data == "svl")
async def cb_saved_list(c: CallbackQuery, db: Database):
    text, kb = _saved_list(await db.saved_stops(c.message.chat.id))
    await _edit(c.message, text, kb)
    await c.answer()


@router.callback_query(F.data == "sadd")
async def cb_saved_add(c: CallbackQuery, state: FSMContext, db: Database):
    await c.answer()
    await _ask_pick(c.message, state, db)


def _board(s: SavedStop, snap: StopSnapshot) -> str:
    now = time.time()
    head = f"⭐ <b>{html.escape(s.title)}</b>" + ("" if s.title == snap.name else f" · «{html.escape(snap.name)}»")
    rows = [f"{html.escape(rf.title)} — {_board_line(snap, rf, now)}" for rf in _sorted_routes(snap)]
    at = dt.datetime.fromtimestamp(snap.fetched_at).strftime("%H:%M:%S")
    return "\n".join([head, *(rows or ["нет маршрутов"]), f"\nобновлено {at}"])


def _board_kb(sid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔔 Оповещение", callback_data=f"sva:{sid}")
    b.button(text="🔄 Обновить", callback_data=f"sv:{sid}")
    b.button(text="✏️ Переименовать", callback_data=f"svr:{sid}")
    b.button(text="🗑 Удалить", callback_data=f"svd:{sid}")
    b.button(text="‹ Все остановки", callback_data="svl")
    b.adjust(2, 2, 1)
    return b.as_markup()


@router.callback_query(F.data.startswith("sv:"))
async def cb_saved_board(c: CallbackQuery, db: Database, stop_source):
    s = await db.saved_stop(int(c.data.split(":")[1]), c.message.chat.id)
    if s is None:
        await c.answer("Остановка удалена")
        return
    await c.answer()
    if stop_source is None:
        return
    snap = await _load(c.message, stop_source.get(s.stop_id, s.stop_url, max_age=15))
    if snap is not None:
        await _edit(c.message, _board(s, snap), _board_kb(s.id))


@router.callback_query(F.data.startswith("sva:"))
async def cb_saved_alarm(c: CallbackQuery, state: FSMContext, db: Database, stop_source, stop_matcher: StopMatcher):
    s = await db.saved_stop(int(c.data.split(":")[1]), c.message.chat.id)
    if s is None or stop_source is None:
        await c.answer("Остановка удалена" if s is None else "Режим остановок выключен")
        return
    await c.answer()
    snap = await _load(c.message, stop_source.get(s.stop_id, s.stop_url, max_age=60))
    if snap is not None:
        await state.clear()
        await _show_stop(c.message, state, db, stop_matcher, snap, s.stop_url)


@router.callback_query(F.data.startswith("svr:"))
async def cb_saved_rename(c: CallbackQuery, state: FSMContext, db: Database):
    s = await db.saved_stop(int(c.data.split(":")[1]), c.message.chat.id)
    if s is None:
        await c.answer("Остановка удалена")
        return
    await state.clear()
    await state.update_data(saved_id=s.id)
    await state.set_state(SavedFlow.rename)
    await c.message.answer(f"Новое название для «{html.escape(s.title)}»?")
    await c.answer()


@router.message(SavedFlow.rename, F.text)
async def got_saved_rename(m: Message, state: FSMContext, db: Database):
    title = " ".join(m.text.split())[:40]
    sid = (await state.get_data()).get("saved_id")
    await state.clear()
    if not title or sid is None:
        await m.answer("Не переименовано.")
        return
    await db.rename_saved_stop(sid, m.chat.id, title)
    text, kb = _saved_list(await db.saved_stops(m.chat.id))
    await m.answer(f"✏️ Теперь «{html.escape(title)}».\n\n{text}", reply_markup=kb)


@router.callback_query(F.data.startswith("svd:"))
async def cb_saved_delete_ask(c: CallbackQuery, db: Database):
    s = await db.saved_stop(int(c.data.split(":")[1]), c.message.chat.id)
    if s is None:
        await c.answer("Уже удалена")
        return
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Да, удалить", callback_data=f"svx:{s.id}")
    b.button(text="Отмена", callback_data=f"sv:{s.id}")
    b.adjust(2)
    await _edit(c.message, f"Удалить «{html.escape(s.title)}» из моих остановок? Оповещения по ней останутся.",
                b.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("svx:"))
async def cb_saved_delete(c: CallbackQuery, db: Database):
    await db.delete_saved_stop(int(c.data.split(":")[1]), c.message.chat.id)
    text, kb = _saved_list(await db.saved_stops(c.message.chat.id))
    await _edit(c.message, text, kb)
    await c.answer("Удалено")


# ---------------- /now ----------------

@router.message(Command("now"))
async def cmd_now(m: Message, db: Database, stop_source, stop_matcher: StopMatcher):
    ss = await db.user_stop_triggers(m.chat.id)
    if not ss:
        await m.answer("Нет оповещений по остановкам. /stop — создать, /stops — табло сохранённых остановок.")
        return
    if stop_source is None:
        await m.answer("Режим остановок выключен (STOP_SOURCE=none).")
        return
    urls = {t.stop_id: t.stop_url for t in ss}
    res = await asyncio.gather(*(stop_source.get(sid, url, max_age=60) for sid, url in urls.items()),
                               return_exceptions=True)
    snaps = {sid: (r if isinstance(r, StopSnapshot) else None) for sid, r in zip(urls, res)}
    now = time.time()
    out = []
    for t in ss:
        snap = snaps.get(t.stop_id) or stop_source.snapshot(t.stop_id)
        head = f"🚏 О{t.id} «{html.escape(t.stop_name)}» · {html.escape(t.route_title)}"
        if snap is None:
            out.append(f"{head}\n   данных нет — Яндекс не ответил")
            continue
        up = stop_matcher.upcoming(snap, t.route_key, now)
        etas = ", ".join(_fmt_eta(e) + ("" if a.live else " (расп.)") for a, e in up[:3]) or "прогноза нет"
        out.append(f"{head}\n   {etas} · данные {int(now - snap.fetched_at)} с назад")
    p = stop_source.problem()
    if p:
        out.append(f"\n⚠️ {html.escape(p)}")
    await m.answer("\n".join(out))
