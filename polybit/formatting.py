"""HTML message builders, URL helpers, parsing and keyboard factories."""

from __future__ import annotations

import html
import re
from typing import Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import config
from .bitquery import MarketRow, OutcomePrice, TradeEvent
from .matcher import Match
from .store import Alert

POLYMARKET_BASE = "https://polymarket.com"
POLYGONSCAN_TX = "https://polygonscan.com/tx"
FOOTER = "<i>Powered by Bitquery</i>"


def polymarket_profile_url(address: str | None) -> str | None:
    """Polymarket public profile page for a 0x… wallet address."""
    if not address:
        return None
    a = address.strip().lower()
    if not a.startswith("0x") or len(a) != 42:
        return None
    return f"{POLYMARKET_BASE}/profile/{a}"


def _addr_link(address: str | None) -> str:
    """Render an address as a clickable Polymarket-profile link, falling back
    to a plain code block if the address looks malformed."""
    short = short_addr(address)
    url = polymarket_profile_url(address)
    if not url:
        return f"<code>{html.escape(short)}</code>"
    return f'<a href="{html.escape(url, quote=True)}"><code>{html.escape(short)}</code></a>'


def _window_label() -> str:
    """Human label for the stats window, e.g. '1h', '24h'."""
    h = config.STATS_LOOKBACK_HOURS
    if h % 24 == 0:
        days = h // 24
        return f"{days}d" if days > 1 else "24h"
    return f"{h}h"


def short_addr(addr: str | None) -> str:
    if not addr:
        return "?"
    a = addr.strip()
    if len(a) <= 10:
        return a
    return f"{a[:6]}…{a[-4:]}"


def slugify_title(title: str) -> str:
    """General-purpose slug: replace any run of non-alphanumerics with `-`.

    Used by the Polymarket fallback URL (`/event/<slug>`) since Polymarket's
    own slug rules collapse punctuation to dashes.
    """
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def polymarket_event_url(
    market_id: str | None,
    title: str | None,
    canonical_url: str | None = None,
) -> str:
    """Link to the Polymarket market page.

    Prefers the canonical URL resolved via gamma-api (`canonical_url`).
    Falls back to a slugified title — that's right for direct yes/no markets
    but wrong for grouped event sub-markets, in which case Polymarket's site
    redirects to a search/landing page.
    """
    if canonical_url:
        return canonical_url
    if title:
        slug = slugify_title(title)
        if slug:
            return f"{POLYMARKET_BASE}/event/{slug}"
    if market_id:
        return f"{POLYMARKET_BASE}/markets/{market_id}"
    return POLYMARKET_BASE


def polygonscan_tx_url(tx_hash: str | None) -> str | None:
    if not tx_hash:
        return None
    return f"{POLYGONSCAN_TX}/{tx_hash.strip()}"


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value/1_000:.1f}k"
    return f"${value:,.2f}"


def fmt_int(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value/1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value/1_000:.1f}k"
    return f"{value:,}"


def fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def fmt_price_pct(value: float | None) -> str:
    if value is None:
        return "—"
    pct = value * 100.0
    if pct >= 1:
        return f"{pct:.0f}%"
    return f"{pct:.1f}%"


def fmt_price_cents(value: float | None) -> str:
    """Polymarket-style cents: 0.97 -> '97¢', 0.016 -> '1.6¢'."""
    if value is None:
        return "—"
    cents = value * 100.0
    if abs(cents - round(cents)) < 0.05:
        return f"{round(cents):.0f}¢"
    return f"{cents:.1f}¢"


def fmt_filter_usd(value: float | None) -> str:
    """Whole-dollar formatting for alert filter values (Min/Max Trade USD).

    User input is always whole numbers (presets are 100, 1000, …) so we
    skip the cents and use thousands-separators instead of `fmt_usd`'s
    abbreviated 'k/M/B' form.
    """
    if value is None:
        return "—"
    return f"${value:,.0f}"


_PRICE_DOT = {
    "yes": "🟢",
    "up": "🟢",
    "no": "🔴",
    "down": "🔴",
}

# Lower number = shown first. Yes/Up always lead, then No/Down, then anything else.
_OUTCOME_PRIORITY = {"yes": 0, "up": 0, "no": 1, "down": 1}


def _outcome_dot(label: str) -> str:
    return _PRICE_DOT.get((label or "").strip().lower(), "🔵")


def _sort_outcomes(prices: list) -> list:
    """Stable sort with Yes/Up first, then No/Down, then everything else."""
    return sorted(
        prices,
        key=lambda p: _OUTCOME_PRIORITY.get((getattr(p, "label", "") or "").strip().lower(), 2),
    )


def fmt_outcome_line(prices: list, max_outcomes: int = 4) -> str:
    """One-line outcome summary: '🟢 Yes: 97¢ | 🔴 No: 3¢'."""
    if not prices:
        return ""
    parts: list[str] = []
    for p in _sort_outcomes(prices)[:max_outcomes]:
        label = html.escape(getattr(p, "label", "") or "?")
        v = getattr(p, "price", None)
        parts.append(
            f"{_outcome_dot(getattr(p, 'label', ''))} {label}: <code>{fmt_price_cents(v)}</code>"
        )
    return " | ".join(parts)


