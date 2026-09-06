from __future__ import annotations

import asyncio
import signal
from pathlib import Path

from aiogram.exceptions import TelegramUnauthorizedError

from app.config import DATA_DIR, load_settings
from app.marketplace.market import MarketEstimator
from app.marketplace.scanner import MarketplaceScanner
from app.notifications.alerts import AlertManager
from app.notifications.publisher import Publisher
from app.profile.analyzer import ProfileAnalyzer
from app.storage.backup import create_backup
from app.storage.database import Database
from app.telegram.bot import BOT_TOKEN_HELP, BotUnauthorizedError, setup_bot, verify_target_channels
from app.telegram.user_client import build_user_client, connect_user_client
from app.utils.logger import log, redact_secrets, setup_logging
from app.utils.rate_limit import ApiLimiter, SessionInvalidError, sleep_seconds
from app.utils.state import AppState


async def backup_loop(state: AppState, db: Database, backup_dir: str, interval: int) -> None:
    interval = max(60, int(interval or 3600))
    while not state.shutdown:
        try:
            await sleep_seconds(interval)
            if state.shutdown:
                return
            create_backup(db, backup_dir)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log("ERROR", f"SQLite backup failed: {type(error).__name__}: {redact_secrets(str(error))}")


async def run_tracker() -> None:
    settings = load_settings()
    setup_logging(settings.debug)
    if settings.memory_only:
        # No files on disk: the tracker remembers the market only for the
        # lifetime of the process, so every restart starts from a clean slate
        # and can never republish yesterday's lots.
        log("DB", "MEMORY_ONLY=true: in-memory database, nothing is written to disk")
    else:
        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        Path(settings.backup_dir).mkdir(parents=True, exist_ok=True)

    db = Database(settings.db_path)
    state = AppState(settings.max_queue_size)
    stored_mode = db.get_scanner_mode()
    if stored_mode:
        state.scanner_mode = stored_mode
    limiter = ApiLimiter(
        concurrency=1,
        min_interval=settings.api_min_interval,
        max_interval=settings.api_max_interval,
    )
    client = build_user_client(settings)
    bot = None
    dp = None
    alerts = AlertManager(settings, None, state)
    tasks: list[asyncio.Task] = []
    worker_lock = asyncio.Lock()

    stop_event = asyncio.Event()

    def _request_stop() -> None:
        log("MAIN", "Shutdown requested")
        state.request_shutdown()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop())

    async def start_workers() -> None:
        async with worker_lock:
            if state.shutdown or state.workers_started:
                return
            if not await client.is_user_authorized():
                return
            analyzer = ProfileAnalyzer(client, db, settings, limiter, state.stats)
            market = MarketEstimator(settings, db, client, limiter, state.stats)
            ctx_bind_analyzer(analyzer)
            scanner = MarketplaceScanner(settings, state, db, client, analyzer, limiter, alerts, market)
            publisher = Publisher(settings, state, db, bot, client, analyzer, alerts, market)
            tasks.append(asyncio.create_task(scanner.run(), name="scanner"))
            tasks.append(asyncio.create_task(publisher.run(), name="publisher"))
            state.user_authorized = True
            state.workers_started = True
            log("MAIN", "Scanner and publisher started")

    try:
        authorized = await connect_user_client(client)
        state.user_authorized = authorized
        analyzer = ProfileAnalyzer(client, db, settings, limiter, state.stats)
        bot, dp = setup_bot(settings, state, db, analyzer, client=client, on_authorized=start_workers)
        alerts.bind_bot(bot)
        try:
            usable = await verify_target_channels(bot, settings, db)
            if not usable:
                await alerts.notify(
                    "ChannelCheck",
                    "TARGET_CHANNEL_ID is missing or the bot cannot post there. "
                    "Add the bot as channel admin with post rights. Lots never go to the bot DM.",
                )
        except BotUnauthorizedError as error:
            log("ERROR", str(error))
            raise SystemExit(1) from error
        except Exception as error:
            log("ERROR", str(error))
            await alerts.notify("ChannelCheck", error)

        if authorized:
            await start_workers()
        else:
            log("AUTH", "Bot is up. Open @%s and send /login" % settings.bot_username)

        tasks.append(asyncio.create_task(dp.start_polling(bot), name="bot"))
        if not settings.memory_only:
            tasks.append(
                asyncio.create_task(
                    backup_loop(state, db, settings.backup_dir, settings.db_backup_interval),
                    name="backup",
                )
            )
        log("MAIN", "Tracker is running. Press Ctrl+C to stop.")
        await stop_event.wait()
    except BotUnauthorizedError as error:
        log("ERROR", str(error))
        raise SystemExit(1) from error
    except TelegramUnauthorizedError as error:
        log("ERROR", BOT_TOKEN_HELP)
        raise SystemExit(1) from error
    except SessionInvalidError as error:
        log("ERROR", str(error))
        if bot is not None:
            await alerts.notify("UserSession", error)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log("ERROR", f"Fatal error: {type(error).__name__}: {redact_secrets(str(error))}")
        if bot is not None:
            await alerts.notify("Main", error)
        raise
    finally:
        state.request_shutdown()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if dp is not None:
            try:
                await dp.stop_polling()
            except Exception:
                pass
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:
                pass
        try:
            await client.disconnect()
        except Exception:
            pass
        db.close()
        log("MAIN", "Shutdown complete")


def ctx_bind_analyzer(analyzer: ProfileAnalyzer) -> None:
    from app.telegram.bot import ctx

    ctx.analyzer = analyzer


def main() -> None:
    asyncio.run(run_tracker())


if __name__ == "__main__":
    main()
