import re
from dataclasses import dataclass


def norm_route(text: str) -> str:
    """'Автобус 135' / ' 135 ' / '135' -> '135'."""
    t = text.strip().lower()
    t = re.sub(r"^(автобус|троллейбус|трамвай|маршрут|№)\s*", "", t)
    return re.sub(r"\s+", "", t)


@dataclass(slots=True, frozen=True)
class Vehicle:
    vehicle_id: str
    route: str              # already normalized
    lat: float
    lon: float
    heading: float | None   # degrees, None if the source doesn't provide it
    speed: float | None     # km/h
    ts: float               # unix seconds of the position


@dataclass(slots=True, frozen=True)
class Trigger:
    id: int
    chat_id: int
    route: str
    title: str
    lat: float
    lon: float
    radius_m: int
    bearing: float | None   # expected compass direction of travel, None = any
    recurring: bool


@dataclass(slots=True, frozen=True)
class Hit:
    trigger: Trigger
    vehicle: Vehicle
    dist_m: float
    eta_min: int


@dataclass(slots=True, frozen=True)
class SavedStop:
    id: int
    chat_id: int
    stop_id: str
    stop_url: str
    title: str              # user's own name ("Дом")
    yandex_name: str        # name on Yandex ("Студгородок")
    lat: float | None
    lon: float | None


@dataclass(slots=True, frozen=True)
class StopTrigger:
    id: int
    chat_id: int
    stop_id: str
    stop_url: str
    stop_name: str
    route_key: str
    route_title: str
    lead_min: int
    recurring: bool
