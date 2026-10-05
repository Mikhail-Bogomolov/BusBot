import time

import aiosqlite

from .models import SavedStop, StopTrigger, Trigger

SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    route TEXT NOT NULL,
    title TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m INTEGER NOT NULL,
    bearing REAL,
    recurring INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triggers_active ON triggers (active, route);
CREATE TABLE IF NOT EXISTS stop_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    stop_url TEXT NOT NULL,
    stop_name TEXT NOT NULL,
    route_key TEXT NOT NULL,
    route_title TEXT NOT NULL,
    lead_min INTEGER NOT NULL,
    recurring INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stop_triggers_active ON stop_triggers (active, stop_id);
CREATE TABLE IF NOT EXISTS saved_stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    stop_url TEXT NOT NULL,
    title TEXT NOT NULL,
    yandex_name TEXT NOT NULL,
    lat REAL,
    lon REAL,
    created_at REAL NOT NULL,
    UNIQUE (chat_id, stop_id)
);
"""

VCOLS = "id, chat_id, stop_id, stop_url, title, yandex_name, lat, lon"

SCOLS = "id, chat_id, stop_id, stop_url, stop_name, route_key, route_title, lead_min, recurring"


def _srow(r) -> StopTrigger:
    return StopTrigger(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], bool(r[8]))

COLS = "id, chat_id, route, title, lat, lon, radius_m, bearing, recurring"


def _row(r) -> Trigger:
    return Trigger(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], bool(r[8]))


class Database:
    def __init__(self, path: str):
        self.path = path
        self._c: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._c = await aiosqlite.connect(self.path)
        await self._c.executescript(SCHEMA)
        await self._c.commit()

    async def close(self) -> None:
        if self._c:
            await self._c.close()

    async def add_trigger(self, chat_id: int, route: str, title: str, lat: float, lon: float,
                          radius_m: int, bearing: float | None, recurring: bool) -> int:
        cur = await self._c.execute(
            "INSERT INTO triggers (chat_id, route, title, lat, lon, radius_m, bearing, recurring, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (chat_id, route, title, lat, lon, radius_m, bearing, int(recurring), time.time()),
        )
        await self._c.commit()
        return cur.lastrowid

    async def active_triggers(self) -> list[Trigger]:
        cur = await self._c.execute(f"SELECT {COLS} FROM triggers WHERE active = 1")
        return [_row(r) for r in await cur.fetchall()]

    async def user_triggers(self, chat_id: int) -> list[Trigger]:
        cur = await self._c.execute(
            f"SELECT {COLS} FROM triggers WHERE active = 1 AND chat_id = ? ORDER BY id", (chat_id,))
        return [_row(r) for r in await cur.fetchall()]

    async def deactivate(self, trigger_id: int, chat_id: int | None = None) -> bool:
        if chat_id is None:
            cur = await self._c.execute("UPDATE triggers SET active = 0 WHERE id = ?", (trigger_id,))
        else:
            cur = await self._c.execute(
                "UPDATE triggers SET active = 0 WHERE id = ? AND chat_id = ?", (trigger_id, chat_id))
        await self._c.commit()
        return cur.rowcount > 0

    # ---- stop (forecast) triggers
    async def add_stop_trigger(self, chat_id: int, stop_id: str, stop_url: str, stop_name: str,
                               route_key: str, route_title: str, lead_min: int, recurring: bool) -> int:
        cur = await self._c.execute(
            "INSERT INTO stop_triggers (chat_id, stop_id, stop_url, stop_name, route_key, route_title,"
            " lead_min, recurring, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (chat_id, stop_id, stop_url, stop_name, route_key, route_title, lead_min, int(recurring), time.time()),
        )
        await self._c.commit()
        return cur.lastrowid

    async def active_stop_triggers(self) -> list[StopTrigger]:
        cur = await self._c.execute(f"SELECT {SCOLS} FROM stop_triggers WHERE active = 1 ORDER BY id")
        return [_srow(r) for r in await cur.fetchall()]

    async def user_stop_triggers(self, chat_id: int) -> list[StopTrigger]:
        cur = await self._c.execute(
            f"SELECT {SCOLS} FROM stop_triggers WHERE active = 1 AND chat_id = ? ORDER BY id", (chat_id,))
        return [_srow(r) for r in await cur.fetchall()]

    async def deactivate_stop(self, trigger_id: int, chat_id: int | None = None) -> bool:
        if chat_id is None:
            cur = await self._c.execute("UPDATE stop_triggers SET active = 0 WHERE id = ?", (trigger_id,))
        else:
            cur = await self._c.execute(
                "UPDATE stop_triggers SET active = 0 WHERE id = ? AND chat_id = ?", (trigger_id, chat_id))
        await self._c.commit()
        return cur.rowcount > 0

    # ---- saved (favourite) stops
    async def save_stop(self, chat_id: int, stop_id: str, stop_url: str, title: str, yandex_name: str,
                        lat: float | None, lon: float | None) -> int:
        """Insert, or rename if this chat already saved the stop. Returns the row id."""
        await self._c.execute(
            "INSERT INTO saved_stops (chat_id, stop_id, stop_url, title, yandex_name, lat, lon, created_at)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (chat_id, stop_id) DO UPDATE SET title = excluded.title",
            (chat_id, stop_id, stop_url, title, yandex_name, lat, lon, time.time()),
        )
        await self._c.commit()
        cur = await self._c.execute("SELECT id FROM saved_stops WHERE chat_id = ? AND stop_id = ?", (chat_id, stop_id))
        return (await cur.fetchone())[0]

    async def saved_stops(self, chat_id: int) -> list[SavedStop]:
        cur = await self._c.execute(f"SELECT {VCOLS} FROM saved_stops WHERE chat_id = ? ORDER BY id", (chat_id,))
        return [SavedStop(*r) for r in await cur.fetchall()]

    async def saved_stop(self, saved_id: int, chat_id: int) -> SavedStop | None:
        cur = await self._c.execute(f"SELECT {VCOLS} FROM saved_stops WHERE id = ? AND chat_id = ?",
                                    (saved_id, chat_id))
        r = await cur.fetchone()
        return SavedStop(*r) if r else None

    async def rename_saved_stop(self, saved_id: int, chat_id: int, title: str) -> bool:
        cur = await self._c.execute("UPDATE saved_stops SET title = ? WHERE id = ? AND chat_id = ?",
                                    (title, saved_id, chat_id))
        await self._c.commit()
        return cur.rowcount > 0

    async def delete_saved_stop(self, saved_id: int, chat_id: int) -> bool:
        cur = await self._c.execute("DELETE FROM saved_stops WHERE id = ? AND chat_id = ?", (saved_id, chat_id))
        await self._c.commit()
        return cur.rowcount > 0
