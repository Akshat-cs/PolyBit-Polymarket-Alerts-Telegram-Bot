"""Unified per-trade alert matcher.

A trade matches an alert if EVERY set filter passes. Skipped (None) filters
always pass. Each match is gated by a per-alert cooldown to avoid spamming
users when a hot market produces many qualifying trades back-to-back.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .bitquery import TradeEvent
from .config import ALERT_COOLDOWN_SECONDS
from .store import Alert, _cents


@dataclass
class Match:
    alert: Alert
    event: TradeEvent
    reasons: list[str]


def _trader_matches(trader_filter: str, event: TradeEvent) -> bool:
    addr = trader_filter.strip().lower()
    if not addr:
        return True
    buyer = (event.buyer or "").lower()
    seller = (event.seller or "").lower()
    return addr == buyer or addr == seller


def _matches(alert: Alert, event: TradeEvent) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    if alert.market_key is not None:
        if event.market_id != alert.market_key:
            return False, []
        reasons.append(f"market: {alert.market_title or alert.market_key}")

    if alert.outcome is not None:
        expected = alert.outcome.strip().lower()
        actual = (event.outcome_label or "").strip().lower()
        if not expected or actual != expected:
            return False, []
        reasons.append(f"outcome: {alert.outcome}")

    cusd = event.collateral_usd
    if alert.min_trade_amount_usd is not None:
        if cusd is None or cusd < alert.min_trade_amount_usd:
            return False, []
        reasons.append(f"trade ≥ ${alert.min_trade_amount_usd:,.0f}")
    if alert.max_trade_amount_usd is not None:
        if cusd is None or cusd > alert.max_trade_amount_usd:
            return False, []
        reasons.append(f"trade ≤ ${alert.max_trade_amount_usd:,.0f}")

    price = event.price
    if alert.min_price_usd is not None:
        if price is None or price < alert.min_price_usd:
            return False, []
        reasons.append(f"price ≥ {_cents(alert.min_price_usd)}")
    if alert.max_price_usd is not None:
        if price is None or price > alert.max_price_usd:
            return False, []
        reasons.append(f"price ≤ {_cents(alert.max_price_usd)}")

    if alert.trader is not None:
        if not _trader_matches(alert.trader, event):
            return False, []
        reasons.append(f"trader {alert.trader[:10]}…")

    return True, reasons


def match_trade(
    event: TradeEvent,
    alerts: list[Alert],
    *,
    now: float | None = None,
    cooldown_seconds: float = ALERT_COOLDOWN_SECONDS,
) -> list[Match]:
    """Run a trade through every active alert, honoring cooldowns.

    Mutates each matched alert's `last_triggered_at` so callers can persist
    the change later.
    """
    ts = now if now is not None else time.time()
    matches: list[Match] = []
    for alert in alerts:
        if not alert.active or alert.paused:
            continue
        if not alert.has_any_filter():
            # Defensive: should be rejected at save time.
            continue
        if (
            alert.last_triggered_at is not None
            and ts - alert.last_triggered_at < cooldown_seconds
        ):
            continue
        ok, reasons = _matches(alert, event)
        if not ok:
            continue
        alert.last_triggered_at = ts
        matches.append(Match(alert=alert, event=event, reasons=reasons))
    return matches
