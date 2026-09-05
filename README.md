# Telegram Marketplace Tracker

Python-трекер новых лотов коллекционных Telegram Gifts. Подходящие объявления публикуются **только** в `TARGET_CHANNEL_ID` / `TARGET_CHANNELS`. Личка бота и `ADMIN_USER_ID` используются исключительно для команд и ошибок.

## 1. Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Требования: Python 3.11+, user-аккаунт Telegram для Telethon, бот от [@BotFather](https://t.me/BotFather), канал, куда бот может писать.

## 2. requirements

`requirements.txt`: `telethon`, `aiogram`, `python-dotenv`, `aiohttp`.

## 3. Файл .env

Скопируйте шаблон и заполните канал:

```bash
cp .env.example .env
```

В этом деплое уже прописаны `API_ID`, `API_HASH`, `BOT_TOKEN` и `@jsjeigiejwhnewbot`.

Обязательно задайте **реальный канал**:

```
TARGET_CHANNEL_ID=-100xxxxxxxxxx
```

Несколько каналов:

```
TARGET_CHANNELS=-100111,-100222
```

`8825465611` — id самого бота. Это **не** канал. Лоты туда не отправляются. `ADMIN_USER_ID` тоже не является target channel.

Ключевые настройки:

| Переменная | Смысл |
| --- | --- |
| `MIN_PRICE` / `MAX_PRICE` | 5000–30000 Stars |
| `PUBLISH_DELAY` | пауза 4 секунды между успешными публикациями |
| `MANUAL_GENDER_FILTER` | например `female`; без ручной метки — SKIP |
| `RUSSIAN_LANGUAGE_REQUIRED` | русскоязычный **публичный текст** профиля |
| `MAX_NFT_COUNT` | максимум 12 collectible NFT |
| `STRICT_NFT_FILTER` | `true` → неизвестный NFT count = SKIP |
| `REQUIRE_FREE_MESSAGES` | неизвестно/платно = SKIP |
| `ENABLE_ACCOUNT_LEVEL_FILTER` | только если API отдаёт `stars_rating.level` |
| `MARKET_SAMPLE_SIZE` | до 20 comparable listings |
| `MAX_MARKET_RATIO` | `listing / market_value`; выше 3.0 → SKIP |
| `MARKET_CACHE_TTL` | кэш market value, секунды |
| `STRICT_MARKET_FILTER` | нет comparable → SKIP |
| `DIVERSIFY_GIFTS` / `MAX_SAME_GIFT_STREAK` | не слать один `gift_id` подряд |
| `WHITELIST_USERS` / `BLACKLIST_USERS` | blacklist важнее |
| `FAVORITE_MODELS` | бонус к score, **не** обходит market filter |
| `DB_BACKUP_INTERVAL` | backup SQLite, по умолчанию 3600 |
| `DEBUG` | подробные логи без секретов |

## 4. Первый запуск

```bash
python -m app.main
```

1. Напишите боту `/start` — это **команды**, лоты сюда не приходят.
2. `/login` → телефон → код Telegram → 2FA при необходимости.
3. Tracker делает **initial snapshot** текущих listings.
4. После snapshot включается **LIVE mode**.

Сессия: `sessions/market_tracker.session`. Код и пароль в файлы и логи не пишутся. Отмена: `/cancel`.

## 5. Telegram login

Вход только через бота (`/login`). Терминал не используется (в Docker `input()` даёт `EOFError`).

User-аккаунт должен уметь вызывать реальные методы:

- `payments.getStarGifts`
- `payments.getResaleStarGifts`
- `payments.getStarGifts` / `payments.getSavedStarGifts` / `payments.getUniqueStarGift` — где это нужно для NFT count и recheck

Поля вроде `listing.created_at` **нет** в Telegram API. Tracker их не выдумывает.

## 6. Initial snapshot

Состояние `scanner_mode`:

`INITIAL_SNAPSHOT` → `LIVE`

При первом запуске (пустая БД):

1. Сканер получает текущие resale listings.
2. Записывает их в SQLite со статусом `EXISTING`.
3. **Ничего не публикует.**
4. Только после полного прохода включает LIVE.

Лог:

```
[SNAPSHOT] Existing listing -> SKIP gift:...
[LIVE] Initial snapshot complete. Switching to LIVE mode
```

## 7. Live mode

Только LIVE может создавать `NEW` listings.

```
[LIVE] New listing detected: ...
[FILTER] Price PASS: 12000
[MARKET] Estimated market value: 18000
[MARKET] Ratio: 0.67
[FILTER] PASS
[QUEUE] Added
[PUBLISHER] Sending to TARGET_CHANNEL_ID ...
[PUBLISHER] Sent successfully
```

## 8. Как указать TARGET_CHANNEL_ID

1. Создайте канал или супергруппу.
2. Добавьте бота администратором с правом **Post messages**.
3. Узнайте id (обычно `-100...`) через @userinfobot или логи бота.
4. Пропишите `TARGET_CHANNEL_ID` в `.env`.
5. Несколько целей: `TARGET_CHANNELS=`.

Админский чат, личка бота и пользователь, который нажал `/start`, **никогда** не считаются target channel.

Публикация идёт только из `app/notifications/publisher.py`.

## 9. Как работает market value

Для нового listing Tracker запрашивает comparable resale listings того же `gift_id` (тот же collectible). По возможности оставляются лоты с той же model / symbol / backdrop. Сам проверяемый listing в выборку **не** входит.

Из цен считается **медиана**, не max и не случайная цена.

Пример: `500, 550, 600, 620, 650, 700, 25000` → market_value ≈ `620`.

Дополнительно:

