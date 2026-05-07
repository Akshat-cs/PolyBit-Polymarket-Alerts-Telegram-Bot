"""PolyBit Telegram bot: handlers, navigation, Add Alert review screen.

Stateless across restarts apart from `data/users.json` and `data/alerts.json`.
Per-user transient state (in-progress wizards, paged context) lives in
PTB's `user_data` dict and is lost on restart - that's intentional.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass

from telegram import (
    BotCommand,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, formatting as fmt
from .bitquery import BitqueryHTTP, MarketRow
from .store import Alert, AlertStore, UserStore, new_alert_id

logger = logging.getLogger(__name__)


# Awaiting flags stored in user_data["awaiting"]:
AWAIT_SEARCH = "search_query"
AWAIT_ADD_CUSTOM_PREFIX = "add_custom:"  # e.g. "add_custom:min_trade"
AWAIT_ADD_MARKET_SEARCH = "add_market_search"


def _set_last_list(context: ContextTypes.DEFAULT_TYPE, *parts: object) -> None:
    """Record the list view the user is currently on so a child view's Back
    button can return them to it at the same sort/page.

    Stored shapes (first element is the list kind):
        ("top", sort, page)
        ("alerts", page)
    """
    context.user_data["last_list"] = parts


def _get_last_list(context: ContextTypes.DEFAULT_TYPE) -> tuple | None:
    raw = context.user_data.get("last_list")
    return raw if isinstance(raw, tuple) else None


# Cached Telegram file_id for the welcome banner; populated on first upload
# so we don't re-send the binary on every /start.
_banner_file_id: str | None = None


@dataclass
class BotDeps:
    """Shared state injected into the Application via bot_data."""

    users: UserStore
    alerts: AlertStore
    bq: BitqueryHTTP
    market_index: dict[str, MarketRow]


def _deps(context: ContextTypes.DEFAULT_TYPE) -> BotDeps:
    return context.application.bot_data["deps"]


def _index_markets(deps: BotDeps, rows: list[MarketRow]) -> None:
    for r in rows:
        deps.market_index[r.market_id] = r


async def _resolve_market_from_url(deps: BotDeps, slug: str) -> MarketRow | None:
    """Resolve a Polymarket market slug → MarketRow via gamma-api.

    Gamma's `id` field equals Bitquery's `Question.MarketId` for the same
    market, so we can bind the alert directly without an extra Bitquery
    round-trip. Returns None when the slug isn't a published market.
    """
    gamma = getattr(deps.bq, "_gamma", None)
    if gamma is None:
        return None
    m = await gamma.resolve_by_slug(slug)
    if not m:
        return None
    market_id = str(m.get("id") or "").strip()
    if not market_id:
        return None
    title = str(m.get("question") or "").strip() or "(unknown market)"
    image = (m.get("image") or "").strip() or None
    condition_id = (m.get("conditionId") or "").strip() or None
    # Gamma exposes the on-chain question id as `questionID` (capital ID);
    # this is what DEXrabbit's URL slug uses.
    question_id = (m.get("questionID") or "").strip() or None
    canonical_url = gamma._url_from_market(m)
    row = MarketRow(
        market_id=market_id,
        title=title,
        image=image,
        condition_id=condition_id,
        question_id=question_id,
        canonical_url=canonical_url,
    )
    deps.market_index[market_id] = row
    return row


def _user_stats(deps: BotDeps, chat_id: str | int | None) -> dict:
    if chat_id is None:
        return {"active_alerts": 0, "paused_alerts": 0, "markets_tracked": 0, "triggered_alerts": 0}
    cid = str(chat_id)
    alerts = deps.alerts.for_chat(cid)
    active = sum(1 for a in alerts if not a.paused)
    paused = sum(1 for a in alerts if a.paused)
    markets = len({a.market_key for a in alerts if a.market_key})
    triggered = sum(1 for a in alerts if a.last_triggered_at is not None)
    return {
        "active_alerts": active,
        "paused_alerts": paused,
        "markets_tracked": markets,
        "triggered_alerts": triggered,
    }


def _welcome_for(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    deps = _deps(context)
    chat = update.effective_chat
    stats = _user_stats(deps, chat.id if chat else None)
    return fmt.welcome_text(stats)


async def _safe_edit(
    update: Update,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit-in-place if invoked from a callback; otherwise send new."""
    msg = update.effective_message
    if msg is None:
        return
    try:
        if update.callback_query is not None:
            await update.callback_query.edit_message_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        else:
            await msg.reply_html(
                text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        # Fallback: send a fresh message instead of failing silently.
        await msg.reply_html(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# -- /start, /help, /stop, main menu -----------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if chat is None:
        return
    deps = _deps(context)
    username = user.username if user else None
    added = await deps.users.add(str(chat.id), username=username)
    if added:
        logger.info("New user subscribed: %s (@%s)", chat.id, username or "?")
    context.user_data.pop("awaiting", None)

    # Telegram deep-link payload (https://t.me/<bot>?start=<payload>) —
    # external sites like dexrabbit.bitquery.io use this to land users on
    # a specific market screen instead of the welcome banner.
    #
    # Supported payloads:
    #   market_<MarketId>  → open that market's detail card
    args = context.args or []
    if args:
        payload = args[0].strip()
        if payload.startswith("market_"):
            market_id = payload[len("market_") :].strip()
            if market_id:
                logger.info(
                    "Deep-link /start from chat %s → market %s",
                    chat.id,
                    market_id,
                )
                await show_market_detail(update, context, market_id)
                return

    text = _welcome_for(update, context)
    keyboard = fmt.kb_main_menu()
    sent = await _send_welcome_banner(context, chat.id, text, keyboard)
    if not sent:
        await _safe_edit(update, text, keyboard)


async def _send_welcome_banner(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | str,
    caption: str,
    keyboard,
) -> bool:
    """Send the bot logo + welcome caption. Caches Telegram's file_id after
    the first upload so we don't re-upload the binary on every /start."""
    global _banner_file_id
    banner_path = config.WELCOME_BANNER_PATH
    if not banner_path.exists():
        return False
    bot = context.bot
    safe_caption = caption[:1024]
    try:
        if _banner_file_id:
            await bot.send_photo(
                chat_id=chat_id,
                photo=_banner_file_id,
                caption=safe_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return True
        with banner_path.open("rb") as f:
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=safe_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        if msg and msg.photo:
            _banner_file_id = msg.photo[-1].file_id
        return True
    except Exception:
        logger.exception("Failed to send welcome banner; falling back to text")
        return False


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_edit(update, fmt.help_text(), fmt.kb_main_menu())


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    deps = _deps(context)
    removed = await deps.users.remove(str(chat.id))
    if removed:
        text = "You're unsubscribed. Send /start to receive alerts again."
    else:
        text = "You weren't subscribed. Send /start to subscribe."
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(text)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting", None)
    context.user_data.pop("draft", None)
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text("Cancelled. Back to the main menu.")
    await _safe_edit(update, _welcome_for(update, context), fmt.kb_main_menu())


async def go_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting", None)
    context.user_data.pop("last_list", None)
    await _safe_edit(update, _welcome_for(update, context), fmt.kb_main_menu())


# -- Markets browsing --------------------------------------------------------


async def show_top(update: Update, context: ContextTypes.DEFAULT_TYPE, sort: str = "volume", page: int = 0) -> None:
    deps = _deps(context)
    try:
        rows = await deps.bq.top_markets(sort=sort)
    except Exception as e:
        logger.exception("top_markets failed")
        await _safe_edit(
            update,
            f"<b>Error loading markets:</b> {e}\n\n{fmt.FOOTER}",
            fmt.kb_main_menu(),
        )
        return
    _index_markets(deps, rows)
    sort_label = {
        "volume": "Volume",
        "traders": "Unique Traders",
        "trades": "Trade Count",
    }.get(sort, "Volume")
    title = f"📈 Top Markets by {sort_label} — last {fmt._window_label()}"
    text = fmt.fmt_market_list_intro(title, rows, page, config.PAGE_SIZE)
    keyboard = fmt.kb_top_markets(rows, sort, page, config.PAGE_SIZE)
    _set_last_list(context, "top", sort, page)
    await _safe_edit(update, text, keyboard)


async def prompt_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting"] = AWAIT_SEARCH
    text = (
        "<b>🔎 Search Markets</b>\n"
        "Send a keyword (e.g. <i>election</i>, <i>bitcoin</i>) and I'll look it up.\n"
        "Or paste a Polymarket link "
        "(e.g. <code>https://polymarket.com/event/…</code>) and I'll open that market directly.\n"
        "Send /cancel to abort.\n\n"
        f"{fmt.FOOTER}"
    )
    await _safe_edit(update, text, fmt.kb_main_menu())


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    deps = _deps(context)
    try:
        rows = await deps.bq.search_markets(query)
    except Exception as e:
        logger.exception("search_markets failed")
        msg = update.effective_message
        if msg is not None:
            await msg.reply_html(
                f"<b>Search failed:</b> {e}\n\n{fmt.FOOTER}",
                reply_markup=fmt.kb_main_menu(),
            )
        return
    _index_markets(deps, rows)
    title = f"🔎 Search results for “{query}”"
    text = fmt.fmt_market_list_intro(title, rows, 0, config.PAGE_SIZE)
    keyboard = fmt.kb_top_markets(rows, "volume", 0, config.PAGE_SIZE)
    msg = update.effective_message
    if msg is not None:
        await msg.reply_html(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


async def show_market_detail(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    market_id: str,
    *,
    refresh: bool = False,
) -> None:
    deps = _deps(context)
    try:
        title, image, prices, last_time, condition_id, question_id = await deps.bq.current_prices(market_id)
    except Exception as e:
        logger.exception("current_prices failed for %s", market_id)
        await _safe_edit(
            update,
            f"<b>Error loading market:</b> {e}\n\n{fmt.FOOTER}",
            fmt.kb_main_menu(),
        )
        return

    cached = deps.market_index.get(market_id)
    if title and cached and cached.title != title:
        cached.title = title
    if cached:
        title = title or cached.title
        image = image or cached.image

    stats_obj = cached
    if stats_obj is None or stats_obj.volume_usd is None or refresh:
        stats = await deps.bq.fetch_recent_stats(market_id)
        if stats_obj is None:
            from .bitquery import MarketRow
            stats_obj = MarketRow(market_id=market_id, title=title or "")
            deps.market_index[market_id] = stats_obj
        # Even when `stats` is an empty dict (quiet market: no trades in the
        # lookback window) we still want the formatter to render a zeroed
        # 1h Stats block, so always populate the row with explicit zeros and
        # let any non-empty values overwrite. Previously a {} return here
        # caused the whole stats section to silently disappear from the
        # deep-link / freshly-opened market detail card.
        stats_obj.volume_usd = (stats or {}).get("volume_usd", stats_obj.volume_usd) or 0.0
        stats_obj.trade_count = (stats or {}).get("trade_count", stats_obj.trade_count) or 0
        stats_obj.unique_buyers = (stats or {}).get("unique_buyers", stats_obj.unique_buyers) or 0

    if stats_obj is not None:
        if prices:
            stats_obj.prices = prices
        if last_time:
            stats_obj.last_trade_time = last_time
        if condition_id and not stats_obj.condition_id:
            stats_obj.condition_id = condition_id
        if question_id and not stats_obj.question_id:
            stats_obj.question_id = question_id

    # Resolve canonical Polymarket URL if we have a condition_id but no URL yet.
    if stats_obj is not None and stats_obj.condition_id and not stats_obj.canonical_url:
        try:
            await deps.bq._resolve_polymarket_urls([stats_obj])
        except Exception:
            logger.exception("resolve_polymarket_urls failed for detail card")

    canonical = stats_obj.canonical_url if stats_obj else None
    text = fmt.fmt_market_detail(
        title, image, market_id, prices, stats_obj, last_time, canonical_url=canonical
    )
    keyboard = fmt.kb_market_detail(market_id, title or (cached.title if cached else ""))
    # Use the Polymarket S3 question image directly as the preview URL.
    # Telegram renders raw image URLs as the link preview, which is fast
    # and reliable (S3 CDN, ~50KB) — much better than the DEXrabbit OG
    # endpoint which had cold-render times that exceeded Telegram's
    # preview-fetch timeout for niche markets.
    preview_url = image or (cached.image if cached else None)
    await _send_card(update, context, preview_url=preview_url, caption=text, keyboard=keyboard)


async def _send_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    preview_url: str | None,
    caption: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Render a market/alert card as a text message with a link preview.

    `preview_url` should point at the DEXrabbit market page; Telegram
    fetches its OG image (the rich market summary card) and renders it
    above the caption text. When `preview_url` is None we send a plain
    text message with previews disabled.

    Behavior:
    - If invoked from a callback query and the source message is text
      (not a photo), we edit it in place via `edit_message_text` so the
      chat history stays clean.
    - If the source is a photo (e.g. a stale send_photo message from an
      earlier session) edit-text fails, and we fall through to sending a
      fresh message.
    - Telegram's 4096-char text limit applies (much roomier than the
      1024-char caption limit we were stuck with under send_photo).
    """
    msg = update.effective_message
    if msg is None:
        return

    if preview_url:
        link_preview = LinkPreviewOptions(
            is_disabled=False,
            url=preview_url,
            prefer_large_media=True,
            show_above_text=True,
        )
    else:
        link_preview = LinkPreviewOptions(is_disabled=True)

    safe_text = caption[:4096]
    cq = update.callback_query

    if cq is not None and not getattr(msg, "photo", None):
        try:
            await cq.edit_message_text(
                text=safe_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                link_preview_options=link_preview,
            )
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            logger.debug("edit_message_text failed: %s", e)

    chat_id = msg.chat.id
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=safe_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            link_preview_options=link_preview,
        )
    except Exception:
        logger.exception("Failed to send card; falling back to plain text")
        await msg.reply_html(
            safe_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# -- Alerts list / detail ----------------------------------------------------


async def show_alerts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    *,
    refresh: bool = False,
) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    deps = _deps(context)
    alerts = deps.alerts.for_chat(str(chat.id))

    if not alerts:
        # No alerts → no market data to fetch, no OG image to render.
        text = fmt.fmt_alerts_list(alerts, page=page, page_size=config.PAGE_SIZE)
        keyboard = fmt.kb_alerts_list(alerts, page, config.PAGE_SIZE)
        _set_last_list(context, "alerts", page)
        await _send_card(update, context, preview_url=None, caption=text, keyboard=keyboard)
        return

    if refresh:
        # Drop cached prices/stats so the next _alert_market_data hits
        # Bitquery fresh.
        deps.bq.cache_clear()

    # Enrich only the page-visible bound alerts so we don't fan out to
    # Bitquery for every alert the user owns. PAGE_SIZE caps concurrency
    # at ~5, which is well within our HTTP client's headroom.
    total_pages = max(1, (len(alerts) + config.PAGE_SIZE - 1) // config.PAGE_SIZE)
    safe_page = max(0, min(page, total_pages - 1))
    start = safe_page * config.PAGE_SIZE
    page_alerts = alerts[start : start + config.PAGE_SIZE]
    bound = [a for a in page_alerts if a.market_key]
    market_data_by_key: dict[str, dict] = {}
    if bound:
        results = await asyncio.gather(
            *[_alert_market_data(deps, a) for a in bound],
            return_exceptions=True,
        )
        for a, md in zip(bound, results):
            if isinstance(md, dict):
                market_data_by_key[a.market_key] = md

    # Telegram allows at most one link preview per message. Use the first
    # page-visible bound alert's Polymarket question image so the message
    # gets the same image-preview UX as the single-market detail screen.
    preview_url: str | None = None
    for a in page_alerts:
        if not a.market_key:
            continue
        md = market_data_by_key.get(a.market_key)
        if not md:
            continue
        img = md.get("image")
        if img:
            preview_url = img
            break

    text = fmt.fmt_alerts_list(
        alerts,
        page=page,
        page_size=config.PAGE_SIZE,
        market_data_by_key=market_data_by_key,
    )
    keyboard = fmt.kb_alerts_list(alerts, page, config.PAGE_SIZE)
    _set_last_list(context, "alerts", page)
    await _send_card(update, context, preview_url=preview_url, caption=text, keyboard=keyboard)


async def show_alert_detail(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    alert_id: str,
    *,
    refresh: bool = False,
) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    deps = _deps(context)
    alert = deps.alerts.get(alert_id)
    if alert is None or alert.chat_id != str(chat.id):
        await _safe_edit(
            update,
            f"<b>Alert not found.</b>\n\n{fmt.FOOTER}",
            fmt.kb_main_menu(),
        )
        return

    if refresh:
        # Drop any cached prices/stats for this market so the next
        # _alert_market_data call hits Bitquery fresh.
        deps.bq.cache_clear()

    market_data = await _alert_market_data(deps, alert)
    text = fmt.fmt_alert_detail(alert, market_data=market_data)
    keyboard = fmt.kb_alert_detail(alert)
    preview_url: str | None = None
    if market_data:
        preview_url = market_data.get("image")
    await _send_card(update, context, preview_url=preview_url, caption=text, keyboard=keyboard)


async def _alert_market_data(deps: BotDeps, alert: Alert) -> dict | None:
    """Fetch current prices, 1h stats, image and canonical URL for an alert's
    bound market.

    Returns:
      None — only when the alert isn't bound to a market (no market to
        fetch state for; the formatter shows "Market: any").
      dict — for any bound alert, even if every individual fetch failed.
        Empty/missing fields default to falsy values so the formatter can
        render zeros instead of dropping the whole "Current Prices / 1h
        Stats" section. The renderer relies on this dict always existing
        for bound alerts.
    """
    if not alert.market_key:
        return None
    market_id = alert.market_key
    title: str | None = None
    image: str | None = None
    prices = []
    condition_id: str | None = None
    question_id: str | None = None

    cached = deps.market_index.get(market_id)
    if cached:
        title = cached.title or None
        image = cached.image or None
        condition_id = cached.condition_id
        question_id = cached.question_id

    try:
        t, img, p, _last, cid, qid = await deps.bq.current_prices(market_id)
        title = t or title
        image = img or image
        if p:
            prices = p
        condition_id = cid or condition_id
        question_id = qid or question_id
    except Exception:
        logger.exception("current_prices failed for alert %s", alert.id)

    stats: dict = {}
    try:
        stats = await deps.bq.fetch_recent_stats(market_id) or {}
    except Exception:
        logger.exception("fetch_recent_stats failed for alert %s", alert.id)

    canonical: str | None = cached.canonical_url if cached else None
    if not canonical and condition_id and deps.bq._gamma is not None:
        try:
            urls = await deps.bq._gamma.resolve([condition_id])
            canonical = urls.get(condition_id.lower())
        except Exception:
            logger.exception("gamma resolve failed for alert %s", alert.id)

    if cached is None:
        deps.market_index[market_id] = MarketRow(
            market_id=market_id,
            title=title or alert.market_title or "",
            image=image,
            condition_id=condition_id,
            question_id=question_id,
            canonical_url=canonical,
        )
    else:
        if title and not cached.title:
            cached.title = title
        if image and not cached.image:
            cached.image = image
        if condition_id and not cached.condition_id:
            cached.condition_id = condition_id
        if question_id and not cached.question_id:
            cached.question_id = question_id
        if canonical and not cached.canonical_url:
            cached.canonical_url = canonical

    return {
        "title": title or alert.market_title,
        "image": image,
        "prices": prices,
        "stats": stats,
        "canonical_url": canonical,
        "condition_id": condition_id,
        "question_id": question_id,
    }


async def toggle_alert(update: Update, context: ContextTypes.DEFAULT_TYPE, alert_id: str) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    deps = _deps(context)
    alert = deps.alerts.get(alert_id)
    if alert is None or alert.chat_id != str(chat.id):
        return
    await deps.alerts.set_paused(alert_id, str(chat.id), not alert.paused)
    await show_alert_detail(update, context, alert_id)


async def delete_alert(update: Update, context: ContextTypes.DEFAULT_TYPE, alert_id: str) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    deps = _deps(context)
    await deps.alerts.delete(alert_id, str(chat.id))
    msg = update.effective_message
    if msg is not None:
        try:
            await update.callback_query.answer("Alert deleted")  # type: ignore[union-attr]
        except Exception:
            pass
    await show_alerts(update, context, page=0)


# -- Add Alert review screen + chips wizard ---------------------------------


def _draft(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Return the in-progress Add-Alert draft from per-user PTB state.

    Internal-only keys (prefixed with `_`) are stripped before persisting:
      _outcomes      : list[str] — outcome labels for the chip picker
      market_locked  : bool      — hide 'Set Market' when wizard was opened
                                   from a market detail's 'Set Alert' button
    """
    draft = context.user_data.get("draft")
    if not isinstance(draft, dict):
        draft = {
            "market_key": None,
            "market_title": None,
            "outcome": None,
            "min_trade_amount_usd": None,
            "max_trade_amount_usd": None,
            "min_price_usd": None,
            "max_price_usd": None,
            "trader": None,
            "market_locked": False,
            "_outcomes": [],
        }
        context.user_data["draft"] = draft
    return draft


_FIELD_LABELS = {
    "outcome": "Outcome",
    "min_trade": "Min Trade USD",
    "max_trade": "Max Trade USD",
    "min_price": "Min Price",
    "max_price": "Max Price",
    "trader": "Trader Wallet",
    "market": "Market",
}

_FIELD_TO_KEY = {
    "min_trade": "min_trade_amount_usd",
    "max_trade": "max_trade_amount_usd",
    "min_price": "min_price_usd",
    "max_price": "max_price_usd",
    "trader": "trader",
}


async def add_alert_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    market_id: str | None = None,
) -> None:
    """Open the Add-Alert wizard.

    When `market_id` is provided the wizard was launched from a market
    detail's 'Set Alert on this Market' button, so we lock the market
    binding (hiding the Market row in the review screen) and pre-populate
    the outcome chips snapshot for the next step.
    """
    draft = _draft(context)
    for k in list(draft.keys()):
        if k == "_outcomes":
            draft[k] = []
        elif k == "market_locked":
            draft[k] = False
        else:
            draft[k] = None
    context.user_data.pop("awaiting", None)
    deps = _deps(context)
    if market_id:
        row = deps.market_index.get(market_id)
        draft["market_key"] = market_id
        draft["market_title"] = row.title if row else None
        draft["market_locked"] = True
        if row is None or not row.prices:
            try:
                title, image, prices, _last, cid, qid = await deps.bq.current_prices(market_id)
                if title:
                    draft["market_title"] = title
                    if row is None:
                        row = MarketRow(
                            market_id=market_id,
                            title=title,
                            image=image,
                            condition_id=cid,
                            question_id=qid,
                        )
                        deps.market_index[market_id] = row
                if prices and row is not None and not row.prices:
                    row.prices = prices
                if row is not None:
                    if cid and not row.condition_id:
                        row.condition_id = cid
                    if qid and not row.question_id:
                        row.question_id = qid
            except Exception:
                pass
        if row and row.prices:
            draft["_outcomes"] = [p.label for p in fmt._sort_outcomes(row.prices)]
    await render_add_review(update, context)


async def render_add_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = _draft(context)
    text = fmt.fmt_add_review(draft)
    keyboard = fmt.kb_add_review(draft)
    await _safe_edit(update, text, keyboard)


async def add_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str) -> None:
    if field == "market":
        deps = _deps(context)
        try:
            top = await deps.bq.top_markets(sort="volume")
        except Exception:
            top = []
        _index_markets(deps, top)
        text = (
            "<b>Pick a market</b>\n"
            "Choose from top markets, search by keyword, or skip to leave market unset.\n\n"
            f"{fmt.FOOTER}"
        )
        await _safe_edit(update, text, fmt.kb_chips_market(top))
        return
    if field == "outcome":
        draft = _draft(context)
        market_id = draft.get("market_key")
        if not market_id:
            text = (
                "<b>Outcome filter needs a market</b>\n"
                "Pick a market first, then come back to choose an outcome.\n\n"
                f"{fmt.FOOTER}"
            )
            await _safe_edit(update, text, fmt.kb_add_review(draft))
            return
        deps = _deps(context)
        prices = []
        cached = deps.market_index.get(market_id)
        if cached and cached.prices:
            prices = cached.prices
        else:
            try:
                title, image, prices, _last, cid, qid = await deps.bq.current_prices(market_id)
                if cached is None and (title or image):
                    deps.market_index[market_id] = MarketRow(
                        market_id=market_id,
                        title=title or "",
                        image=image,
                        prices=prices or [],
                        condition_id=cid,
                        question_id=qid,
                    )
                elif cached is not None:
                    if prices:
                        cached.prices = prices
                    if cid and not cached.condition_id:
                        cached.condition_id = cid
                    if qid and not cached.question_id:
                        cached.question_id = qid
            except Exception:
                logger.exception("current_prices failed for outcome chips")
                prices = []
        if not prices:
            text = (
                "<b>No outcomes available yet</b>\n"
                "This market doesn't have outcome data we can pull right now.\n\n"
                f"{fmt.FOOTER}"
            )
            await _safe_edit(update, text, fmt.kb_add_review(draft))
            return
        sorted_labels = [p.label for p in fmt._sort_outcomes(prices)]
        draft["_outcomes"] = sorted_labels
        text = (
            "<b>Pick an outcome</b>\n"
            "Only fire alerts for trades on this side of the market. "
            "Skip to match any outcome.\n\n"
            f"{fmt.FOOTER}"
        )
        await _safe_edit(update, text, fmt.kb_chips_outcome(prices))
        return
    if field in ("min_trade", "max_trade"):
        text = fmt.fmt_chips_prompt(_FIELD_LABELS[field])
        await _safe_edit(update, text, fmt.kb_chips_usd(field))
        return
    if field in ("min_price", "max_price"):
        text = fmt.fmt_chips_prompt(_FIELD_LABELS[field])
        await _safe_edit(update, text, fmt.kb_chips_price(field))
        return
    if field == "trader":
        text = (
            "<b>Set trader filter</b>\n"
            "Match trades where this 0x… address is the buyer or seller.\n\n"
            f"{fmt.FOOTER}"
        )
        await _safe_edit(update, text, fmt.kb_chips_trader())
        return


