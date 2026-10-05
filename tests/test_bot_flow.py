"""Drives the real aiogram Dispatcher through the full /track dialog with a stubbed network session."""
import asyncio
import datetime as dt

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageReplyMarkup, EditMessageText, SendLocation, SendMessage
from aiogram.types import CallbackQuery, Chat, Location, Message, Update, User

from busbot.db import Database
from busbot.handlers import Access, router
from busbot.models import Vehicle
from busbot.store import VehicleStore
from busbot.stop_engine import StopMatcher
from busbot.sources.mock_stops import MockStopSource

CHAT, USER = 777, 777
U = User(id=USER, is_bot=False, first_name="T")
C = Chat(id=CHAT, type="private")


class Stub(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self): ...

    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(message_id=len(self.calls) + 100, date=dt.datetime.now(), chat=C, text="x").as_(bot)
        return True


def msg(mid, **kw):
    return Message(message_id=mid, date=dt.datetime.now(), chat=C, from_user=U, **kw)


def test_full_track_flow(tmp_path):
    async def run():
        db = Database(str(tmp_path / "f.sqlite3"))
        await db.open()
        store = VehicleStore()
        store.update([Vehicle("b", "135", 56.0, 92.0, 90.0, 30.0, __import__("time").time())])
        sess = Stub()
        bot = Bot("1:TEST", session=sess)
        dp = Dispatcher(storage=MemoryStorage())
        dp["db"], dp["store"], dp["stale_after"] = db, store, 120.0
        dp["stop_source"], dp["stop_matcher"] = None, StopMatcher()
        acc = Access(frozenset({USER}))
        dp.message.outer_middleware(acc)
        dp.callback_query.outer_middleware(acc)
        router._parent_router = None   # module-level router reused across tests
        dp.include_router(router)

        n = [0]

        async def feed(**kw):
            n[0] += 1
            await dp.feed_update(bot, Update(update_id=n[0], **kw))

        async def cb(data):
            n[0] += 1
            await dp.feed_update(bot, Update(update_id=n[0], callback_query=CallbackQuery(
                id=str(n[0]), from_user=U, chat_instance="ci", data=data, message=msg(50, text="menu"))))

        await feed(message=msg(1, text="/track"))
        await feed(message=msg(2, text="Автобус 135"))
        await feed(message=msg(3, location=Location(latitude=56.01, longitude=92.89)))
        await cb("dir:90")
        await cb("rad:800")
        await cb("mode:rec")

        ts = await db.active_triggers()
        assert len(ts) == 1
        t = ts[0]
        assert (t.chat_id, t.route, t.radius_m, t.bearing, t.recurring) == (CHAT, "135", 800, 90.0, True)
        assert abs(t.lat - 56.01) < 1e-9

        await feed(message=msg(4, text="/list"))
        await feed(message=msg(5, text="/where 135"))
        sent = [c for c in sess.calls if isinstance(c, SendMessage)]
        assert any("📍 #1" in c.text for c in sent)                 # /list
        assert any("на линии: 1" in c.text for c in sent)           # /where

        await cb("del:1")
        assert await db.active_triggers() == []

        # access control
        other = User(id=999, is_bot=False, first_name="X")
        before = len(sess.calls)
        await feed(message=Message(message_id=9, date=dt.datetime.now(), chat=Chat(id=999, type="private"),
                                   from_user=other, text="/track"))
        assert "Доступ закрыт" in sess.calls[before].text
        await db.close()

    asyncio.run(run())


def test_stop_flow_with_mock_source(tmp_path):
    async def run():
        db = Database(str(tmp_path / "s.sqlite3"))
        await db.open()
        sess = Stub()
        bot = Bot("1:TEST", session=sess)
        dp = Dispatcher(storage=MemoryStorage())
        src = MockStopSource()
        dp["db"], dp["store"], dp["stale_after"] = db, VehicleStore(), 120.0
        dp["stop_source"], dp["stop_matcher"] = src, StopMatcher()
        router._parent_router = None   # module-level router reused across tests
        dp.include_router(router)
        n = [0]

        async def feed(text):
            n[0] += 1
            await dp.feed_update(bot, Update(update_id=n[0], message=msg(n[0], text=text)))

        async def cb(data):
            n[0] += 1
            await dp.feed_update(bot, Update(update_id=n[0], callback_query=CallbackQuery(
                id=str(n[0]), from_user=U, chat_instance="ci", data=data, message=msg(50, text="menu"))))

        await feed("/stop")
        await feed("вот https://yandex.ru/maps/62/krasnoyarsk/?masstransit%5BstopId%5D=stop__4242&z=19")
        edits = [c for c in sess.calls if isinstance(c, EditMessageText)]
        assert "Тестовая остановка 4242" in edits[-1].text
        buttons = [b.text for row in edits[-1].reply_markup.inline_keyboard for b in row]
        assert any(b.startswith("Автобус 135") for b in buttons) and any("Троллейбус 7" in b for b in buttons)
        idx = next(i for i, b in enumerate(buttons) if b.startswith("Автобус 99"))
        await cb(f"sr:{idx}")
        await cb("sl:7")
        await cb("sm:once")
        ts = await db.active_stop_triggers()
        assert len(ts) == 1
        t = ts[0]
        assert (t.stop_id, t.route_key, t.route_title, t.lead_min, t.recurring) == \
               ("stop__4242", "99|bus", "Автобус 99", 7, False)

        await src.sync({t.stop_id: t.stop_url})
        await feed("/now")
        await feed("/list")
        texts = [c.text for c in sess.calls if isinstance(c, SendMessage)]
        assert any("О1" in x and "мин" in x for x in texts)          # /now with ETAs
        assert any("🚏 О1" in x for x in texts)                       # /list

        await feed("/stop")
        await feed("просто текст")
        assert "Не вижу ссылки" in [c for c in sess.calls if isinstance(c, SendMessage)][-1].text

        await cb("sdel:1")
        assert await db.active_stop_triggers() == []
        await db.close()

    asyncio.run(run())