- `floor_price` = минимум выборки
- `sample_size` ≤ `MARKET_SAMPLE_SIZE` (20)
- `confidence`: `<5` low, `5–9` medium, `10+` high
- `price_ratio = listing_price / market_value`
- если `listing_price > market_value * MAX_MARKET_RATIO` (3.0) → SKIP
- `discount_percent = ((market_value - listing_price) / market_value) * 100`

Кэш `market_prices`, ключ `gift_id|model|symbol|backdrop`, TTL `MARKET_CACHE_TTL=60`.

Market filter — **hard filter**. Score и favorite models его не обходят. Listing за 25000 при market 600 отклоняется.

Если comparable нет, значение не выдумывается. При `STRICT_MARKET_FILTER=true` такой лот пропускается.

## 10. Почему старые listings не отправляются

- Первый запуск = snapshot, статус `EXISTING`.
- У каждого лота стабильный `listing_key` (`gift:{starGiftUnique.id}` или fallback `slug:...`).
- Если ключ уже есть в SQLite — это не NEW.
- `first_seen_at` значит только «tracker впервые увидел этот listing», это **не** время создания лота в Telegram.
- После перезапуска старые ключи не публикуются повторно.
- Статус `SENT` никогда не отправляется снова.

## 11. Ручной gender

Tracker **не** определяет пол по фото, имени, username, аватару, языку или описанию.

Метка только вручную:

```
/tag USER_ID gender=female
/tag USER_ID gender=male
/tag USER_ID gender=unknown
/untag USER_ID
/profile USER_ID
```

При `MANUAL_GENDER_FILTER=female` проходят только профили с меткой `female`. Нет метки → SKIP.

## 12. Очередь

Архитектура:

```
Scanner → filters (включая market) → Queue → Publisher → TARGET_CHANNEL_ID
```

Scanner сам лоты не публикует. Очередь приоритетная, лимит `MAX_QUEUE_SIZE`.

`/pause` сбрасывает текущую очередь. После `/resume` накопленные лоты не публикуются «как новые». Сканер при паузе может обновлять БД, но в publisher ничего не отправляет.

## 13. Задержка 4 секунды

После **успешной** публикации listing:

```
listing 1 -> send
wait 4 sec
listing 2 -> send
```

Массовой отправки через `asyncio.gather()` нет.

## 14. Anti-duplicate

- `listing_key UNIQUE` в SQLite
- проверка перед queue и перед send
- после успеха: `status=SENT`, `sent_at=NOW`
- изменение цены — новая запись в `listing_price_history`, это **не** новый listing

Одинаковый `gift_id` не идёт подряд, если в очереди есть другой (`DIVERSIFY_GIFTS=true`, `MAX_SAME_GIFT_STREAK=1`). Повтор разрешён только если альтернативы нет или диверсификация выключена.

## 15. FloodWait

`FloodWaitError` / `TelegramRetryAfter`: Tracker ждёт `N` секунд (`await asyncio.sleep(N)`) и не спамит повторными запросами. Ошибка уходит админу с троттлингом, лоты в админку не перекладываются.

## 16. Что Telegram API объективно не даёт

Программа не выдумывает поля.

| Данные | Источник | Если нет |
| --- | --- | --- |
| Время создания listing | нет такого поля | используется только `first_seen_at` трекера |
| Цена | `starGiftUnique.resell_amount` → `StarsAmount` | SKIP |
| Model / symbol / backdrop | `StarGiftAttribute*` | comparable по атрибуту ужесточается только если атрибут есть |
| NFT count | подсчёт unique gifts через `payments.getSavedStarGifts` | `STRICT_NFT_FILTER` |
| Free messages | `send_paid_messages_stars` | unknown; при `REQUIRE_FREE_MESSAGES=true` → SKIP |
| Account level | `userFull.stars_rating.level` | не симулируется; фильтр включён и значение неизвестно → SKIP |
| Пол / национальность | только ручные метки | автодетект запрещён |
| Market value | медиана чужих resale того же gift | нет выборки → не выдумывается |

Pagination: `next_offset` пишется в `scanner_state`. Каждый цикл LIVE начинается с пустого offset (свежие лоты первыми). Зацикливание offset прерывается.

## Команды бота

`/start` `/login` `/pause` `/resume` `/status` `/stats` `/tag` `/untag` `/profile` `/cancel`

`/status` показывает mode, TARGET_CHANNEL_ID, queue, scanned / existing / new / sent / errors.

## Pipeline

```
Telegram Marketplace
→ Scanner
→ NEW / EXISTING
→ price → NFT → profile cache → manual gender → language
→ free messages → account level → market value
→ whitelist/blacklist → score
→ Queue → gift diversification → 4s delay → recheck
→ Publisher → TARGET_CHANNEL_ID
```

Админ отдельно:

```
Bot → commands / errors / stats → ADMIN_USER_ID
```

## SQLite

Файл: `data/tracker.db`

Таблицы: `listings`, `listing_price_history`, `market_prices`, `profiles`, `manual_tags`, `scanner_state`, `scanner_meta`, `queue`, `stats`.

Backup: `DB_BACKUP_INTERVAL=3600`, каталог `data/backups/`, через SQLite backup API (WAL не ломается).

## Тесты

```bash
python -m compileall app tests
python -m unittest discover -s tests -v
pytest -q
```

## Структура

```
app/
  main.py
  config.py
  telegram/          user client + aiogram bot (команды/ошибки)
  marketplace/       scanner, parser, filters, market, models
  profile/           analyzer + language of public text
  storage/           sqlite + backup
  notifications/     publisher (только TARGET_CHANNEL_ID) + admin alerts
  utils/             logger, rate limit, queue, stats
```
