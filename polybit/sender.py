"""Outbound Telegram sender with per-chat throttle and 429 handling.

Trade alerts can fan out to many users at once. Telegram limits us to
roughly 1 message/second per chat and ~30 messages/second overall.
We push (chat_id, OutboundMessage) onto a queue and let a small pool of
workers drain it, while a per-chat tracker enforces a minimum gap between
sends to the same chat. On HTTP 429 we honor the server's `retry_after`."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from telegram import InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import Application

from . import config

logger = logging.getLogger(__name__)


@dataclass
class OutboundMessage:
    """A queued Telegram message.

    `preview_url` (when set) drives Telegram's link preview — we point it
    at the Polymarket-hosted question image (S3) so Telegram renders the
    image directly above the alert text. When None, link previews are
    disabled.
    """

    chat_id: str
    text: str
    reply_markup: Optional[InlineKeyboardMarkup] = None
    preview_url: Optional[str] = None
    parse_mode: str = ParseMode.HTML
    attempts: int = 0


class TelegramSender:
    def __init__(self, app: Application, num_workers: int = config.TELEGRAM_SENDER_WORKERS) -> None:
        self._app = app
        self._queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._num_workers = num_workers
        self._workers: list[asyncio.Task] = []
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._last_send: dict[str, float] = {}
        self._stop = asyncio.Event()

    async def start(self) -> None:
        for i in range(self._num_workers):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"tg-sender-{i}"))

    async def stop(self) -> None:
        self._stop.set()
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()

    async def enqueue(self, msg: OutboundMessage) -> None:
        await self._queue.put(msg)

    def _lock_for(self, chat_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _wait_chat_window(self, chat_id: str) -> None:
        last = self._last_send.get(chat_id)
        if last is None:
            return
        delay = config.TELEGRAM_PER_CHAT_INTERVAL_SECONDS - (time.time() - last)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _send_one(self, msg: OutboundMessage) -> bool:
        bot = self._app.bot
        if msg.preview_url:
            link_preview = LinkPreviewOptions(
                is_disabled=False,
                url=msg.preview_url,
                prefer_large_media=True,
                show_above_text=True,
            )
        else:
            link_preview = LinkPreviewOptions(is_disabled=True)
        try:
            await bot.send_message(
                chat_id=msg.chat_id,
                text=msg.text[:4096],
                parse_mode=msg.parse_mode,
                reply_markup=msg.reply_markup,
                link_preview_options=link_preview,
            )
            return True
        except RetryAfter as e:
            wait = float(getattr(e, "retry_after", 5))
            logger.warning("Telegram 429; sleeping %.1fs and requeueing", wait)
            await asyncio.sleep(wait + 0.5)
            msg.attempts += 1
            if msg.attempts < 4:
                await self._queue.put(msg)
            else:
                logger.error("Dropped message after %d retries (chat %s)", msg.attempts, msg.chat_id)
            return False
        except TelegramError as e:
            logger.warning("Telegram error sending to %s: %s", msg.chat_id, e)
            return False

    async def _worker(self, idx: int) -> None:
        while not self._stop.is_set():
            try:
                msg = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                async with self._lock_for(msg.chat_id):
                    await self._wait_chat_window(msg.chat_id)
                    sent = await self._send_one(msg)
                    self._last_send[msg.chat_id] = time.time()
                    if not sent:
                        await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Sender worker %d failed; continuing", idx)
            finally:
                self._queue.task_done()