async def add_pick(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, value: str) -> None:
    draft = _draft(context)

    if field == "market":
        if value == "skip":
            draft["market_key"] = None
            draft["market_title"] = None
            draft["outcome"] = None
            draft["_outcomes"] = []
            await render_add_review(update, context)
            return
        if value == "search":
            context.user_data["awaiting"] = AWAIT_ADD_MARKET_SEARCH
            text = (
                "<b>Find a market</b>\n"
                "Send a <b>keyword</b> (e.g. <i>bitcoin</i>, <i>election</i>) "
                "or paste a <b>Polymarket link</b> "
                "(e.g. <code>https://polymarket.com/event/…</code>) "
                "and I'll bind that market to your alert.\n\n"
                "/cancel to abort.\n\n"
                f"{fmt.FOOTER}"
            )
            await _safe_edit(update, text, None)
            return
        deps = _deps(context)
        row = deps.market_index.get(value)
        if draft.get("market_key") != value:
            draft["outcome"] = None
            draft["_outcomes"] = [p.label for p in fmt._sort_outcomes(row.prices)] if (row and row.prices) else []
        draft["market_key"] = value
        draft["market_title"] = row.title if row else None
        await render_add_review(update, context)
        return

    if field == "outcome":
        if value == "skip":
            draft["outcome"] = None
            await render_add_review(update, context)
            return
        try:
            idx = int(value)
        except ValueError:
            await render_add_review(update, context)
            return
        outcomes = draft.get("_outcomes") or []
        if 0 <= idx < len(outcomes):
            draft["outcome"] = outcomes[idx]
        await render_add_review(update, context)
        return

    if field in ("min_trade", "max_trade", "min_price", "max_price"):
        key = _FIELD_TO_KEY[field]
        if value == "skip":
            draft[key] = None
            await render_add_review(update, context)
            return
        if value == "custom":
            context.user_data["awaiting"] = f"{AWAIT_ADD_CUSTOM_PREFIX}{field}"
            hint = (
                "Type just the number, e.g. 100, 1000, 10000."
                if field.endswith("trade")
                else "Type the price in cents (1-99), e.g. 25, 50, 75."
            )
            text = fmt.fmt_custom_prompt(_FIELD_LABELS[field], hint)
            await _safe_edit(update, text, None)
            return
        try:
            num = float(value)
        except ValueError:
            await render_add_review(update, context)
            return
        draft[key] = num
        await render_add_review(update, context)
        return

    if field == "trader":
        if value == "skip":
            draft["trader"] = None
            await render_add_review(update, context)
            return
        if value == "custom":
            context.user_data["awaiting"] = f"{AWAIT_ADD_CUSTOM_PREFIX}trader"
            text = fmt.fmt_custom_prompt(
                "Trader wallet",
                "Send a 0x… address (40 hex chars).",
            )
            await _safe_edit(update, text, None)
            return


