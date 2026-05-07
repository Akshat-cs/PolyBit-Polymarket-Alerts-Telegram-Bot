"""Bitquery client: WebSocket trade stream + HTTP aggregation queries.

The stream is the single global Polymarket-trades subscription that all
in-process matchers read. HTTP queries power the Top / New / Search market
browsing, with a 60s in-memory cache keyed by (query_kind, *args)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import quote

import httpx
from gql import gql
from gql.transport.exceptions import TransportQueryError
from gql.transport.websockets import WebsocketsTransport

from . import config, queries

logger = logging.getLogger(__name__)


@dataclass
class OutcomePrice:
    label: str
    price: float | None
    price_usd: float | None
    asset_id: str | None = None


@dataclass
class MarketRow:
    """Lightweight market summary used by browsing UI.

    `condition_id` and `question_id` are *different* hex identifiers; we
    keep both because they're used by different downstream services:
        - condition_id  -> Polymarket Gamma API canonical-URL lookup
        - question_id   -> DEXrabbit market page URL (OG image preview)
    """

    market_id: str
    title: str
    image: str | None = None
    volume_usd: float | None = None
    trade_count: int | None = None
    unique_buyers: int | None = None
    created_at: str | None = None
    outcomes: list[str] = field(default_factory=list)
    prices: list[OutcomePrice] = field(default_factory=list)
    last_trade_time: str | None = None
    condition_id: str | None = None
    question_id: str | None = None
    canonical_url: str | None = None


def _ws_url(token: str) -> str:
    return f"{config.BITQUERY_WS_URL}?token={quote(token, safe='')}"


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _s(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


@dataclass
class TradeEvent:
    """Normalized trade payload used by matcher + formatter.

    `buyer` and `seller` are NORMALIZED to the outcome-token perspective:
        buyer  = address that received outcome tokens (paid USDC collateral)
        seller = address that sent outcome tokens (received USDC collateral)

    The Bitquery `Trade.OutcomeTrade.Buyer/Seller` fields use the order-book
    convention which flips meaning based on `IsOutcomeBuy`:

        IsOutcomeBuy=true  -> Seller (maker) gives USDC, Buyer (taker) gives outcome tokens
                              => outcome_buyer = raw Seller, outcome_seller = raw Buyer
        IsOutcomeBuy=false -> Buyer gives USDC, Seller gives outcome tokens
                              => outcome_buyer = raw Buyer,  outcome_seller = raw Seller

    By collapsing this in `from_raw` the matcher and formatter can treat
    `buyer`/`seller` as "who took the outcome position" without re-deriving
    direction every time.
    """

    market_id: str
    market_title: str
    market_image: str | None
    outcome_label: str | None
    price: float | None
    price_usd: float | None
    amount: float | None
    collateral_usd: float | None
    is_outcome_buy: bool | None
    buyer: str | None
    seller: str | None
    tx_hash: str | None
    block_time: str | None
    condition_id: str | None = None
    # See MarketRow docstring — distinct from condition_id; used by DEXrabbit.
    question_id: str | None = None

    @classmethod
    def from_raw(cls, row: dict[str, Any]) -> "TradeEvent | None":
        if not isinstance(row, dict):
            return None
        trade = (row or {}).get("Trade") or {}
        pred = trade.get("Prediction") or {}
        question = pred.get("Question") or {}
        outcome = pred.get("Outcome") or {}
        ot = trade.get("OutcomeTrade") or {}
        tx = row.get("Transaction") or {}
        block = row.get("Block") or {}
        market_id = _s(question.get("MarketId"))
        title = _s(question.get("Title")) or "(unknown market)"
        if not market_id:
            return None
        is_outcome_buy = ot.get("IsOutcomeBuy")
        raw_buyer = _s(ot.get("Buyer"))
        raw_seller = _s(ot.get("Seller"))
        if is_outcome_buy is True:
            outcome_buyer = raw_seller
            outcome_seller = raw_buyer
        else:
            outcome_buyer = raw_buyer
            outcome_seller = raw_seller
        return cls(
            market_id=market_id,
            market_title=title,
            market_image=_s(question.get("Image")),
            outcome_label=_s(outcome.get("Label")),
            price=_f(ot.get("Price")),
            price_usd=_f(ot.get("PriceInUSD")),
            amount=_f(ot.get("Amount")),
            collateral_usd=_f(ot.get("CollateralAmountInUSD")),
            is_outcome_buy=is_outcome_buy,
            buyer=outcome_buyer,
            seller=outcome_seller,
            tx_hash=_s(tx.get("Hash")),
            block_time=_s(block.get("Time")),
            condition_id=_s(pred.get("ConditionId")),
            question_id=_s(question.get("Id")),
        )


class BitqueryStreamer:
    """Subscribes to Polymarket trades and dispatches each TradeEvent to
    the registered handler. Reconnects with exponential backoff on failure."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._stop = asyncio.Event()
        self._handlers: list[Callable[[TradeEvent], Awaitable[None]]] = []

    def add_handler(self, fn: Callable[[TradeEvent], Awaitable[None]]) -> None:
        self._handlers.append(fn)

    async def _consume_once(self) -> None:
        transport = WebsocketsTransport(
            url=_ws_url(self._token),
            headers={"Sec-WebSocket-Protocol": "graphql-ws"},
        )
        await transport.connect()
        logger.info("Bitquery WS connected")
        try:
            async for result in transport.subscribe(gql(queries.TRADES_SUBSCRIPTION)):
                if self._stop.is_set():
                    break
                if result.errors:
                    logger.error("Bitquery GraphQL errors: %s", result.errors)
                    continue
                data = result.data or {}
                evm = data.get("EVM") or {}
                rows = evm.get("PredictionTrades") or []
                if not isinstance(rows, list):
                    rows = [rows]
                for row in rows:
                    event = TradeEvent.from_raw(row)
                    if event is None:
                        continue
                    for handler in self._handlers:
                        try:
                            await handler(event)
                        except Exception:
                            logger.exception("Trade handler raised; continuing")
        finally:
            try:
                await transport.close()
            except Exception:
                pass
            logger.info("Bitquery WS disconnected")

    async def run(self) -> None:
        delay = config.WS_RECONNECT_INITIAL_DELAY
        while not self._stop.is_set():
            try:
                await self._consume_once()
                if self._stop.is_set():
                    return
                logger.warning("Bitquery WS ended cleanly; reconnecting in %.1fs", delay)
            except asyncio.CancelledError:
                raise
            except TransportQueryError as e:
                logger.error("Bitquery query error: %s; reconnecting in %.1fs", e, delay)
            except Exception:
                logger.exception("Bitquery WS error; reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            delay = min(delay * config.WS_RECONNECT_FACTOR, config.WS_RECONNECT_MAX_DELAY)

    def stop(self) -> None:
        self._stop.set()


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
POLYMARKET_BASE = "https://polymarket.com"


class GammaResolver:
    """Resolves canonical Polymarket URLs from CTF condition IDs, batching
    requests to https://gamma-api.polymarket.com/markets and caching results
    in-process. Slugs effectively never change once published, so we cache
    indefinitely (process lifetime)."""

    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}  # condition_id (lower) -> url or None
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "GammaResolver":
        self._client = httpx.AsyncClient(
            base_url=GAMMA_API_BASE,
            headers={"User-Agent": "PolyBit/1.0 (+telegram-bot)"},
            timeout=15,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _url_from_market(m: dict[str, Any]) -> str | None:
        slug = _s(m.get("slug"))
        if not slug:
            return None
        events = m.get("events") or []
        event_slug = None
        if isinstance(events, list) and events:
            head = events[0]
            if isinstance(head, dict):
                event_slug = _s(head.get("slug"))
        if event_slug:
            return f"{POLYMARKET_BASE}/event/{event_slug}/{slug}"
        return f"{POLYMARKET_BASE}/market/{slug}"

    async def _fetch_batch(self, condition_ids: list[str], closed: bool) -> dict[str, str]:
        """Hit gamma-api once for the given IDs. Returns {cond_id_lower: url}."""
        if not condition_ids or self._client is None:
            return {}
        params: list[tuple[str, str]] = [("closed", "true" if closed else "false")]
        for cid in condition_ids:
            params.append(("condition_ids", cid))
        try:
            r = await self._client.get("/markets", params=params)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("gamma-api request failed: %s", e)
            return {}
        out: dict[str, str] = {}
        if not isinstance(data, list):
            return out
        for m in data:
            if not isinstance(m, dict):
                continue
            cid = _s(m.get("conditionId"))
            url = self._url_from_market(m)
            if cid and url:
                out[cid.lower()] = url
        return out

    async def resolve_by_slug(self, slug: str) -> dict[str, Any] | None:
        """Look up a single market by its URL slug.

        Polymarket pages live at /event/<event-slug>/<market-slug> or
        /market/<slug>; both produce the same `slug` we pass here. We try
        open markets first, then closed, since `closed=false` is gamma's
        default and silently hides resolved markets.
        """
        s = (slug or "").strip().lower()
        if not s or self._client is None:
            return None
        for closed in ("false", "true"):
            try:
                r = await self._client.get(
                    "/markets", params={"slug": s, "closed": closed}
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                logger.warning("gamma-api slug lookup failed (%s): %s", closed, e)
                continue
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict):
                    return first
            elif isinstance(data, dict) and data.get("slug"):
                return data
        return None

    async def resolve(self, condition_ids: list[str]) -> dict[str, str | None]:
        """Returns {condition_id_lower: url_or_None} for every requested id.
        Uses cache + at most two gamma-api round-trips (closed=false then
        closed=true) per call, regardless of batch size."""
        wanted = {c.strip().lower() for c in condition_ids if c}
        if not wanted:
            return {}
        result: dict[str, str | None] = {}
        missing: list[str] = []
        async with self._lock:
            for cid in wanted:
                if cid in self._cache:
                    result[cid] = self._cache[cid]
                else:
                    missing.append(cid)
        if not missing:
            return result

        # Try open markets first, then closed; merge.
        open_hits = await self._fetch_batch(missing, closed=False)
        still_missing = [c for c in missing if c not in open_hits]
        closed_hits = await self._fetch_batch(still_missing, closed=True) if still_missing else {}

        async with self._lock:
            for cid in missing:
                url = open_hits.get(cid) or closed_hits.get(cid)
                self._cache[cid] = url  # cache None too, so we don't retry every time
                result[cid] = url
        return result


class BitqueryHTTP:
    """HTTP GraphQL client with a 60s in-memory cache for browse queries."""

    def __init__(self, token: str, gamma: "GammaResolver | None" = None) -> None:
        self._token = token
        self._cache: dict[tuple[Any, ...], tuple[float, list[MarketRow]]] = {}
        self._cache_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._gamma = gamma

    async def __aenter__(self) -> "BitqueryHTTP":
        self._client = httpx.AsyncClient(
            base_url=config.BITQUERY_HTTP_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _exec(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None, "Use within `async with` context"
        r = await self._client.post(
            "",
            json={"query": query, "variables": variables},
        )
        r.raise_for_status()
        payload = r.json()
        if isinstance(payload, dict) and payload.get("errors"):
            logger.error("Bitquery HTTP errors: %s", payload["errors"])
        return payload.get("data") or {}

    @staticmethod
    def _rows_from_trade_aggregate(data: dict[str, Any]) -> list[MarketRow]:
        evm = (data or {}).get("EVM") or {}
        rows = evm.get("PredictionTrades") or []
        out: list[MarketRow] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            pred = (((row.get("Trade") or {}).get("Prediction"))) or {}
            q = pred.get("Question") or {}
            mid = _s(q.get("MarketId"))
            if not mid:
                continue
            out.append(
                MarketRow(
                    market_id=mid,
                    title=_s(q.get("Title")) or "(unknown market)",
                    image=_s(q.get("Image")),
                    volume_usd=_f(row.get("volume_usd")),
                    trade_count=_i(row.get("trade_count")),
                    unique_buyers=_i(row.get("unique_buyers")),
                    condition_id=_s(pred.get("ConditionId")),
                    question_id=_s(q.get("Id")),
                )
            )
        return out

    @staticmethod
    def _rows_from_managements(data: dict[str, Any]) -> list[MarketRow]:
        evm = (data or {}).get("EVM") or {}
        rows = evm.get("PredictionManagements") or []
        out: list[MarketRow] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            mgmt = row.get("Management") or {}
            pred = mgmt.get("Prediction") or {}
            q = pred.get("Question") or {}
            cond = pred.get("Condition") or {}
            outcomes_raw = cond.get("Outcomes") or []
            outcomes = [
                _s((o or {}).get("Label")) or ""
                for o in outcomes_raw
                if isinstance(o, dict)
            ]
            outcomes = [o for o in outcomes if o]
            mid = _s(q.get("MarketId"))
            if not mid:
                continue
            out.append(
                MarketRow(
                    market_id=mid,
                    title=_s(q.get("Title")) or "(unknown market)",
                    image=_s(q.get("Image")),
                    created_at=_s(q.get("CreatedAt")) or _s((row.get("Block") or {}).get("Time")),
                    outcomes=outcomes,
                    condition_id=_s(pred.get("ConditionId")),
                    question_id=_s(q.get("Id")),
                )
            )
        return out

    async def _cached(
        self,
        cache_key: tuple[Any, ...],
        fetch: Callable[[], Awaitable[list[MarketRow]]],
    ) -> list[MarketRow]:
        now = time.time()
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] < config.MARKETS_CACHE_TTL_SECONDS:
                return cached[1]
        rows = await fetch()
        async with self._cache_lock:
            self._cache[cache_key] = (now, rows)
        return rows

    async def _enrich_prices(self, rows: list[MarketRow]) -> None:
        """Fill `prices` and `last_trade_time` on each row with one query."""
        ids = [r.market_id for r in rows if r.market_id]
        if not ids:
            return
        try:
            data = await self._exec(
                queries.CURRENT_PRICES_FOR_MARKETS, {"marketIds": ids}
            )
        except Exception as e:
            logger.warning("price enrichment failed: %s", e)
            return
        evm = (data or {}).get("EVM") or {}
        result_rows = evm.get("PredictionTrades") or []
        per_market: dict[str, list[OutcomePrice]] = {}
        latest_time: dict[str, str] = {}
        for entry in result_rows:
            if not isinstance(entry, dict):
                continue
            trade = entry.get("Trade") or {}
            pred = trade.get("Prediction") or {}
            q = pred.get("Question") or {}
            outcome = pred.get("Outcome") or {}
            outcome_token = pred.get("OutcomeToken") or {}
            ot = trade.get("OutcomeTrade") or {}
            block = entry.get("Block") or {}
            mid = _s(q.get("MarketId"))
            if not mid:
                continue
            per_market.setdefault(mid, []).append(
                OutcomePrice(
                    label=_s(outcome.get("Label")) or "?",
                    price=_f(ot.get("Price")),
                    price_usd=_f(ot.get("PriceInUSD")),
                    asset_id=_s(outcome_token.get("AssetId")),
                )
            )
            t = _s(block.get("Time"))
            if t and (mid not in latest_time or t > latest_time[mid]):
                latest_time[mid] = t
        for r in rows:
            ps = per_market.get(r.market_id)
            if ps:
                r.prices = ps
            t = latest_time.get(r.market_id)
            if t:
                r.last_trade_time = t

    async def _resolve_polymarket_urls(self, rows: list[MarketRow]) -> None:
        """Fill `canonical_url` on each row by asking gamma-api for the slug
        of every distinct condition_id we have. Best-effort: rows without a
        condition_id, or whose slug isn't in gamma-api yet, keep
        `canonical_url=None` and the caller falls back to a slugified title."""
        if self._gamma is None:
            return
        cids = [r.condition_id for r in rows if r.condition_id]
        if not cids:
            return
        try:
            urls = await self._gamma.resolve(cids)
        except Exception:
            logger.exception("gamma-api resolve failed")
            return
        for r in rows:
            if r.condition_id and r.canonical_url is None:
                r.canonical_url = urls.get(r.condition_id.lower())

    async def _enrich_volumes(
        self, rows: list[MarketRow], hours: int = config.STATS_LOOKBACK_HOURS
    ) -> None:
        """Fill volume / trade_count / unique_buyers on rows missing it."""
        ids = [r.market_id for r in rows if r.market_id and r.volume_usd is None]
        if not ids:
            return
        try:
            data = await self._exec(
                queries.VOLUMES_FOR_MARKETS,
                {"marketIds": ids, "hours": hours},
            )
        except Exception as e:
            logger.warning("volume enrichment failed: %s", e)
            return
        evm = (data or {}).get("EVM") or {}
        result_rows = evm.get("PredictionTrades") or []
        by_id: dict[str, dict[str, Any]] = {}
        for entry in result_rows:
            if not isinstance(entry, dict):
                continue
            q = ((entry.get("Trade") or {}).get("Prediction") or {}).get("Question") or {}
            mid = _s(q.get("MarketId"))
            if not mid:
                continue
            by_id[mid] = {
                "volume_usd": _f(entry.get("volume_usd")),
                "trade_count": _i(entry.get("trade_count")),
                "unique_buyers": _i(entry.get("unique_buyers")),
            }
        for r in rows:
            data_for_row = by_id.get(r.market_id)
            if not data_for_row:
                continue
            if r.volume_usd is None:
                r.volume_usd = data_for_row.get("volume_usd")
            if r.trade_count is None:
                r.trade_count = data_for_row.get("trade_count")
            if r.unique_buyers is None:
                r.unique_buyers = data_for_row.get("unique_buyers")

    async def top_markets(
        self, sort: str = "volume", hours: int = config.STATS_LOOKBACK_HOURS
    ) -> list[MarketRow]:
        sort = sort.lower()
        query_map = {
            "volume": queries.TOP_MARKETS_BY_VOLUME,
            "traders": queries.TOP_MARKETS_BY_TRADERS,
            "trades": queries.TOP_MARKETS_BY_TRADES,
        }
        q = query_map.get(sort, queries.TOP_MARKETS_BY_VOLUME)

        async def _fetch() -> list[MarketRow]:
            data = await self._exec(
                q, {"hours": hours, "limit": config.TOP_MARKETS_LIMIT}
            )
            rows = self._rows_from_trade_aggregate(data)
            await self._enrich_prices(rows)
            await self._resolve_polymarket_urls(rows)
            return rows

        return await self._cached(("top", sort, hours), _fetch)

    async def new_markets(self) -> list[MarketRow]:
        """DORMANT — kept for easy re-enabling.

        The /new command and its main-menu button were removed because
        brand-new markets typically have no trades and no Gamma index
        entry yet, which made the UI render mostly empty rows with
        broken Polymarket links. The query and parsers are still
        correct; wire them back up when we have a smarter "new markets"
        UX (e.g., gated on Gamma indexing + first-trade signal).
        """
        async def _fetch() -> list[MarketRow]:
            data = await self._exec(
                queries.NEW_MARKETS,
                {"limit": config.NEW_MARKETS_LIMIT},
            )
            rows = self._rows_from_managements(data)
            await self._enrich_volumes(rows)
            await self._enrich_prices(rows)
            await self._resolve_polymarket_urls(rows)
            return rows

        return await self._cached(("new",), _fetch)

    async def search_markets(self, q: str) -> list[MarketRow]:
        q_norm = (q or "").strip()
        if not q_norm:
            return []

        async def _fetch() -> list[MarketRow]:
            data = await self._exec(
                queries.SEARCH_MARKETS,
                {
                    "q": q_norm,
                    "limit": config.SEARCH_MARKETS_LIMIT,
                    "hours": config.STATS_LOOKBACK_HOURS,
                },
            )
            rows = self._rows_from_trade_aggregate(data)
            await self._enrich_prices(rows)
            await self._resolve_polymarket_urls(rows)
            return rows

        return await self._cached(("search", q_norm.lower()), _fetch)

    async def current_prices(
        self, market_id: str
    ) -> tuple[
        str | None, str | None, list[OutcomePrice], str | None, str | None, str | None
    ]:
        """Returns (title, image, [OutcomePrice...], last_block_time,
        condition_id, question_id).

        condition_id and question_id are *different* hex IDs (see MarketRow
        docstring) — both are returned because callers need both.
        """
        data = await self._exec(
            queries.CURRENT_PRICES_FOR_MARKET, {"marketId": market_id}
        )
        evm = (data or {}).get("EVM") or {}
        rows = evm.get("PredictionTrades") or []
        title: str | None = None
        image: str | None = None
        condition_id: str | None = None
        question_id: str | None = None
        prices: list[OutcomePrice] = []
        latest_time: str | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            trade = row.get("Trade") or {}
            pred = trade.get("Prediction") or {}
            q = pred.get("Question") or {}
            ot = trade.get("OutcomeTrade") or {}
            outcome = pred.get("Outcome") or {}
            outcome_token = pred.get("OutcomeToken") or {}
            block = row.get("Block") or {}
            title = title or _s(q.get("Title"))
            image = image or _s(q.get("Image"))
            condition_id = condition_id or _s(pred.get("ConditionId"))
            question_id = question_id or _s(q.get("Id"))
            prices.append(
                OutcomePrice(
                    label=_s(outcome.get("Label")) or "?",
                    price=_f(ot.get("Price")),
                    price_usd=_f(ot.get("PriceInUSD")),
                    asset_id=_s(outcome_token.get("AssetId")),
                )
            )
            t = _s(block.get("Time"))
            if t and (latest_time is None or t > latest_time):
                latest_time = t
        return title, image, prices, latest_time, condition_id, question_id

    async def fetch_recent_stats(
        self, market_id: str, hours: int = config.STATS_LOOKBACK_HOURS
    ) -> dict[str, Any]:
        """Volume / trade count / unique traders for a single market within
        the configured lookback window."""
        data = await self._exec(
            queries.VOLUMES_FOR_MARKETS,
            {"marketIds": [market_id], "hours": hours},
        )
        evm = (data or {}).get("EVM") or {}
        rows = evm.get("PredictionTrades") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            return {
                "volume_usd": _f(row.get("volume_usd")),
                "trade_count": _i(row.get("trade_count")),
                "unique_buyers": _i(row.get("unique_buyers")),
            }
        return {}

    def cache_clear(self, prefix: str | None = None) -> None:
        """Drop cached browse results. With a prefix, only matching keys."""
        if prefix is None:
            self._cache.clear()
            return
        for key in list(self._cache.keys()):
            if key and key[0] == prefix:
                self._cache.pop(key, None)
