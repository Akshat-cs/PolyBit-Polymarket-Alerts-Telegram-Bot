"""Print a snapshot of users and alerts from the JSON stores.

Usage:
    # On Render (Shell tab):
    python -m polybit.stats

    # Locally:
    python -m polybit.stats

Reads from `POLYBIT_DATA_DIR` (defaults to `<project>/data`). Read-only —
safe to run while the bot is live.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config


def _load(path: Path, key: str) -> list[dict]:
    if not path.exists():
        print(f"  ! {path} does not exist yet", file=sys.stderr)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  ! could not read {path}: {e}", file=sys.stderr)
        return []
    raw = data.get(key) if isinstance(data, dict) else None
    return raw if isinstance(raw, list) else []


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ago(iso: str | None, now: datetime) -> str:
    dt = _parse_iso(iso)
    if dt is None:
        return "—"
    delta = now - dt
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86_400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86_400}d ago"


def _alert_summary(a: dict) -> str:
    parts: list[str] = []
    if a.get("outcome"):
        parts.append(f"outcome={a['outcome']}")
    lo, hi = a.get("min_trade_amount_usd"), a.get("max_trade_amount_usd")
    if lo is not None or hi is not None:
        if lo is not None and hi is not None:
            parts.append(f"trade ${lo:,.0f}-${hi:,.0f}")
        elif lo is not None:
            parts.append(f"trade≥${lo:,.0f}")
        else:
            parts.append(f"trade≤${hi:,.0f}")
    plo, phi = a.get("min_price_usd"), a.get("max_price_usd")
    if plo is not None or phi is not None:
        if plo is not None and phi is not None:
            parts.append(f"price {plo*100:.0f}¢-{phi*100:.0f}¢")
        elif plo is not None:
            parts.append(f"price≥{plo*100:.0f}¢")
        else:
            parts.append(f"price≤{phi*100:.0f}¢")
    if a.get("trader"):
        parts.append(f"trader {a['trader'][:8]}…")
    return " · ".join(parts) or "(no filters)"


def main() -> int:
    data_dir = config.DATA_DIR
    now = datetime.now(timezone.utc)

    users = _load(data_dir / "users.json", "users")
    alerts = _load(data_dir / "alerts.json", "alerts")

    user_by_chat = {str(u.get("chat_id") or ""): u for u in users}

    print("=" * 60)
    print("  PolyBit · stats snapshot")
    print(f"  Data dir: {data_dir}")
    print(f"  Time:     {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    # --- Users -----------------------------------------------------------
    joined_24h = sum(
        1 for u in users
        if (dt := _parse_iso(u.get("joined_at"))) and (now - dt) <= timedelta(hours=24)
    )
    joined_7d = sum(
        1 for u in users
        if (dt := _parse_iso(u.get("joined_at"))) and (now - dt) <= timedelta(days=7)
    )
    print(f"\n👥 Users: {len(users)}")
    print(f"   Joined last 24h: {joined_24h}")
    print(f"   Joined last 7d:  {joined_7d}")

    # --- Alerts ----------------------------------------------------------
    active = sum(1 for a in alerts if a.get("active", True) and not a.get("paused"))
    paused = sum(1 for a in alerts if a.get("paused"))
    bound = sum(1 for a in alerts if a.get("market_key"))
    with_trader = sum(1 for a in alerts if a.get("trader"))
    triggered = sum(1 for a in alerts if a.get("last_triggered_at"))
    created_24h = sum(
        1 for a in alerts
        if (dt := _parse_iso(a.get("created_at"))) and (now - dt) <= timedelta(hours=24)
    )
    print(f"\n🔔 Alerts: {len(alerts)}")
    print(f"   Active:                  {active}")
    print(f"   Paused:                  {paused}")
    print(f"   Bound to a market:       {bound}")
    print(f"   With trader filter:      {with_trader}")
    print(f"   Triggered at least once: {triggered}")
    print(f"   Created last 24h:        {created_24h}")

    # --- Per-user alert counts ------------------------------------------
    by_chat: Counter[str] = Counter(str(a.get("chat_id") or "") for a in alerts)
    if by_chat:
        print("\n📊 Alerts per user (top 10):")
        for cid, count in by_chat.most_common(10):
            uname = (user_by_chat.get(cid, {}).get("username") or "?")
            print(f"   chat_id={cid:<14} @{uname:<20} {count} alerts")

    # --- Most-targeted markets ------------------------------------------
    by_market: Counter[str] = Counter(
        a.get("market_title") or a.get("market_key") or ""
        for a in alerts
        if a.get("market_key")
    )
    if by_market:
        print("\n🎯 Most-targeted markets (top 5):")
        for title, count in by_market.most_common(5):
            t = title if len(title) <= 60 else title[:57] + "…"
            print(f"   {count:>3}× {t}")

    # --- Recent alerts ---------------------------------------------------
    if alerts:
        recent = sorted(alerts, key=lambda a: a.get("created_at", ""), reverse=True)[:10]
        print("\n🕒 Recent alerts (last 10):")
        for a in recent:
            cid = str(a.get("chat_id") or "")
            uname = user_by_chat.get(cid, {}).get("username") or "?"
            title = a.get("market_title") or "any market"
            t = title if len(title) <= 50 else title[:47] + "…"
            tag = " [paused]" if a.get("paused") else ""
            print(f"   {_ago(a.get('created_at'), now):<10} @{uname:<18} → {t}{tag}")
            print(f"              {_alert_summary(a)}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