def fmt_relative_time(iso: str | None) -> str:
    """Approximate '2m ago' / '3h ago' / '1d ago' from an ISO 8601 string."""
    if not iso:
        return "—"
    from datetime import datetime, timezone

    s = iso.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


# -- Parsers -----------------------------------------------------------------

_USD_RE = re.compile(r"""^\s*\$?\s*([0-9]+(?:[,\.][0-9]+)*)([kKmMbB]?)\s*$""")


def parse_usd(text: str) -> float | None:
    s = (text or "").strip().replace(",", "")
    if not s:
        return None
    m = _USD_RE.match(s)
    if not m:
        try:
            return float(s.lstrip("$"))
        except ValueError:
            return None
    base = float(m.group(1).replace(",", ""))
    suffix = m.group(2).lower()
    if suffix == "k":
        base *= 1_000
    elif suffix == "m":
        base *= 1_000_000
    elif suffix == "b":
        base *= 1_000_000_000
    return base


def parse_price(text: str) -> float | None:
    s = (text or "").strip().lower()
    if not s:
        return None
    if s.endswith("%"):
        try:
            v = float(s[:-1].strip())
        except ValueError:
            return None
        return v / 100.0
    if s.endswith("c"):
        try:
            v = float(s[:-1].strip())
        except ValueError:
            return None
        return v / 100.0
    try:
        v = float(s)
    except ValueError:
        return None
    if v > 1.0:
        v = v / 100.0
    return v


def parse_address(text: str) -> str | None:
    s = (text or "").strip().lower()
    if not s:
        return None
    if not re.fullmatch(r"0x[0-9a-f]{40}", s):
        return None
    return s


def parse_polymarket_url(text: str) -> str | None:
    """Extract the leaf market slug from a Polymarket URL.

    Accepts forms like:
        https://polymarket.com/event/<event-slug>/<market-slug>
        https://polymarket.com/event/<slug>
        https://polymarket.com/market/<slug>
        polymarket.com/event/<slug> (scheme optional)

    Returns the last path segment, which we then resolve via gamma-api.
    Returns None for non-Polymarket URLs and for plain text (so callers can
    fall through to keyword search).
    """
    s = (text or "").strip()
    if not s:
        return None
    from urllib.parse import urlparse

    candidate = s if "://" in s else f"https://{s}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    netloc = (parsed.netloc or "").lower()
    if not netloc:
        return None
    if not (netloc == "polymarket.com" or netloc.endswith(".polymarket.com")):
        return None
    segments = [seg for seg in (parsed.path or "").split("/") if seg]
    if not segments:
        return None
    return segments[-1]


# -- Keyboard factories ------------------------------------------------------

CB_MAIN = "main"
CB_TOP = "top"
CB_SEARCH = "search"
CB_ALERTS = "alerts"
CB_ADD = "add"
CB_HELP = "help"
CB_NOOP = "noop"  # used by the page-indicator chip; tap is silently ack'd
CB_BACK = "back"  # navigate to the user's last list view (or main menu if none)


def _pagination_row(
    callback_prefix: str,
    page: int,
    total_items: int,
    page_size: int,
) -> list[InlineKeyboardButton]:
    """Build a 'Show Previous · Page X/Y · Next' row.

    `callback_prefix` is the navigation callback head + any sticky args
    needed to re-render the same view at a new page (e.g. ``"top|volume"``
    so the resulting callbacks are ``top|volume|<page>``). The Page chip
    uses CB_NOOP and the bot's callback handler ack's it without action.

    Returns an empty list when there's only one page worth of data — no
    navigation row is needed and we don't want a stray Page 1/1 chip.
    """
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    if total_pages <= 1:
        return []
    page = max(0, min(page, total_pages - 1))
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(
            InlineKeyboardButton(
                "⬅ Previous",
                callback_data=f"{callback_prefix}|{page - 1}",
            )
        )
    row.append(
        InlineKeyboardButton(
            f"Page {page + 1}/{total_pages}",
            callback_data=CB_NOOP,
        )
    )
    if page < total_pages - 1:
        row.append(
            InlineKeyboardButton(
                "Next ➡",
                callback_data=f"{callback_prefix}|{page + 1}",
            )
        )
    return row


def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📈 Top Markets", callback_data=f"{CB_TOP}|volume|0"),
                InlineKeyboardButton("🔎 Search", callback_data=f"{CB_SEARCH}|"),
            ],
            [
                InlineKeyboardButton("🔔 My Alerts", callback_data=f"{CB_ALERTS}|0"),
                InlineKeyboardButton("➕ Add Alert", callback_data=f"{CB_ADD}|new"),
            ],
            [
                InlineKeyboardButton("❓ Help", callback_data=f"{CB_HELP}"),
            ],
        ]
    )