def _stop_dp(db, sess):
    bot = Bot("1:TEST", session=sess)
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"], dp["store"], dp["stale_after"] = db, VehicleStore(), 120.0
    dp["stop_source"], dp["stop_matcher"] = MockStopSource(), StopMatcher()
    router._parent_router = None   # module-level router reused across tests
    dp.include_router(router)
    n = [0]

    async def feed(**kw):
        n[0] += 1
        await dp.feed_update(bot, Update(update_id=n[0], message=msg(n[0], **kw)))

    async def cb(data):
        n[0] += 1
        await dp.feed_update(bot, Update(update_id=n[0], callback_query=CallbackQuery(
            id=str(n[0]), from_user=U, chat_instance="ci", data=data, message=msg(50, text="menu"))))

    return feed, cb


def _buttons(call):
    return [(b.text, b.callback_data) for row in call.reply_markup.inline_keyboard for b in row]


def test_location_save_and_saved_stops(tmp_path):
    async def run():
        db = Database(str(tmp_path / "v.sqlite3"))
        await db.open()
        sess = Stub()
        feed, cb = _stop_dp(db, sess)
        last_edit = lambda: [c for c in sess.calls if isinstance(c, EditMessageText)][-1]   # noqa: E731
        last_send = lambda: [c for c in sess.calls if isinstance(c, SendMessage)][-1]      # noqa: E731

        # a location sent without any command -> nearest stops
        await feed(location=Location(latitude=55.998, longitude=92.796))
        near = _buttons(last_edit())
        assert [d for _, d in near] == ["sn:0", "sn:1", "sn:2"]
        assert "Тестовая остановка 9001" in near[0][0] and " м" in near[0][0]

        await cb("sn:1")
        card = last_edit()
        assert "Тестовая остановка 9002" in card.text
        btns = _buttons(card)
        assert ("⭐ Сохранить остановку", "ssave") in btns and ("‹ Другие остановки", "snb") in btns
        await cb("snb")                                     # back to the list and pick another
        assert [d for _, d in _buttons(last_edit())][0] == "sn:0"
        await cb("sn:0")

        await cb("ssave")
        assert "Как назвать" in last_edit().text
        await feed(text="  Дом  ")
        saved = await db.saved_stops(CHAT)
        assert [(s.stop_id, s.title, s.yandex_name) for s in saved] == [("stop__9001", "Дом", "Тестовая остановка 9001")]
        card = last_edit()                                  # stop card again, now under the user's name
        assert "Дом" in card.text and ("⭐ Сохранить остановку", "ssave") not in _buttons(card)

        # alarm straight from that card
        idx = next(i for i, (t, _) in enumerate(_buttons(card)) if t.startswith("Автобус 135"))
        await cb(f"sr:{idx}")
        await cb("sl:5")
        await cb("sm:rec")
        ts = await db.active_stop_triggers()
        assert [(t.stop_id, t.route_title, t.lead_min) for t in ts] == [("stop__9001", "Автобус 135", 5)]

        # /stop starts with the saved stops
        await feed(text="/stop")
        sends = [c for c in sess.calls if isinstance(c, SendMessage)]
        assert ("⭐ Дом", f"spk:{saved[0].id}") in _buttons(sends[-2])
        await cb(f"spk:{saved[0].id}")
        assert "Дом" in last_edit().text and "Какой маршрут" in last_edit().text

        # /stops -> board -> rename -> delete
        await feed(text="/stops")
        assert ("Дом · Тестовая остановка 9001", f"sv:{saved[0].id}") in _buttons(last_send())
        await cb(f"sv:{saved[0].id}")
        board = last_edit()
        assert "Автобус 135 — " in board.text and "мин" in board.text and "обновлено" in board.text
        await cb(f"svr:{saved[0].id}")
        await feed(text="Универ")
        assert (await db.saved_stops(CHAT))[0].title == "Универ"
        await cb(f"svd:{saved[0].id}")
        assert "Удалить «Универ»" in last_edit().text
        await cb(f"svx:{saved[0].id}")
        assert await db.saved_stops(CHAT) == []
        assert "пока нет" in last_edit().text
        assert len(await db.active_stop_triggers()) == 1   # alarms survive deleting the saved stop

        # a Yandex link without /stop also works; plain text does not trigger anything
        before = len(sess.calls)
        await feed(text="https://yandex.ru/maps/62/krasnoyarsk/stops/stop__4242/")
        assert "Тестовая остановка 4242" in last_edit().text
        await feed(text="привет")
        assert not any(isinstance(c, SendMessage) and "привет" in c.text for c in sess.calls[before:])
        await db.close()

    asyncio.run(run())


def test_bot_commands_menu():
    from busbot.handlers import bot_commands, help_text
    names = [c.command for c in bot_commands(False)]
    assert names[:2] == ["stop", "stops"] and "help" in names and "track" not in names
    assert "track" in [c.command for c in bot_commands(True)]
    assert "/stops" in help_text(False) and "/track" not in help_text(False)
