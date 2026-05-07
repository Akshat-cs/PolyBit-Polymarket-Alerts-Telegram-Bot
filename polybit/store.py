"""JSON-backed stores for users and alerts.

Both files are loaded into memory on startup; mutations rewrite the file
atomically (tmp + os.replace) under an asyncio.Lock so a crash mid-write
cannot corrupt state. At ~100 users with a few alerts each, total state is
well under 100KB and write latency is sub-millisecond.

Schema:
    users.json  -> { "users": [ { "chat_id": str, "username": str|None,
                                  "joined_at": iso8601 } ] }
    alerts.json -> { "alerts": [ { "id": uuid, "chat_id": str,
                                   "market_key": str|None,
                                   "market_title": str|None,
                                   "min_trade_amount_usd": float|None,
                                   "max_trade_amount_usd": float|None,
                                   "min_price_usd": float|None,
                                   "max_price_usd": float|None,
                                   "trader": str|None,
                                   "last_triggered_at": float|None,
                                   "created_at": iso8601,
                                   "active": bool, "paused": bool } ] }

A `null` filter field means "skip" - that filter always passes in the matcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cents(value: float | None) -> str:
    if value is None:
        return "—"
    cents = value * 100.0
    if abs(cents - round(cents)) < 0.05:
        return f"{round(cents):.0f}¢"
    return f"{cents:.1f}¢"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class User:
    chat_id: str
    username: str | None = None
    joined_at: str = field(default_factory=_now_iso)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "User":
        return cls(
            chat_id=str(data.get("chat_id", "")).strip(),
            username=(data.get("username") or None),
            joined_at=str(data.get("joined_at") or _now_iso()),
        )


@dataclass
class Alert:
    """Unified alert with seven optional filters; null = skip (always passes).

    `outcome` only makes sense alongside `market_key` since outcome labels
    (e.g. 'Yes', 'Up', team names) vary per market - the bot's add-alert UI
    enforces this and clears `outcome` whenever the user changes market.
    """

    id: str
    chat_id: str
    market_key: str | None = None
    market_title: str | None = None
    outcome: str | None = None
    min_trade_amount_usd: float | None = None
    max_trade_amount_usd: float | None = None
    min_price_usd: float | None = None
    max_price_usd: float | None = None
    trader: str | None = None
    last_triggered_at: float | None = None
    created_at: str = field(default_factory=_now_iso)
    active: bool = True
    paused: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Alert":
        def _flt(key: str) -> float | None:
            v = data.get(key)
            return None if v is None else float(v)

        def _str(key: str) -> str | None:
            v = data.get(key)
            if v is None:
                return None
            s = str(v).strip()
            return s or None

        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            chat_id=str(data.get("chat_id", "")).strip(),
            market_key=_str("market_key"),
            market_title=_str("market_title"),
            outcome=_str("outcome"),
            min_trade_amount_usd=_flt("min_trade_amount_usd"),
            max_trade_amount_usd=_flt("max_trade_amount_usd"),
            min_price_usd=_flt("min_price_usd"),
            max_price_usd=_flt("max_price_usd"),
            trader=(_str("trader") or "").lower() or None,
            last_triggered_at=_flt("last_triggered_at"),
            created_at=str(data.get("created_at") or _now_iso()),
            active=bool(data.get("active", True)),
            paused=bool(data.get("paused", False)),
        )

    def has_any_filter(self) -> bool:
        return any(
            getattr(self, k) is not None
            for k in (
                "market_key",
                "outcome",
                "min_trade_amount_usd",
                "max_trade_amount_usd",
                "min_price_usd",
                "max_price_usd",
                "trader",
            )
        )

    def summary(self) -> str:
        parts: list[str] = []
        if self.market_title:
            parts.append(f"market: {self.market_title}")
        elif self.market_key:
            parts.append(f"market: {self.market_key}")
        if self.outcome:
            parts.append(f"outcome: {self.outcome}")
        if self.min_trade_amount_usd is not None or self.max_trade_amount_usd is not None:
            lo = self.min_trade_amount_usd
            hi = self.max_trade_amount_usd
            if lo is not None and hi is not None:
                parts.append(f"trade ${lo:,.0f}-${hi:,.0f}")
            elif lo is not None:
                parts.append(f"trade ≥ ${lo:,.0f}")
            else:
                parts.append(f"trade ≤ ${hi:,.0f}")
        if self.min_price_usd is not None or self.max_price_usd is not None:
            lo = self.min_price_usd
            hi = self.max_price_usd
            if lo is not None and hi is not None:
                parts.append(f"price {_cents(lo)}-{_cents(hi)}")
            elif lo is not None:
                parts.append(f"price ≥ {_cents(lo)}")
            else:
                parts.append(f"price ≤ {_cents(hi)}")
        if self.trader:
            parts.append(f"trader {self.trader[:10]}…")
        if not parts:
            return "(no filters)"
        return " · ".join(parts)


class UserStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._users: dict[str, User] = {}

    def load(self) -> None:
        """Synchronous load called once at startup."""
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("Could not read %s: %s", self._path, e)
            return
        raw = data.get("users") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            user = User.from_json(entry)
            if user.chat_id:
                self._users[user.chat_id] = user

    async def add(self, chat_id: str, username: str | None = None) -> bool:
        cid = str(chat_id).strip()
        if not cid:
            return False
        async with self._lock:
            if cid in self._users:
                if username and self._users[cid].username != username:
                    self._users[cid].username = username
                    self._save_unlocked()
                return False
            self._users[cid] = User(chat_id=cid, username=username)
            self._save_unlocked()
            return True

    async def remove(self, chat_id: str) -> bool:
        cid = str(chat_id).strip()
        async with self._lock:
            if cid not in self._users:
                return False
            del self._users[cid]
            self._save_unlocked()
            return True

    def chat_ids(self) -> list[str]:
        return list(self._users.keys())

    def count(self) -> int:
        return len(self._users)

    def _save_unlocked(self) -> None:
        payload = {
            "users": [u.to_json() for u in self._users.values()],
        }
        _atomic_write_json(self._path, payload)


class AlertStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._alerts: dict[str, Alert] = {}
        self._dirty = False
        self._last_persist = 0.0

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("Could not read %s: %s", self._path, e)
            return
        raw = data.get("alerts") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            alert = Alert.from_json(entry)
            if alert.id and alert.chat_id:
                self._alerts[alert.id] = alert

    async def add(self, alert: Alert) -> Alert:
        async with self._lock:
            self._alerts[alert.id] = alert
            self._save_unlocked()
            return alert

    async def delete(self, alert_id: str, chat_id: str) -> bool:
        async with self._lock:
            existing = self._alerts.get(alert_id)
            if not existing or existing.chat_id != str(chat_id):
                return False
            del self._alerts[alert_id]
            self._save_unlocked()
            return True

    async def set_paused(self, alert_id: str, chat_id: str, paused: bool) -> bool:
        async with self._lock:
            existing = self._alerts.get(alert_id)
            if not existing or existing.chat_id != str(chat_id):
                return False
            if existing.paused == paused:
                return True
            existing.paused = paused
            self._save_unlocked()
            return True

    def get(self, alert_id: str) -> Alert | None:
        return self._alerts.get(alert_id)

    def for_chat(self, chat_id: str) -> list[Alert]:
        cid = str(chat_id)
        return [a for a in self._alerts.values() if a.chat_id == cid and a.active]

    def active_alerts(self) -> Iterable[Alert]:
        for a in self._alerts.values():
            if a.active and not a.paused:
                yield a

    def mark_triggered(self, alert_ids: Iterable[str], at: float | None = None) -> None:
        """Update last_triggered_at in memory; persistence is debounced."""
        ts = at if at is not None else time.time()
        touched = False
        for aid in alert_ids:
            alert = self._alerts.get(aid)
            if alert is not None:
                alert.last_triggered_at = ts
                touched = True
        if touched:
            self._dirty = True

    async def persist_if_dirty(self, min_interval: float = 5.0) -> None:
        if not self._dirty:
            return
        if time.time() - self._last_persist < min_interval:
            return
        async with self._lock:
            if not self._dirty:
                return
            self._save_unlocked()
            self._dirty = False
            self._last_persist = time.time()

    def _save_unlocked(self) -> None:
        payload = {"alerts": [a.to_json() for a in self._alerts.values()]}
        _atomic_write_json(self._path, payload)
        self._dirty = False
        self._last_persist = time.time()


def new_alert_id() -> str:
    return uuid.uuid4().hex
