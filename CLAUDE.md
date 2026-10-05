# busbot

Telegram-бот (MVP, Красноярск, ~5 тестировщиков): присылает уведомление, когда нужный автобус будет на остановке через N минут. Прогноз берётся из Яндекс.Карт неофициальным способом — это прототип, а не продукт на продажу. Полная спецификация лежит в `spec.md`, инструкция для пользователя — в `README.md`.

## Стек
Python 3.12, asyncio, aiogram 3 (long polling, `MemoryStorage` для FSM), aiosqlite, httpx, python-dotenv. Playwright/Chromium нужен только для запасного источника `yandex_browser`. Запуск через Docker Compose на домашнем сервере.

## Команды
```bash
pip install -r requirements.txt
python -m busbot                                  # бот (.env подхватывается сам)
python -m busbot.probe "<ссылка|stop__N>"         # проверить Яндекс без Telegram
python -m busbot.probe 55.998,92.796              # ближайшие остановки к точке
python -m busbot.probe --browser "<ссылка>"       # то же через Chromium
pytest -q                                         # 38 тестов, ~30 с; браузерные пропускаются без Chromium
docker compose build && docker compose up -d
```

## Архитектура
Два независимых режима. Каждый — фоновая asyncio-задача (движок) плюс диалоги в `handlers.py`.

**Режим остановок (`/stop`, основной)**
- `sources/yandex_http.py` — `YandexHttpSource`. Делает обычный GET страницы остановки и достаёт JSON из `<script class="state-view">` → `config.masstransitStop`. Ближайшие остановки ищет через страницу поиска «остановка» вокруг точки. Не больше 2 запросов одновременно, редиректы проходит вручную с проверкой хоста.
- `sources/yandex.py` — `YandexStopSource` (headless Chromium перехватывает XHR `getStopInfo`) и общие `YandexConfig`, `check_url`, `is_captcha`.
- `sources/mock_stops.py` — фейковые прогнозы для разработки.
- `stopinfo.py` — модели (`StopSnapshot`, `RouteForecast`, `Arrival`, `NearStop`) и устойчивый парсер `parse_stop_info`: ключи без учёта регистра, три известных формата 2019–2026, секунды и миллисекунды.
- `stop_engine.py` — протокол `StopSource`, `StopMatcher` (решает, пора ли слать уведомление), `StopEngine` (тик раз в `STOP_INTERVAL`), исключение `SourceBlocked`.

**Режим геозон (`/track`, `/where`)** — включается только при `SOURCE=kgt`
- `sources/kgt.py` — координаты машин с mu-kgt.ru; URL и имена полей задаются в `.env`. `sources/mock.py` — фейк.
- `store.py` (последние позиции в памяти; курс вычисляется по смещению), `matcher.py` (радиус, направление, «едет к точке»), `engine.py` (опрос с экспоненциальным backoff), `geo.py`.

**Общее:** `__main__.py` собирает всё вместе и передаёт зависимости в Dispatcher (`db`, `stop_source`, `stop_matcher`, `store`, `track_enabled`). `db.py` — SQLite с таблицами `triggers`, `stop_triggers`, `saved_stops`; схема создаётся через `CREATE IF NOT EXISTS`, миграций нет. `config.py` — разбор переменных окружения. Middleware `Access` пускает только `ALLOWED_USERS`.

## Инварианты (не ломать)
- **Капчу и 403/429 не обходить.** При блокировке источник ставится на паузу `YANDEX_BLOCKED_PAUSE` и шлёт алерт админу. Это сознательное решение, а не недоработка.
- Каждый редирект проверяется на хосты Яндекса (`allowed_hosts`), путь должен быть `/maps…`. Ссылка от пользователя не должна уводить бота на чужой домен.
- ETA считается по часам Яндекса (`server_time` в снапшоте), а не по часам сервера.
- Уведомления идут только по GPS-прогнозу (`Arrival.live`); расписание используется только при `STOP_USE_SCHEDULED=1`. Один `vehicleId` даёт одно уведомление на триггер (cooldown 30 мин).
- Триггер «один раз» деактивируется (`active=0`) сразу после отправки. Строки из БД не удаляются, кроме `saved_stops`.
- Все запросы к БД из хендлеров фильтруются по `chat_id`.
- Тексты для пользователя на русском, `parse_mode=HTML`; любые внешние строки проходят через `html.escape`.
- `callback_data` короткие с префиксами (`sr:`, `sl:`, `sm:`, `sdel:`, `sv:`, `svr:`, `svd:`, `svx:`, `spk:`, `sn:`, `snb`, `ssave`, `sname:keep`, `del:`, `dir:`, `rad:`, `mode:`). Новый префикс не должен совпадать с началом существующего.

## Тесты
`tests/`: `test_all.py` (парсер, матчеры, движки на mock и SQLite с подменой `clock`), `test_bot_flow.py` (диалоги через настоящий aiogram Dispatcher), `test_stops.py` (геометка → ближайшие → сохранение → `/stops`), `test_yandex_http.py` (HTTP-источник против локальной имитации Яндекса), `test_yandex_browser.py` (настоящий Chromium). Фикстуры в `tests/fixtures/` — это реальные ответы Яндекса; не редактировать их руками. При изменении формата добавлять новую фикстуру рядом. Время в движках и источниках подменяется через параметр `clock`; `time.sleep` в тестах не использовать.

## Стиль
Плотный код без лишних абстракций, `dataclass(slots=True, frozen=True)` для моделей, комментарии короткие и на английском, сообщения об ошибках для пользователя на русском. Ошибки источника: `ValueError` — понятная пользователю причина, `TimeoutError`/`ConnectionError` — сеть, `SourceBlocked` — бан. `handlers._load` превращает их в текст.

## Осторожно
- `.env` содержит настоящий `BOT_TOKEN`. Не выводить его и не коммитить. `.gitignore` в проекте пока нет.
- `data/` — рабочие данные: SQLite, профиль Chromium, дампы `yandex_dump/`.
- Дефолты в `config.py` расходятся с `.env.example`: `SOURCE` (mock против none), `YANDEX_REFRESH` (60 против 30). Источник истины — `.env`.
