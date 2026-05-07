"""PolyBit entrypoint.

Runs three concurrent tasks on a single asyncio loop:
1. python-telegram-bot Application polling Telegram for updates.
2. Bitquery WebSocket streamer pushing trades into the in-process matcher.
3. Telegram sender draining the outbound queue with per-chat throttling.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from . import bot as bot_mod
from . import config, formatting as fmt
from .bitquery import BitqueryHTTP, BitqueryStreamer, GammaResolver, TradeEvent
from .matcher import match_trade
from .sender import OutboundMessage, TelegramSender
from .store import AlertStore, UserStore

logger = logging.getLogger(__name__)


async def _run() -> None:
    config.load_env()
    bq_token = config.get_bitquery_token()
    tg_token = config.get_telegram_token()
    if not bq_token:
        raise SystemExit("Missing BITQUERY_TOKEN in environment / .env")
    if not tg_token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN in environment / .env")

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    users = UserStore(config.USERS_FILE)
    users.load()
    alerts = AlertStore(config.ALERTS_FILE)
    alerts.load()

    logger.info(
        "PolyBit starting up: %d user(s), %d alert(s)",
        users.count(),
        sum(1 for _ in alerts.active_alerts()),
    )

    async with GammaResolver() as gamma, BitqueryHTTP(bq_token, gamma=gamma) as bq_http:
        deps = bot_mod.BotDeps(
            users=users,
            alerts=alerts,
            bq=bq_http,
            market_index={},
        )
        application = bot_mod.build_application(tg_token, deps)
        sender = TelegramSender(application)
        streamer = BitqueryStreamer(bq_token)

        async def on_trade(event: TradeEvent) -> None:
            active = list(alerts.active_alerts())
            if not active:
                return
            matches = match_trade(event, active)
            if not matches:
                return
            alerts.mark_triggered(m.alert.id for m in matches)

            # Best-effort canonical URL: prefer cached row, otherwise resolve
            # this single market on demand so future trades hit a warm cache.
            canonical = None
            cached_row = deps.market_index.get(event.market_id)
            if cached_row and cached_row.canonical_url:
                canonical = cached_row.canonical_url
            elif event.condition_id:
                try:
                    urls = await gamma.resolve([event.condition_id])
                    canonical = urls.get(event.condition_id.lower())
                    if canonical and cached_row and not cached_row.canonical_url:
                        cached_row.canonical_url = canonical
                except Exception:
                    logger.debug("gamma resolve failed for trade %s", event.tx_hash)

            for m in matches:
                text, kb, preview = fmt.fmt_trade_notification(m, canonical_url=canonical)
                await sender.enqueue(
                    OutboundMessage(
                        chat_id=m.alert.chat_id,
                        text=text,
                        reply_markup=kb,
                        preview_url=preview,
                    )
                )

        streamer.add_handler(on_trade)

        async def persist_alerts_loop() -> None:
            while True:
                try:
                    await asyncio.sleep(config.ALERTS_PERSIST_DEBOUNCE_SECONDS)
                    await alerts.persist_if_dirty(
                        min_interval=config.ALERTS_PERSIST_DEBOUNCE_SECONDS
                    )
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.exception("persist_alerts_loop error")

        await application.initialize()
        # PTB only calls post_init from run_polling()/run_webhook(), so when
        # we drive the lifecycle manually we have to invoke it ourselves.
        await bot_mod.post_init(application)
        await application.start()
        await application.updater.start_polling(
            allowed_updates=["message", "callback_query"],
        )
        await sender.start()
        stream_task = asyncio.create_task(streamer.run(), name="bitquery-stream")
        persist_task = asyncio.create_task(persist_alerts_loop(), name="persist-alerts")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _stop_handler(*_: object) -> None:
            stop_event.set()

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, _stop_handler)
            except (NotImplementedError, RuntimeError):
                pass

        logger.info("PolyBit is running. Press Ctrl+C to stop.")
        try:
            await stop_event.wait()
        finally:
            logger.info("Shutting down…")
            streamer.stop()
            stream_task.cancel()
            persist_task.cancel()
            with suppress(asyncio.CancelledError):
                await stream_task
            with suppress(asyncio.CancelledError):
                await persist_task
            await sender.stop()
            try:
                await application.updater.stop()
            except Exception:
                pass
            try:
                await application.stop()
            except Exception:
                pass
            try:
                await application.shutdown()
            except Exception:
                pass
            await alerts.persist_if_dirty(min_interval=0)


def main() -> None:
    config.configure_logging()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