async def add_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    draft = _draft(context)
    deps = _deps(context)

    if not any(draft.get(k) is not None for k in (
        "market_key",
        "outcome",
        "min_trade_amount_usd",
        "max_trade_amount_usd",
        "min_price_usd",
        "max_price_usd",
        "trader",
    )):
        try:
            await update.callback_query.answer(  # type: ignore[union-attr]
                "Please set at least one filter.", show_alert=True
            )
        except Exception:
            pass
        return

    lo, hi = draft.get("min_trade_amount_usd"), draft.get("max_trade_amount_usd")
    if lo is not None and hi is not None and lo > hi:
        try:
            await update.callback_query.answer(  # type: ignore[union-attr]
                "Min Trade USD can't be greater than Max.", show_alert=True
            )
        except Exception:
            pass
        return

    lo, hi = draft.get("min_price_usd"), draft.get("max_price_usd")
    if lo is not None and hi is not None and lo > hi:
        try:
            await update.callback_query.answer(  # type: ignore[union-attr]
                "Min Price can't be greater than Max.", show_alert=True
            )
        except Exception:
            pass
        return

    alert = Alert(
        id=new_alert_id(),
        chat_id=str(chat.id),
        market_key=draft.get("market_key"),
        market_title=draft.get("market_title"),
        outcome=(draft.get("outcome") or None),
        min_trade_amount_usd=draft.get("min_trade_amount_usd"),
        max_trade_amount_usd=draft.get("max_trade_amount_usd"),
        min_price_usd=draft.get("min_price_usd"),
        max_price_usd=draft.get("max_price_usd"),
        trader=(draft.get("trader") or None),
    )
    await deps.alerts.add(alert)
    await deps.users.add(str(chat.id), username=update.effective_user.username if update.effective_user else None)
    context.user_data.pop("awaiting", None)
    context.user_data.pop("draft", None)
    await _safe_edit(update, fmt.fmt_alert_saved(alert), fmt.kb_alert_detail(alert))


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting", None)
    context.user_data.pop("draft", None)
    await _safe_edit(update, _welcome_for(update, context), fmt.kb_main_menu())


