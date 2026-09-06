# Telegram Marketplace Tracker

Python-трекер новых лотов коллекционных Telegram Gifts на Marketplace. Подходящие объявления публикуются в указанный Telegram-канал (или несколько каналов) с паузой 4 секунды между успешными отправками.

## 1. Требования

- Python 3.11+
- Аккаунт Telegram для Telethon user client
- Бот от [@BotFather](https://t.me/BotFather)
- Канал, куда бот может писать (бот — администратор с правом публикации)

## 2. Создание venv

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows:

```bat
python -m venv .venv
.venv\Scripts\activate
```

## 3. Установка зависимостей

```bash
pip install -r requirements.txt
```

## 4. Файл .env

В этом деплое ключи уже прописаны в `.env`, `.env.example` и значениях по умолчанию в `app/config.py`:

- `API_ID=36101343`
- бот `@jsjeigiejwhnewbot`
- `TARGET_CHANNEL_ID=-1003784435307`
- `PUBLISH_DELAY=4`

Достаточно запустить программу. При необходимости скопируйте шаблон:

```bash
cp .env.example .env
```

## 5. Где получить API_ID / API_HASH

1. Откройте https://my.telegram.org
2. Войдите по номеру телефона
3. Перейдите в **API development tools**
4. Создайте приложение и скопируйте `api_id` и `api_hash`

## 6. Где получить BOT_TOKEN

1. Откройте [@BotFather](https://t.me/BotFather)
2. Команда `/newbot`
3. Скопируйте токен вида `123456789:AAH...`

## 7. Как указать TARGET_CHANNEL_ID

Числовой id чата, куда бот будет отправлять лоты.

- Канал/супергруппа обычно выглядит как `-100xxxxxxxxxx`
- Узнать id можно через [@userinfobot](https://t.me/userinfobot), экспорт чата или логи бота после добавления в канал
- Несколько каналов: `TARGET_CHANNELS=-100111,-100222`
- Если заданы и `TARGET_CHANNEL_ID`, и `TARGET_CHANNELS`, списки объединяются

Лоты идут **только в канал** `-1003784435307`. Личка с ботом — команды и `/login`, туда лоты не дублируются. Добавьте бота админом канала с правом публикации.

## 8. Как добавить бота в канал

1. Откройте канал → Administrators → Add Admin
2. Найдите бота по username
3. Добавьте его администратором

## 9. Какие права нужны

- **Post messages** — обязательно
- Остальные права не требуются
- User-аккаунт Telethon должен уметь вызывать `payments.getStarGifts` и `payments.getResaleStarGifts` (обычный пользовательский аккаунт)

## 10. Первый запуск Telethon

Программа больше не спрашивает номер в терминале (в Docker это падает с `EOFError`).

1. Запустите tracker
2. Напишите боту `/start`
3. Отправьте `/login`
4. Пришлите номер телефона, например `+79001234567`
5. Пришлите код из Telegram
6. Если включён 2FA — пришлите облачный пароль

Сессия сохраняется в `sessions/market_tracker.session`. Номер, код и пароль в файлы проекта не пишутся и в лог не попадают. Отмена: `/cancel`.

`/start` не делает личку получателем лотов. Канал задаётся в `TARGET_CHANNEL_ID`.

## 11. Как работает Marketplace scanner

1. `payments.getStarGifts` — каталог типов подарков и их `gift_id`
2. Для каждого `gift_id` вызывается `payments.getResaleStarGifts` с `stars_only=true`
3. `sort_by_price` и `sort_by_num` **не** используются: Telegram тогда отдаёт лоты по Unix-времени последнего изменения цены (убывание) — это приоритет для свежих объявлений
4. Пагинация идёт по `next_offset`, пока он не пустой, не зациклился, не исчерпан лимит страниц или пока не пошла пачка уже известных лотов
5. Первый прогон — **snapshot**: текущий рынок сохраняется как `EXISTING` и **не публикуется**. Потом режим `LIVE`: в канал идут только лоты, которых не было на рынке в момент старта
6. Цена лота сверяется с медианой коллекции (`MAX_MARKET_RATIO=3`). Подарок за 300⭐, выставленный за 7к, не пройдёт
7. Один владелец публикуется один раз. Одна и та же модель/коллекция не идёт подряд

## 12. Как работают фильтры

Порядок:

1. Только свежий лот с начала ленты (старый рынок запоминается и не публикуется)
2. Collectible/resale
3. Цена `5000–25000` ⭐
4. Model / symbol / backdrop (если списки в `.env` не пустые)
5. Диапазон номера NFT
6. Blacklist, затем whitelist
7. Русский язык публичного текста профиля
8. Количество collectible gifts `0–12` (если счёт недоступен — SKIP)
9. Free messages в ЛС
10. Account level не выше 2
11. Только девушки
12. Score

Whitelist ограничивает **чьи** лоты можно публиковать, но не отключает цену, NFT, язык и остальные обязательные проверки.

## 13. SQLite

Файл: `data/tracker.db`

Таблицы:

- `listings` — лоты, статусы `NEW / QUEUED / SENT / SKIPPED / ERROR`
- `profiles` — кэш публичных профилей
- `profile_preferences` — ручные метки
- `price_history` — история цен
- `scanner_state` — pagination offset
- `stats` — зарезервировано

После перезапуска уже отправленные `listing_key` повторно не публикуются. Изменение цены не считается новым лотом.

## 14. Очередь

Используется `asyncio.PriorityQueue` с лимитом `MAX_QUEUE_SIZE` (по умолчанию 100).

Приоритет выше у лотов с большим score, избранной моделью, меньшим номером и более выгодной ценой внутри диапазона.

Если очередь полная:

```
[QUEUE] Queue is full, listing skipped
```

## 15. Почему задержка 4 секунды

`PUBLISH_DELAY=4` — пауза **после успешной** публикации: в канал не чаще чем раз в 4 секунды.

Под каждым лотом кнопки: **Открыть лот** (`https://t.me/nft/<slug>`) и **Написать** (профиль продавца).

## 16. Ручные метки

Трекер **не** угадывает пол и национальность.

```
/tag USER_ID gender=female nationality=ru tag=trusted
/tag USER_ID gender=male
/tag USER_ID nationality=ru
/tag USER_ID gender=unknown nationality=unknown
/untag USER_ID
/profile USER_ID
```

Допустимые теги: `trusted`, `interesting`, `ignore`, `favorite`. Профили с `ignore` не публикуются.

Фильтры из `.env`:

- `MANUAL_GENDER_FILTER=female` — только девушки: `/tag` или имя (Анна, Мария, …)
- `RUSSIAN_LANGUAGE_REQUIRED=true` — русский профиль
- `MANUAL_NATIONALITY_FILTER=ru` — только вручную помеченные `ru`
- пустое значение — фильтр выключен
- если фильтр включён, а профиль не размечен — SKIP

## 17. Какие данные Telegram API может не предоставлять

Безопасные fallback (программа не падает и ничего не выдумывает):

| Данные | Источник | Если нет |
| --- | --- | --- |
| Цена | `starGiftUnique.resell_amount` → `starsAmount` | SKIP |
| Slug / ссылка | `slug` | поле не показывается |
| Model / symbol / backdrop | `StarGiftAttribute*` | `None`, фильтр allowlist пропускает лот только если список пуст или значение есть |
| NFT count | подсчёт `StarGiftUnique` через `payments.getSavedStarGifts` | при `STRICT_NFT_FILTER=false` не отклоняет |
| Free messages | `user` / `userFull.send_paid_messages_stars` | `unknown`; при `REQUIRE_FREE_MESSAGES=true` → SKIP, по умолчанию выключено |
| Account level | `userFull.stars_rating.level` | лог `[PROFILE] Account level unavailable through Telegram API`; при выключенном фильтре не отклоняет |
| Username, bio, канал | публичный User / UserFull | поля пустые, score ниже |
| Картинка лота | document атрибута модели | отправляется только текст: бот не может прикрепить файл из user-session без отдельной загрузки |

Offset пагинации сохраняется в `scanner_state`, но **новый цикл сканирования всегда начинается с пустого offset**: результаты отсортированы от самых свежих, восстановление старого offset после рестарта небезопасно и привело бы к пропуску новых лотов. Битый offset сбрасывается.

## 18. Запуск

```bash
python -m app.main
```

Остановка: `Ctrl+C`. Корректно закрываются scanner, publisher, bot, Telethon и SQLite.

Если сразу после старта в логе `TelegramUnauthorizedError` / `BOT_TOKEN is invalid or revoked` — это **не** Telethon user-сессия. Telegram отверг токен бота (часто после утечки в git). `/login` в этом состоянии не работает.

1. Откройте [@BotFather](https://t.me/BotFather) → `/token` или `/newbot`
2. Пропишите новый токен в `.env` как `BOT_TOKEN=...`
3. Перезапустите tracker
4. Напишите боту `/login` и авторизуйте user-сессию

Сообщение `User session is not authorized` само по себе не фатально: бот должен остаться онлайн и ждать `/login`.

## 19. Команда /status

Напишите боту `/status`:

- Scanner: `RUNNING` / `PAUSED` / `STOPPED`
- Publisher: `RUNNING` / `WAITING` / `STOPPED`
- User session: `AUTHORIZED` / `WAITING_LOGIN`
- Queue, last scan/publish, scanned, new, filtered, skip-причины, sent, errors

`/pause` останавливает получение новых лотов. Publisher досылает очередь. `/resume` продолжает сканирование.

## 20. Команда /stats

`/stats` показывает статистику текущего запуска: scanned, duplicates, фильтры, queued, sent, send_errors, price_changes, floodwaits, average_scan_time, average_publish_time.

## Дополнительно

### Ошибки администратору

Укажите `ADMIN_USER_ID`. Критические ошибки scanner/publisher/сессии/канала/БД приходят админу. FloodWait в статистику пишется, но критической ошибкой не считается. Секреты в уведомления не попадают.

### Кэш профилей

`PROFILE_CACHE_TTL=300` — повторно не дергать Telegram, если профиль обновлялся меньше 5 минут назад. Перед публикацией профиль всё равно перепроверяется.

### Backup SQLite

`DB_BACKUP_INTERVAL=3600` — копии в `data/backups/`, не чаще заданного интервала.

### Debug

`DEBUG=true` включает технические логи: страницы pagination, `next_offset`, тайминги, причины фильтров, размер очереди, операции БД. Не логируются `API_HASH`, `BOT_TOKEN`, код входа и 2FA.

### Тесты

```bash
python -m unittest discover -s tests -v
```

## Структура

```
app/
  main.py
  config.py
  telegram/          user client + aiogram bot
  marketplace/       scanner, parser, filters, models
  profile/           analyzer + language
  storage/           sqlite + backup
  notifications/     publisher + admin alerts
  utils/             logger, rate limit, queue, stats
tests/
sessions/
data/
```
