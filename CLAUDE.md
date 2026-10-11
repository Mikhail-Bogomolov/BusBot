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
python -m busbot.probe --line "<ссылка>" 3 30     # схема маршрута и контрольные остановки для упреждения 30 мин
python -m busbot.tgproxy "<vless://…|подписка>" [N]   # ключ VPN -> xray/config.json для контейнера tg-proxy
pytest -q                                         # 65 тестов, ~20 с; браузерные пропускаются без Chromium, Xray — без xray/XRAY_PATH
docker compose build && docker compose up -d
```

## Архитектура
Два независимых режима. Каждый — фоновая asyncio-задача (движок) плюс диалоги в `handlers.py`.

**Режим остановок (`/stop`, основной)**
- `sources/yandex_http.py` — `YandexHttpSource`. Делает обычный GET страницы остановки и достаёт JSON из `<script class="state-view">` → `config.masstransitStop`. Ближайшие остановки ищет через страницу поиска «остановка» вокруг точки. `line()` грузит страницу маршрута (`/maps/{регион}/routes/{тип}_{номер}/{hex(uri линии)}/` → `config.masstransitLine`: остановки каждого направления по порядку + линия пути), кеш `YANDEX_LINE_TTL`. Не больше 2 запросов одновременно, редиректы проходит вручную с проверкой хоста.
- `sources/yandex.py` — `YandexStopSource` (headless Chromium перехватывает XHR `getStopInfo`) и общие `YandexConfig`, `check_url`, `is_captcha`.
- `sources/mock_stops.py` — фейковые прогнозы для разработки.
- `stopinfo.py` — модели (`StopSnapshot`, `RouteForecast`, `Arrival`, `NearStop`, `LineThread`, `LineStop`) и устойчивые парсеры `parse_stop_info` (ключи без учёта регистра, три известных формата 2019–2026, секунды и миллисекунды) и `parse_line`.
- `stop_engine.py` — протокол `StopSource`, `StopMatcher` (решает, пора ли слать уведомление), `StopEngine` (тик раз в `STOP_INTERVAL`), исключение `SourceBlocked`.
- `upstream.py` — упреждение больше горизонта Яндекса (`YANDEX_HORIZON`, ~15 мин). **A:** для оповещения с `lead_min > горизонт` выбирает «контрольные» остановки выше по маршруту (не дальше ~12 мин пути друг от друга, чтобы горизонты перекрывались), движок опрашивает их вместе с целевой, а GPS-прибытие там переносится на цель: `eta_T = eta_U + время(U→T)`. Время перегона `Travel` учит по реальным проездам одного `vehicleId` (таблица `segment_times`): последние автобусы → тот же час в прошлые дни → расстояние / выученная скорость на подходе к цели → `UPSTREAM_SPEED`. **B:** при `SOURCE=kgt` координаты машин проецируются на линию пути (`geo.locate_on_path`), ETA = остаток пути / скорость. Оценки — `Arrival(via=…)`, id машин КГТ с префиксом `kgt:`.

**Режим геозон (`/track`, `/where`)** — включается только при `SOURCE=kgt`
- `sources/kgt.py` — координаты машин с mu-kgt.ru; URL и имена полей задаются в `.env`. `sources/mock.py` — фейк.
- `store.py` (последние позиции в памяти; курс вычисляется по смещению), `matcher.py` (радиус, направление, «едет к точке»), `engine.py` (опрос с экспоненциальным backoff), `geo.py`.

**Telegram из РФ:** с сервера Telegram заблокирован, VPN на сервере не включаем (Яндекс должен видеть российский IP). `docker-compose.yml` поднимает `tg-proxy` (официальный образ Xray) с `xray/config.json`, бот получает `TELEGRAM_PROXY=socks5://tg-proxy:1080`. `__main__.make_bot` направляет через прокси только сессию aiogram (http/socks4/socks5, нужен `aiohttp-socks`). `tgproxy.py` превращает ключ из Happ/v2rayN (`vless`/`vmess`/`trojan`/`ss`) или подписку в конфиг Xray: SOCKS5-вход без пароля на 1080, порт наружу не публикуется.

**Общее:** `__main__.py` собирает всё вместе и передаёт зависимости в Dispatcher (`db`, `stop_source`, `stop_matcher`, `upstream`, `store`, `track_enabled`). `db.py` — SQLite с таблицами `triggers`, `stop_triggers`, `saved_stops`, `segment_times`; схема создаётся через `CREATE IF NOT EXISTS`, миграций нет. `config.py` — разбор переменных окружения. Middleware `Access` пускает только `ALLOWED_USERS`.