# -- Message router (free-text input during wizards/search) -----------------


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or msg.text is None:
        return
    text = msg.text.strip()
    if not text or text.startswith("/"):
        return
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        await msg.reply_html(
            "Use /start to open the menu, or /help for commands.",
        )
        return

    if awaiting == AWAIT_SEARCH:
        context.user_data.pop("awaiting", None)
        # If the user pasted a Polymarket market link, jump straight to the
        # market detail card instead of running a keyword search.
        slug = fmt.parse_polymarket_url(text)
        if slug:
            deps = _deps(context)
            row = await _resolve_market_from_url(deps, slug)
            if row is None:
                await msg.reply_html(
                    "<b>Couldn't find that market on Polymarket.</b>\n"
                    "Double-check the link, or send a keyword instead.\n\n"
                    f"{fmt.FOOTER}",
                    reply_markup=fmt.kb_main_menu(),
                )
                return
            _index_markets(deps, [row])
            await show_market_detail(update, context, row.market_id)
            return
        await run_search(update, context, text)
        return

    if awaiting == AWAIT_ADD_MARKET_SEARCH:
        deps = _deps(context)
        slug = fmt.parse_polymarket_url(text)
        if slug:
            context.user_data.pop("awaiting", None)
            row = await _resolve_market_from_url(deps, slug)
            if row is None:
                await msg.reply_html(
                    "<b>Couldn't find that market on Polymarket.</b>\n"
                    "Double-check the link, or send a keyword instead.\n\n"
                    f"{fmt.FOOTER}",
                    reply_markup=fmt.kb_chips_market([]),
                )
                context.user_data["awaiting"] = AWAIT_ADD_MARKET_SEARCH
                return
            draft = _draft(context)
            if draft.get("market_key") != row.market_id:
                draft["outcome"] = None
                draft["_outcomes"] = []
            draft["market_key"] = row.market_id
            draft["market_title"] = row.title
            try:
                _t, _img, prices, _last, _cid, qid = await deps.bq.current_prices(row.market_id)
                if qid:
                    if row.market_id in deps.market_index and not deps.market_index[row.market_id].question_id:
                        deps.market_index[row.market_id].question_id = qid
                    if not row.question_id:
                        row.question_id = qid
                if prices:
                    if row.market_id in deps.market_index and not deps.market_index[row.market_id].prices:
                        deps.market_index[row.market_id].prices = prices
                    draft["_outcomes"] = [p.label for p in fmt._sort_outcomes(prices)]
            except Exception:
                logger.exception("current_prices failed for URL-bound market")
            await msg.reply_html(
                f"<b>Market bound:</b> {html.escape(row.title)}",
            )
            await render_add_review(update, context)
            return

        context.user_data.pop("awaiting", None)
        try:
            rows = await deps.bq.search_markets(text)
        except Exception as e:
            logger.exception("search_markets failed")
            await msg.reply_html(
                f"<b>Search failed:</b> {e}\n\n{fmt.FOOTER}",
                reply_markup=fmt.kb_main_menu(),
            )
            return
        _index_markets(deps, rows)
        intro = (
            f"<b>Pick a market</b> ({len(rows)} match)" if rows else "<b>No matches</b>"
        )
        if not rows:
            await msg.reply_html(
                intro
                + "\n\nTry a different keyword, or paste a Polymarket link.\n\n"
                + f"{fmt.FOOTER}",
                reply_markup=fmt.kb_chips_market([]),
            )
            return
        await msg.reply_html(
            intro + f"\n\n{fmt.FOOTER}",
            reply_markup=fmt.kb_chips_market(rows),
        )
        return

    if awaiting.startswith(AWAIT_ADD_CUSTOM_PREFIX):
        field = awaiting[len(AWAIT_ADD_CUSTOM_PREFIX):]
        draft = _draft(context)
        if field in ("min_trade", "max_trade"):
            v = fmt.parse_usd(text)
            if v is None or v < 0:
                await msg.reply_html(
                    "That doesn't look like a USD amount. "
                    "Type just the number, e.g. <code>1000</code>."
                )
                return
            draft[_FIELD_TO_KEY[field]] = float(v)
        elif field in ("min_price", "max_price"):
            v = fmt.parse_price(text)
            if v is None or not (0 <= v <= 1):
                await msg.reply_html(
                    "Price must be between 1¢ and 99¢. "
                    "Type the price in cents, e.g. <code>25</code>, <code>50</code>, <code>75</code>."
                )
                return
            draft[_FIELD_TO_KEY[field]] = float(v)
        elif field == "trader":
            v = fmt.parse_address(text)
            if v is None:
                await msg.reply_html(
                    "That doesn't look like a wallet. Send a 0x address with 40 hex characters."
                )
                return
            draft["trader"] = v
        context.user_data.pop("awaiting", None)
        await msg.reply_html("Got it. Updating your alert…")
        await render_add_review(update, context)
        return