def kb_back_main() -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("⬅ Back", callback_data=f"{CB_MAIN}"),
        InlineKeyboardButton("🏠 Main Menu", callback_data=f"{CB_MAIN}"),
    ]


def kb_top_markets(rows: list[MarketRow], sort: str, page: int, page_size: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    sorts = [
        ("volume", "Volume"),
        ("traders", "Traders"),
        ("trades", "Trades"),
    ]
    sort_row = []
    for key, label in sorts:
        text = f"🔘 Sorted by {label}" if key == sort else f"Sort by {label}"
        sort_row.append(
            InlineKeyboardButton(text, callback_data=f"{CB_TOP}|{key}|0")
        )
    buttons.append(sort_row)

    start = page * page_size
    page_rows = rows[start : start + page_size]
    for idx, row in enumerate(page_rows, start=start + 1):
        title = row.title if len(row.title) <= 48 else row.title[:47] + "…"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{idx}. {title}",
                    callback_data=f"market|{row.market_id}",
                )
            ]
        )

    nav = _pagination_row(f"{CB_TOP}|{sort}", page, len(rows), page_size)
    if nav:
        buttons.append(nav)

    buttons.append(
        [
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data=f"{CB_TOP}|refresh|{sort}|{page}",
            )
        ]
    )
    buttons.append(kb_back_main())
    return InlineKeyboardMarkup(buttons)