## Инварианты (не ломать)
- **Капчу и 403/429 не обходить.** При блокировке источник ставится на паузу `YANDEX_BLOCKED_PAUSE` и шлёт алерт админу. Это сознательное решение, а не недоработка.
- Каждый редирект проверяется на хосты Яндекса (`allowed_hosts`), путь должен быть `/maps…`. Ссылка от пользователя не должна уводить бота на чужой домен.
- ETA считается по часам Яндекса (`server_time` в снапшоте), а не по часам сервера.
- Уведомления идут только по GPS-прогнозу (`Arrival.live`); расписание используется только при `STOP_USE_SCHEDULED=1`. Один `vehicleId` даёт одно уведомление на триггер (cooldown 30 мин + ETA на момент уведомления, чтобы объявленный заранее автобус не повторился до прибытия). Автобус, объявленный по координатам КГТ (`kgt:…`), не объявляется повторно, когда его покажет Яндекс: тот же id без префикса или прибытие в пределах допуска.
- Оценки сверх горизонта (`Arrival.via`) делаются только для оповещений с `lead_min > YANDEX_HORIZON` и в тексте помечаются как оценка. Прямой прогноз Яндекса по цели всегда важнее оценки для того же `vehicleId`. Mock-координаты (`SOURCE=mock`) в вариант B не подаются: они лежат на реальных улицах Красноярска.
- Триггер «один раз» деактивируется (`active=0`) сразу после отправки. Строки из БД не удаляются, кроме `saved_stops`.
- Все запросы к БД из хендлеров фильтруются по `chat_id`.
- Тексты для пользователя на русском, `parse_mode=HTML`; любые внешние строки проходят через `html.escape`.
- `callback_data` короткие с префиксами (`sr:`, `sl:`, `sm:`, `sdel:`, `sv:`, `svr:`, `svd:`, `svx:`, `spk:`, `sn:`, `snb`, `ssave`, `sname:keep`, `del:`, `dir:`, `rad:`, `mode:`). Новый префикс не должен совпадать с началом существующего.

## Тесты
`tests/`: `test_all.py` (парсер, матчеры, движки на mock и SQLite с подменой `clock`), `test_bot_flow.py` (диалоги через настоящий aiogram Dispatcher), `test_stops.py` (геометка → ближайшие → сохранение → `/stops`), `test_yandex_http.py` (HTTP-источник против локальной имитации Яндекса), `test_upstream.py` (схема маршрута, выбор контрольных остановок, симуляция автобусов по реальной схеме маршрута 3 с обучением времени перегона, координаты КГТ, «одно уведомление на автобус»), `test_yandex_browser.py` (настоящий Chromium), `test_proxy.py` (Telegram через HTTP CONNECT, разбор `TELEGRAM_PROXY`), `test_tgproxy.py` (ключи и подписка → конфиг Xray; с настоящим Xray: `xray run -test` и цепочка бот → SOCKS5 → Xray → VLESS → имитация Telegram). Фикстуры в `tests/fixtures/` — это реальные ответы Яндекса; не редактировать их руками. При изменении формата добавлять новую фикстуру рядом. Время в движках и источниках подменяется через параметр `clock`; `time.sleep` в тестах не использовать.

## Стиль
Плотный код без лишних абстракций, `dataclass(slots=True, frozen=True)` для моделей, комментарии короткие и на английском, сообщения об ошибках для пользователя на русском. Ошибки источника: `ValueError` — понятная пользователю причина, `TimeoutError`/`ConnectionError` — сеть, `SourceBlocked` — бан. `handlers._load` превращает их в текст.

## Осторожно
- `.env` содержит настоящий `BOT_TOKEN`, `xray/config.json` — ключ VPN. Не выводить их и не коммитить (оба в `.gitignore`).
- Прокси только для Telegram. Не задавать `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` в окружении: httpx в клиенте Яндекса их подхватит, и запросы к Яндексу пойдут через VPN (заграничный IP, редиректы на `.eu`/`.com`).
- Старый python-dotenv читает `KEY=   # коммент` как значение `# коммент`; `config._value` это страхует (`TELEGRAM_PROXY`, `ADMIN_ID`, `ALLOWED_USERS`).
- `data/` — рабочие данные: SQLite, профиль Chromium, дампы `yandex_dump/`.
- Дефолты в `config.py` расходятся с `.env.example`: `SOURCE` (mock против none), `YANDEX_REFRESH` (60 против 30). Источник истины — `.env`.