# -- Callback router ---------------------------------------------------------


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if cq is None:
        return
    try:
        await cq.answer()
    except Exception:
        pass
    data = (cq.data or "").strip()
    if not data:
        return
    parts = data.split("|")
    head = parts[0]

    if head == fmt.CB_NOOP:
        # Page-indicator chip (e.g. "Page 2/4") — already ack'd above.
        return
    if head == fmt.CB_BACK:
        last = _get_last_list(context)
        if not last:
            await go_main(update, context)
            return
        kind = last[0]
        if kind == "top" and len(last) >= 3:
            await show_top(update, context, sort=str(last[1]), page=int(last[2]))
        elif kind == "alerts" and len(last) >= 2:
            await show_alerts(update, context, page=int(last[1]))
        else:
            await go_main(update, context)
        return
    if head == fmt.CB_MAIN:
        await go_main(update, context)
        return
    if head == fmt.CB_TOP:
        if len(parts) > 1 and parts[1] == "refresh":
            sort = parts[2] if len(parts) > 2 else "volume"
            page = int(parts[3]) if len(parts) > 3 else 0
            deps = _deps(context)
            deps.bq.cache_clear(prefix="top")
            await show_top(update, context, sort=sort, page=page)
            return
        sort = parts[1] if len(parts) > 1 else "volume"
        page = int(parts[2]) if len(parts) > 2 else 0
        await show_top(update, context, sort=sort, page=page)
        return
    if head == fmt.CB_SEARCH:
        await prompt_search(update, context)
        return
    if head == fmt.CB_ALERTS:
        if len(parts) > 1 and parts[1] == "refresh":
            page = int(parts[2]) if len(parts) > 2 else 0
            await show_alerts(update, context, page=page, refresh=True)
            return
        page = int(parts[1]) if len(parts) > 1 else 0
        await show_alerts(update, context, page=page)
        return
    if head == fmt.CB_HELP:
        await help_cmd(update, context)
        return
    if head == "market":
        if len(parts) > 1:
            if parts[1] == "refresh" and len(parts) > 2:
                deps = _deps(context)
                deps.bq.cache_clear()
                await show_market_detail(update, context, parts[2], refresh=True)
                return
            await show_market_detail(update, context, parts[1])
        return
    if head == "alert":
        if len(parts) < 3:
            return
        action, alert_id = parts[1], parts[2]
        if action == "view":
            await show_alert_detail(update, context, alert_id)
        elif action == "toggle":
            await toggle_alert(update, context, alert_id)
        elif action == "delete":
            await delete_alert(update, context, alert_id)
        elif action == "refresh":
            await show_alert_detail(update, context, alert_id, refresh=True)
        return
    if head == fmt.CB_ADD:
        sub = parts[1] if len(parts) > 1 else "new"
        if sub == "new":
            await add_alert_start(update, context, market_id=None)
            return
        if sub == "market" and len(parts) > 2:
            await add_alert_start(update, context, market_id=parts[2])
            return
        if sub == "review":
            await render_add_review(update, context)
            return
        if sub == "edit" and len(parts) > 2:
            await add_edit_field(update, context, parts[2])
            return
        if sub == "pick" and len(parts) > 3:
            await add_pick(update, context, parts[2], parts[3])
            return
        if sub == "save":
            await add_save(update, context)
            return
        if sub == "cancel":
            await add_cancel(update, context)
            return