def kb_market_detail(market_id: str, title: str) -> InlineKeyboardMarkup:
    """Market detail keyboard.

    The Back button uses CB_BACK so it returns to the list the user came
    from (Top / New / My Alerts) at the same sort & page — see the bot's
    `last_list` tracking. When there's no recorded parent it gracefully
    falls back to the main menu.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔔 Set Alert on this Market",
                    callback_data=f"{CB_ADD}|market|{market_id}",
                ),
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"market|refresh|{market_id}",
                ),
            ],
            [
                InlineKeyboardButton("⬅ Back", callback_data=CB_BACK),
                InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN),
            ],
        ]
    )


def kb_alerts_list(alerts: list[Alert], page: int, page_size: int) -> InlineKeyboardMarkup:
    """My Alerts list keyboard. Each numbered button opens that alert's
    detail card; a Refresh button at the bottom re-pulls fresh market stats
    for the rendered page."""
    buttons: list[list[InlineKeyboardButton]] = []
    start = page * page_size
    page_alerts = alerts[start : start + page_size]
    for idx, alert in enumerate(page_alerts, start=start + 1):
        status = "⏸" if alert.paused else "🔔"
        title = alert.market_title or alert.market_key or alert.summary()
        if len(title) > 42:
            title = title[:41] + "…"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{idx}. {status} {title}",
                    callback_data=f"alert|view|{alert.id}",
                )
            ]
        )
    nav = _pagination_row(CB_ALERTS, page, len(alerts), page_size)
    if nav:
        buttons.append(nav)
    buttons.append(
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"{CB_ALERTS}|refresh|{page}")]
    )
    buttons.append(
        [InlineKeyboardButton("➕ Add Alert", callback_data=f"{CB_ADD}|new")]
    )
    buttons.append(kb_back_main())
    return InlineKeyboardMarkup(buttons)


def kb_alert_detail(alert: Alert) -> InlineKeyboardMarkup:
    pause_label = "▶ Resume" if alert.paused else "⏸ Pause"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(pause_label, callback_data=f"alert|toggle|{alert.id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"alert|delete|{alert.id}"),
            ],
            [
                InlineKeyboardButton(
                    "🔄 Refresh", callback_data=f"alert|refresh|{alert.id}"
                ),
            ],
            [InlineKeyboardButton("⬅ Back to Alerts", callback_data=f"{CB_ALERTS}|0")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data=f"{CB_MAIN}")],
        ]
    )


# -- Add Alert review-screen keyboards ---------------------------------------

CB_ADD_EDIT = "add|edit"
CB_ADD_PICK = "add|pick"
CB_ADD_SAVE = "add|save"
CB_ADD_CANCEL = "add|cancel"


def _review_label(name: str, value: str | None) -> str:
    """Button copy for the Add-Alert review screen: 'Set <name>' when the
    field is unset, '<name>: <value>' once the user picks something."""
    if value is None or value == "":
        return f"Set {name}"
    return f"{name}: {value}"


def kb_add_review(draft: dict[str, object]) -> InlineKeyboardMarkup:
    """Render the Add Alert review keyboard.

    Behaviour notes:
    - When `draft['market_locked']` is True (the user opened the wizard from
      a market detail's 'Set Alert on this Market' button), the Market row
      is hidden — the market is already implied.
    - The Outcome row only appears once a market is set; outcome labels are
      market-specific so the filter has no meaning otherwise.
    """
    def usd_value(key: str) -> str | None:
        v = draft.get(key)
        if v is None:
            return None
        try:
            return fmt_filter_usd(float(v))
        except (TypeError, ValueError):
            return str(v)

    def price_value(key: str) -> str | None:
        v = draft.get(key)
        if v is None:
            return None
        try:
            return fmt_price_cents(float(v))
        except (TypeError, ValueError):
            return str(v)

    market_value = draft.get("market_title") or draft.get("market_key") or None
    if isinstance(market_value, str) and len(market_value) > 30:
        market_value = market_value[:29] + "…"

    trader_raw = draft.get("trader")
    trader_value = (
        f"{str(trader_raw)[:6]}…{str(trader_raw)[-4:]}"
        if isinstance(trader_raw, str) and len(trader_raw) > 12
        else (trader_raw or None)
    )

    market_locked = bool(draft.get("market_locked"))
    market_set = bool(draft.get("market_key"))
    outcome_value = draft.get("outcome")
    if isinstance(outcome_value, str) and len(outcome_value) > 26:
        outcome_value = outcome_value[:25] + "…"

    buttons: list[list[InlineKeyboardButton]] = []
    if not market_locked:
        buttons.append([InlineKeyboardButton(
            _review_label("Market", market_value if isinstance(market_value, str) else None),
            callback_data=f"{CB_ADD_EDIT}|market",
        )])
    if market_set:
        buttons.append([InlineKeyboardButton(
            _review_label("Outcome", outcome_value if isinstance(outcome_value, str) else None),
            callback_data=f"{CB_ADD_EDIT}|outcome",
        )])
    buttons.extend([
        [InlineKeyboardButton(
            _review_label("Min Trade USD", usd_value("min_trade_amount_usd")),
            callback_data=f"{CB_ADD_EDIT}|min_trade",
        )],
        [InlineKeyboardButton(
            _review_label("Max Trade USD", usd_value("max_trade_amount_usd")),
            callback_data=f"{CB_ADD_EDIT}|max_trade",
        )],
        [InlineKeyboardButton(
            _review_label("Min Price", price_value("min_price_usd")),
            callback_data=f"{CB_ADD_EDIT}|min_price",
        )],
        [InlineKeyboardButton(
            _review_label("Max Price", price_value("max_price_usd")),
            callback_data=f"{CB_ADD_EDIT}|max_price",
        )],
        [InlineKeyboardButton(
            _review_label("Trader", trader_value if isinstance(trader_value, str) else None),
            callback_data=f"{CB_ADD_EDIT}|trader",
        )],
        [
            InlineKeyboardButton("✅ Save Alert", callback_data=CB_ADD_SAVE),
            InlineKeyboardButton("❌ Cancel", callback_data=CB_ADD_CANCEL),
        ],
    ])
    return InlineKeyboardMarkup(buttons)


def kb_chips_usd(field: str) -> InlineKeyboardMarkup:
    presets = [100, 1_000, 10_000, 100_000]
    rows: list[list[InlineKeyboardButton]] = []
    chunk: list[InlineKeyboardButton] = []
    for v in presets:
        chunk.append(
            InlineKeyboardButton(
                str(v),
                callback_data=f"{CB_ADD_PICK}|{field}|{v}",
            )
        )
        if len(chunk) == 2:
            rows.append(chunk)
            chunk = []
    if chunk:
        rows.append(chunk)
    rows.append(
        [
            InlineKeyboardButton("✏ Custom…", callback_data=f"{CB_ADD_PICK}|{field}|custom"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"{CB_ADD_PICK}|{field}|skip"),
        ]
    )
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"{CB_ADD}|review")])
    return InlineKeyboardMarkup(rows)


def kb_chips_price(field: str) -> InlineKeyboardMarkup:
    presets = [0.10, 0.25, 0.50, 0.75, 0.90]
    rows: list[list[InlineKeyboardButton]] = []
    chunk: list[InlineKeyboardButton] = []
    for v in presets:
        chunk.append(
            InlineKeyboardButton(
                fmt_price_cents(v),
                callback_data=f"{CB_ADD_PICK}|{field}|{v}",
            )
        )
        if len(chunk) == 3:
            rows.append(chunk)
            chunk = []
    if chunk:
        rows.append(chunk)
    rows.append(
        [
            InlineKeyboardButton("✏ Custom…", callback_data=f"{CB_ADD_PICK}|{field}|custom"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"{CB_ADD_PICK}|{field}|skip"),
        ]
    )
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"{CB_ADD}|review")])
    return InlineKeyboardMarkup(rows)


def kb_chips_outcome(prices: list) -> InlineKeyboardMarkup:
    """Outcome picker for the Add-Alert wizard.

    Each chip's callback carries the outcome's index in the sorted list (so
    we never bump into Telegram's 64-byte callback_data limit on long
    outcome labels). The bot resolves the index back to the canonical label
    using the prices snapshot it stashed on the draft.
    """
    sorted_prices = _sort_outcomes(prices)
    rows: list[list[InlineKeyboardButton]] = []
    chunk: list[InlineKeyboardButton] = []
    for idx, p in enumerate(sorted_prices[:6]):
        label = (getattr(p, "label", None) or "?").strip()
        if len(label) > 22:
            label_short = label[:21] + "…"
        else:
            label_short = label
        chunk.append(
            InlineKeyboardButton(
                f"{_outcome_dot(label)} {label_short}",
                callback_data=f"{CB_ADD_PICK}|outcome|{idx}",
            )
        )
        if len(chunk) == 2:
            rows.append(chunk)
            chunk = []
    if chunk:
        rows.append(chunk)
    rows.append(
        [InlineKeyboardButton("⏭ Skip (any outcome)", callback_data=f"{CB_ADD_PICK}|outcome|skip")]
    )
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"{CB_ADD}|review")])
    return InlineKeyboardMarkup(rows)


def kb_chips_trader() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏ Enter Wallet…", callback_data=f"{CB_ADD_PICK}|trader|custom"),
                InlineKeyboardButton("⏭ Skip", callback_data=f"{CB_ADD_PICK}|trader|skip"),
            ],
            [InlineKeyboardButton("⬅ Back", callback_data=f"{CB_ADD}|review")],
        ]
    )


def kb_chips_market(top_rows: list[MarketRow]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for r in top_rows[:8]:
        title = r.title if len(r.title) <= 40 else r.title[:39] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    title,
                    callback_data=f"{CB_ADD_PICK}|market|{r.market_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("🔎 Search…", callback_data=f"{CB_ADD_PICK}|market|search"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"{CB_ADD_PICK}|market|skip"),
        ]
    )
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"{CB_ADD}|review")])
    return InlineKeyboardMarkup(rows)


# -- Message bodies ----------------------------------------------------------

def welcome_text(stats: dict | None = None) -> str:
    s = stats or {}
    active = int(s.get("active_alerts", 0))
    paused = int(s.get("paused_alerts", 0))
    markets = int(s.get("markets_tracked", 0))
    triggered = int(s.get("triggered_alerts", 0))
    paused_note = f" <i>({paused} paused)</i>" if paused else ""

    return (
        "<b>🎯 Welcome to PolyBit</b>\n"
        "<i>Your edge in Polymarket prediction markets.</i>\n\n"
        f"🔔 <b>Active Alerts:</b> {active}{paused_note}\n"
        f"📊 <b>Markets You Watch:</b> {markets}\n"
        f"✅ <b>Triggered Alerts:</b> {triggered}\n\n"
        "<b>📚 What can PolyBit do?</b>\n"
        "📈 Browse top markets by volume, traders, or trade count\n"
        "🔎 Search any market by keyword\n"
        "🔔 Custom alerts on trade size, price ranges, or specific traders\n\n"
        "Pick an option below to get started.\n\n"
        f"{FOOTER}"
    )


def help_text() -> str:
    w = _window_label()
    return (
        "<b>PolyBit Commands</b>\n"
        "/start — open the main menu\n"
        f"/topmarkets — top markets last {w}\n"
        "/search — search markets by keyword\n"
        "/myalerts — your active alerts\n"
        "/addalert — create a new alert\n"
        "/stop — stop receiving alerts\n"
        "/help — this message\n\n"
        "<b>Alerts</b>\n"
        "Each alert combines up to six optional filters: market, "
        "min/max trade USD, min/max share price, and trader wallet. "
        "Skip any filter you don't care about — only set filters are checked.\n\n"
        f"{FOOTER}"
    )


def _row_stats_line(row: "MarketRow") -> str:
    parts: list[str] = []
    if row.volume_usd is not None and row.volume_usd > 0:
        parts.append(f"Volume: <code>{fmt_usd(row.volume_usd)}</code>")
    if row.unique_buyers is not None and row.unique_buyers > 0:
        parts.append(f"Traders: <code>{fmt_int(row.unique_buyers)}</code>")
    if row.trade_count is not None and row.trade_count > 0:
        parts.append(f"Trades: <code>{fmt_int(row.trade_count)}</code>")
    return " · ".join(parts)


def _row_block(idx: int, row: "MarketRow") -> list[str]:
    """Render one market row: number, title, prices, stats, Polymarket link."""
    title_h = html.escape(row.title)
    block = [f"<b>{idx}.</b> {title_h}"]
    outcome_line = fmt_outcome_line(row.prices)
    if outcome_line:
        block.append(f"   {outcome_line}")
    elif row.outcomes:
        block.append(f"   <i>Outcomes: {html.escape(' / '.join(row.outcomes))}</i>")
    stats_line = _row_stats_line(row)
    if stats_line:
        block.append(f"   {stats_line}")
    url = polymarket_event_url(row.market_id, row.title, getattr(row, "canonical_url", None))
    block.append(f'   🌐 <a href="{html.escape(url, quote=True)}">View on Polymarket</a>')
    return block


def fmt_market_list_intro(
    title: str,
    rows: list[MarketRow],
    page: int,
    page_size: int,
    *,
    sort: str | None = None,  # kept for back-compat, no longer rendered inline
) -> str:
    lines = [f"<b>{html.escape(title)}</b>"]
    if not rows:
        lines.append("\nNo markets found.")
        lines.append(f"\n{FOOTER}")
        return "\n".join(lines)
    start = page * page_size
    page_rows = rows[start : start + page_size]
    for idx, row in enumerate(page_rows, start=start + 1):
        lines.append("")
        lines.extend(_row_block(idx, row))
    lines.append(f"\n{FOOTER}")
    return "\n".join(lines)


def fmt_market_detail(
    title: str | None,
    image: str | None,
    market_id: str,
    prices: list[OutcomePrice],
    stats: MarketRow | None = None,
    last_updated: str | None = None,
    *,
    canonical_url: str | None = None,
) -> str:
    safe_title = html.escape(title or "(unknown market)")
    lines = [f"📌 <b>{safe_title}</b>"]
    lines.append("")

    if prices:
        lines.append("💵 <b>Current Prices:</b>")
        for p in _sort_outcomes(prices):
            label_text = html.escape(p.label or "?")
            dot = _outcome_dot(p.label)
            cents = fmt_price_cents(p.price)
            lines.append(f"  {dot} <b>{label_text}:</b> <code>{cents}</code>")
    else:
        lines.append("💵 <b>Current Prices:</b> no trades yet")

    if stats and (stats.volume_usd is not None or stats.unique_buyers is not None or stats.trade_count is not None):
        lines.append("")
        lines.append(f"📊 <b>{_window_label()} Stats:</b>")
        if stats.volume_usd is not None:
            lines.append(f"  Volume: <code>{fmt_usd(stats.volume_usd)}</code>")
        if stats.unique_buyers is not None:
            lines.append(f"  Unique Traders: <code>{fmt_int(stats.unique_buyers)}</code>")
        if stats.trade_count is not None:
            lines.append(f"  Trades: <code>{fmt_int(stats.trade_count)}</code>")

    url = polymarket_event_url(market_id, title, canonical_url)
    lines.append("")
    lines.append(f'🌐 <a href="{html.escape(url, quote=True)}">View on Polymarket</a>')

    lines.append(f"\n{FOOTER}")
    return "\n".join(lines)


def _alert_card_block(
    idx: int, alert: Alert, market_data: dict | None = None
) -> list[str]:
    """Render one alert summary block for the paginated /myalerts message.

    When ``market_data`` is provided (bot.py enriches bound alerts via
    ``_alert_market_data``), we also append, in this order:
      • a 💵 Current Prices line (Yes/Up first, sorted)
      • a 📊 1h Stats line (Volume · Traders · Trades; zeros when quiet)
      • a 🌐 View on Polymarket inline link

    Same vertical order as market list rows so the bot reads consistently.
    """
    state = "⏸" if alert.paused else "🔔"
    title_h = html.escape(alert.market_title or alert.market_key or "Any market")
    block = [f"<b>{idx}. {state} {title_h}</b>"]
    if alert.outcome:
        block.append(f"   Outcome: <code>{html.escape(alert.outcome)}</code>")
    if alert.min_trade_amount_usd is not None or alert.max_trade_amount_usd is not None:
        lo, hi = alert.min_trade_amount_usd, alert.max_trade_amount_usd
        if lo is not None and hi is not None:
            block.append(
                f"   Trade: <code>{fmt_filter_usd(lo)}</code>–<code>{fmt_filter_usd(hi)}</code>"
            )
        elif lo is not None:
            block.append(f"   Trade: ≥ <code>{fmt_filter_usd(lo)}</code>")
        else:
            block.append(f"   Trade: ≤ <code>{fmt_filter_usd(hi)}</code>")
    if alert.min_price_usd is not None or alert.max_price_usd is not None:
        lo, hi = alert.min_price_usd, alert.max_price_usd
        if lo is not None and hi is not None:
            block.append(
                f"   Price: <code>{fmt_price_cents(lo)}</code>–<code>{fmt_price_cents(hi)}</code>"
            )
        elif lo is not None:
            block.append(f"   Price: ≥ <code>{fmt_price_cents(lo)}</code>")
        else:
            block.append(f"   Price: ≤ <code>{fmt_price_cents(hi)}</code>")
    if alert.trader:
        block.append(f"   Trader: {_addr_link(alert.trader)}")

    if alert.market_key:
        prices = (market_data or {}).get("prices") or []
        outcome_line = fmt_outcome_line(prices)
        if outcome_line:
            block.append(f"   💵 {outcome_line}")

        stats = (market_data or {}).get("stats") or {}
        vol = stats.get("volume_usd") or 0.0
        traders = stats.get("unique_buyers") or 0
        trades = stats.get("trade_count") or 0
        block.append(
            f"   📊 {_window_label()} Stats: "
            f"Volume: <code>{fmt_usd(vol)}</code> · "
            f"Traders: <code>{fmt_int(traders)}</code> · "
            f"Trades: <code>{fmt_int(trades)}</code>"
        )

        title_for_link = (market_data or {}).get("title") or alert.market_title
        canonical = (market_data or {}).get("canonical_url")
        url = polymarket_event_url(alert.market_key, title_for_link, canonical)
        block.append(
            f'   🌐 <a href="{html.escape(url, quote=True)}">View on Polymarket</a>'
        )
    return block


def fmt_alerts_list(
    alerts: list[Alert],
    page: int = 0,
    page_size: int = 5,
    market_data_by_key: dict[str, dict] | None = None,
) -> str:
    if not alerts:
        return (
            "<b>🔔 My Alerts</b>\n\n"
            "You have no active alerts.\n"
            "Tap <b>Add Alert</b> to create one.\n\n"
            f"{FOOTER}"
        )
    total_pages = max(1, (len(alerts) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    page_alerts = alerts[start : start + page_size]

    lines = [f"<b>🔔 My Alerts</b> ({len(alerts)})"]
    lines.append("<i>Tap an alert to manage it.</i>")
    for offset, alert in enumerate(page_alerts):
        lines.append("")
        md = None
        if market_data_by_key and alert.market_key:
            md = market_data_by_key.get(alert.market_key)
        lines.extend(_alert_card_block(start + offset + 1, alert, market_data=md))
    lines.append(f"\n{FOOTER}")
    return "\n".join(lines)


def fmt_alert_detail(alert: Alert, market_data: dict | None = None) -> str:
    """Render the alert detail card.

    Sections (each separated by a blank line):
      1. Header line with active/paused state.
      2. Market title + inline 'View on Polymarket' link.
      3. Filters block — only set fields are listed (no 'Skip' rows).
      4. Trader row + inline profile link (when set).
      5. Current Prices + 1h Stats (when the market has data).
      6. Created-at footer.

    Numeric values are wrapped in <code>...</code> so the Telegram client
    renders them in monospace and lets the user tap-to-copy.
    """
    state = "⏸ Paused Alert" if alert.paused else "🔔 Active Alert"
    lines = [f"<b>{state}</b>"]

    market_title = alert.market_title or alert.market_key
    if alert.market_key and market_data:
        market_title = market_data.get("title") or market_title
    if market_title:
        title_h = html.escape(str(market_title))
        lines.append("")
        lines.append(f"📌 <b>Market:</b> {title_h}")
        if alert.market_key:
            canonical = market_data.get("canonical_url") if market_data else None
            url = polymarket_event_url(alert.market_key, market_title, canonical)
            lines.append(
                f'🌐 <a href="{html.escape(url, quote=True)}">View on Polymarket</a>'
            )
    else:
        lines.append("")
        lines.append("📌 <b>Market:</b> any")

    filter_lines: list[str] = []
    if alert.outcome:
        filter_lines.append(
            f"  • Outcome: <code>{html.escape(alert.outcome)}</code>"
        )
    if alert.min_trade_amount_usd is not None:
        filter_lines.append(
            f"  • Min Trade USD: <code>{fmt_filter_usd(alert.min_trade_amount_usd)}</code>"
        )
    if alert.max_trade_amount_usd is not None:
        filter_lines.append(
            f"  • Max Trade USD: <code>{fmt_filter_usd(alert.max_trade_amount_usd)}</code>"
        )
    if alert.min_price_usd is not None:
        filter_lines.append(
            f"  • Min Price: <code>{fmt_price_cents(alert.min_price_usd)}</code>"
        )
    if alert.max_price_usd is not None:
        filter_lines.append(
            f"  • Max Price: <code>{fmt_price_cents(alert.max_price_usd)}</code>"
        )
    if filter_lines:
        lines.append("")
        lines.append("🎯 <b>Filters:</b>")
        lines.extend(filter_lines)

    if alert.trader:
        profile = polymarket_profile_url(alert.trader)
        lines.append("")
        lines.append(f"<b>Trader:</b> {_addr_link(alert.trader)}")
        if profile:
            lines.append(
                f'👤 <a href="{html.escape(profile, quote=True)}">View Trader profile on Polymarket</a>'
            )

    if alert.market_key and market_data:
        prices = market_data.get("prices") or []
        stats = market_data.get("stats") or {}
        if prices:
            lines.append("")
            lines.append("💵 <b>Current Prices:</b>")
            for p in _sort_outcomes(prices):
                label_text = html.escape(getattr(p, "label", None) or "?")
                dot = _outcome_dot(getattr(p, "label", "") or "")
                lines.append(
                    f"  {dot} <b>{label_text}:</b> "
                    f"<code>{fmt_price_cents(getattr(p, 'price', None))}</code>"
                )
        # Always show the recent-activity line for bound alerts so quiet
        # markets read as "0" instead of disappearing — gives users a clear
        # signal there's been no action vs. data simply being unavailable.
        vol = stats.get("volume_usd") or 0.0
        traders = stats.get("unique_buyers") or 0
        trades = stats.get("trade_count") or 0
        lines.append("")
        lines.append(
            f"📊 <b>{_window_label()} Stats:</b> "
            f"Volume: <code>{fmt_usd(vol)}</code> · "
            f"Traders: <code>{fmt_int(traders)}</code> · "
            f"Trades: <code>{fmt_int(trades)}</code>"
        )

    lines.append("")
    lines.append(f"<i>Created {html.escape(alert.created_at)}</i>")
    lines.append(f"\n{FOOTER}")
    return "\n".join(lines)


def fmt_add_review(draft: dict[str, object]) -> str:
    title = draft.get("market_title") or draft.get("market_key")
    headline = (
        "<b>➕ New Alert</b>\n"
        "Set at least one filter so we know what to notify you about. "
        "Tap any row to edit it."
    )
    if title:
        headline += f"\n\n<b>Market:</b> {html.escape(str(title))}"
    headline += f"\n\n{FOOTER}"
    return headline


def fmt_chips_prompt(field_name: str) -> str:
    return f"<b>Pick a value for {field_name}</b>\nOr tap Skip to leave it unset.\n\n{FOOTER}"


def fmt_custom_prompt(field_name: str, hint: str) -> str:
    return (
        f"<b>Enter a value for {field_name}</b>\n"
        f"<i>{hint}</i>\n"
        "Send /cancel to abort.\n\n"
        f"{FOOTER}"
    )


def fmt_alert_saved(alert: Alert) -> str:
    return (
        "<b>✅ Alert created</b>\n"
        f"{html.escape(alert.summary())}\n\n"
        f"{FOOTER}"
    )


def fmt_alert_deleted() -> str:
    return f"<b>🗑 Alert deleted</b>\n\n{FOOTER}"


# -- Trade / alert notifications ---------------------------------------------

def fmt_trade_notification(
    match: Match,
    *,
    canonical_url: str | None = None,
) -> tuple[str, InlineKeyboardMarkup, str | None]:
    """Returns (html_text, keyboard, preview_url_or_none).

    The third tuple element is the URL that Telegram should fetch a link
    preview for — a DEXrabbit market page when we have a conditionId, else
    None. DEXrabbit's OG image is a rich market summary (title + outcome
    prices + 24h volume), so the preview header reads like a built-in
    market card.

    Inline-link design (matches the rest of the bot): View on Polymarket /
    PolygonScan / Trader profile are rendered as `<a>` links inside the
    caption, not as URL buttons. The only inline button left is the action
    button "⏸ Mute this Alert".

    `event.buyer` and `event.seller` are pre-normalized in `TradeEvent`
    (see its docstring): `buyer` is whoever received outcome tokens.
    """
    e = match.event
    a = match.alert

    title_h = html.escape(e.market_title)
    direction = ""
    if e.is_outcome_buy is True:
        direction = " · <b>BUY</b>"
    elif e.is_outcome_buy is False:
        direction = " · <b>SELL</b>"

    lines = [f"<b>🔔 {title_h}</b>"]
    if match.reasons:
        lines.append(
            f"<i>Matched: {html.escape(' · '.join(match.reasons))}</i>"
        )
    lines.append("")

    if e.outcome_label:
        outcome_h = html.escape(e.outcome_label)
        lines.append(
            f"<b>Outcome:</b> {outcome_h} @ <code>{fmt_price_cents(e.price)}</code>"
        )
    if e.collateral_usd is not None:
        lines.append(
            f"<b>Trade size:</b> <code>{fmt_usd(e.collateral_usd)}</code>{direction}"
        )

    if e.buyer:
        lines.append(f"<b>Outcome Buyer:</b> {_addr_link(e.buyer)}")
    if e.seller:
        lines.append(f"<b>Outcome Seller:</b> {_addr_link(e.seller)}")

    market_url = polymarket_event_url(e.market_id, e.market_title, canonical_url)
    lines.append("")
    lines.append(
        f'🌐 <a href="{html.escape(market_url, quote=True)}">View on Polymarket</a>'
    )
    pgs = polygonscan_tx_url(e.tx_hash)
    if pgs:
        lines.append(
            f'🔗 <a href="{html.escape(pgs, quote=True)}">View Trade on PolygonScan</a>'
        )
    trader_url = polymarket_profile_url(a.trader) if a.trader else None
    if trader_url:
        lines.append(
            f'👤 <a href="{html.escape(trader_url, quote=True)}">View Trader profile on Polymarket</a>'
        )

    lines.append(f"\n{FOOTER}")
    text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏸ Mute this Alert", callback_data=f"alert|toggle|{a.id}")]]
    )
    # Use the Polymarket-hosted question image as the preview source.
    # Telegram renders raw image URLs as the link preview directly, which
    # is fast and reliable (S3-hosted, ~50KB).
    preview = e.market_image
    return text, kb, preview
