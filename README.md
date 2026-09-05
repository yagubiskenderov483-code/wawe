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

Скопируйте пример и заполните значения:

```bash
cp .env.example .env
```

Никогда не коммитьте `.env` и не вставляйте `API_HASH` / `BOT_TOKEN` в исходный код.

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

`8825465611` — это id самого бота, а не канала. Для публикации в канал укажите id вида `-100...`. Пока в `.env` стоит это значение, бот сможет писать только в тот чат, где это технически возможно (например, личка, если пользователь написал боту `/start`).

## 8. Как добавить бота в канал

1. Откройте канал → Administrators → Add Admin
2. Найдите бота по username
3. Добавьте его администратором

## 9. Какие права нужны

- **Post messages** — обязательно
- Остальные права не требуются
- User-аккаунт Telethon должен уметь вызывать `payments.getStarGifts` и `payments.getResaleStarGifts` (обычный пользовательский аккаунт)

## 10. Первый запуск Telethon

```bash
python -m app.main
```

При первом старте Telethon интерактивно спросит:

- номер телефона
- код из Telegram
- пароль 2FA, если он включён

Сессия сохраняется в `sessions/market_tracker.session`. Код и пароль в файлы проекта не пишутся.

## 11. Как работает Marketplace scanner

1. `payments.getStarGifts` — каталог типов подарков и их `gift_id`
2. Для каждого `gift_id` вызывается `payments.getResaleStarGifts` с `stars_only=true`
3. `sort_by_price` и `sort_by_num` **не** используются: Telegram тогда отдаёт лоты по Unix-времени последнего изменения цены (убывание) — это приоритет для свежих объявлений
4. Пагинация идёт по `next_offset`, пока он не пустой, не зациклился, не исчерпан лимит страниц или пока не пошла пачка уже известных лотов
5. Новые лоты определяются по стабильному `listing_key` (предпочтительно `starGiftUnique.id`)

## 12. Как работают фильтры

Порядок:

1. Новый лот / изменение цены
2. Collectible/resale
3. Цена `5000–30000` ⭐
4. Model / symbol / backdrop (если списки в `.env` не пустые)
5. Диапазон номера NFT
6. Blacklist, затем whitelist
7. Русский язык публичного текста профиля
8. Количество collectible gifts `0–12`
9. Free messages (только флаг Telegram API)
10. Account level (`userFull.stars_rating.level`), если фильтр включён
11. Ручные метки пола/национальности/тега
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

После перезапуска уже отправленные `listing_key` повторно не публикуются. Изменение цены в диапазон `5000–30000` считается новым сигналом, одно и то же изменение дважды не отправляется.

## 14. Очередь

Используется `asyncio.PriorityQueue` с лимитом `MAX_QUEUE_SIZE` (по умолчанию 100).

Приоритет выше у лотов с большим score, избранной моделью, меньшим номером и более выгодной ценой внутри диапазона.

Если очередь полная:

```
[QUEUE] Queue is full, listing skipped
```

## 15. Почему задержка 4 секунды

`PUBLISH_DELAY=4` — пауза **после успешной** публикации, в том числе между каналами. Несколько сообщений одновременно не отправляются.

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

- `MANUAL_GENDER_FILTER=female` — только вручную помеченные `female`
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
| Free messages | `user` / `userFull.send_paid_messages_stars` | `unknown`; при `REQUIRE_FREE_MESSAGES=true` → SKIP |
| Account level | `userFull.stars_rating.level` | лог `[PROFILE] Account level unavailable through Telegram API`; при выключенном фильтре не отклоняет |
| Username, bio, канал | публичный User / UserFull | поля пустые, score ниже |
| Картинка лота | document атрибута модели | отправляется только текст: бот не может прикрепить файл из user-session без отдельной загрузки |

Offset пагинации сохраняется в `scanner_state`, но **новый цикл сканирования всегда начинается с пустого offset**: результаты отсортированы от самых свежих, восстановление старого offset после рестарта небезопасно и привело бы к пропуску новых лотов. Битый offset сбрасывается.

## 18. Запуск

```bash
python -m app.main
```

Остановка: `Ctrl+C`. Корректно закрываются scanner, publisher, bot, Telethon и SQLite.

## 19. Команда /status

Напишите боту `/status`:

- Scanner: `RUNNING` / `PAUSED` / `STOPPED`
- Publisher: `RUNNING` / `WAITING` / `STOPPED`
- Queue, last scan/publish, scanned, new, filtered, sent, errors

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