# -- Application factory -----------------------------------------------------


async def post_init(app: Application) -> None:
    commands = [
        BotCommand("start", "🏠 Home & main menu"),
        BotCommand("topmarkets", f"📈 Top markets (last {fmt._window_label()})"),
        BotCommand("search", "🔎 Search markets by keyword"),
        BotCommand("myalerts", "🔔 Your active alerts"),
        BotCommand("addalert", "➕ Create a new alert"),
        BotCommand("cancel", "❌ Cancel current action"),
        BotCommand("stop", "⏹ Stop receiving alerts"),
        BotCommand("help", "❓ Help and commands"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except Exception:
        logger.exception("set_my_commands failed")


def build_application(
    bot_token: str,
    deps: BotDeps,
) -> Application:
    app = (
        Application.builder()
        .token(bot_token)
        .post_init(post_init)
        .build()
    )
    app.bot_data["deps"] = deps

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("topmarkets", lambda u, c: show_top(u, c, "volume", 0)))
    app.add_handler(CommandHandler("search", lambda u, c: prompt_search(u, c)))
    app.add_handler(CommandHandler("myalerts", lambda u, c: show_alerts(u, c, 0)))
    app.add_handler(CommandHandler("addalert", lambda u, c: add_alert_start(u, c, None)))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app
