"""Centralized configuration: env loading + tunable constants."""

from __future__ import annotations

import logging
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Data directory holds the JSON files for users + alerts. In production
# (e.g. Render) this MUST point at a persistent-disk mount; the container
# filesystem is wiped on every deploy and most restarts. Override via the
# POLYBIT_DATA_DIR env var; falls back to <repo>/data for local dev.
DATA_DIR = Path(os.environ.get("POLYBIT_DATA_DIR", str(PROJECT_ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"
ALERTS_FILE = DATA_DIR / "alerts.json"
ASSETS_DIR = PROJECT_ROOT / "assets"
WELCOME_BANNER_PATH = ASSETS_DIR / "Bot-Logo.png"

BITQUERY_WS_URL = "wss://streaming.bitquery.io/graphql"
BITQUERY_HTTP_URL = "https://streaming.bitquery.io/graphql"
POLYMARKET_PROTOCOL_NAME = "polymarket"

MARKETS_CACHE_TTL_SECONDS = 60
TOP_MARKETS_LIMIT = 10
NEW_MARKETS_LIMIT = 10
SEARCH_MARKETS_LIMIT = 10
PAGE_SIZE = 5

# Time window used by Top/New/Search lists, the per-market detail "Stats"
# section, and the volume-enrichment sub-queries. Change this single value
# to widen/narrow every list and stats panel at once.
STATS_LOOKBACK_HOURS = 1

ALERT_COOLDOWN_SECONDS = 60
ALERTS_PERSIST_DEBOUNCE_SECONDS = 5

TELEGRAM_PER_CHAT_INTERVAL_SECONDS = 1.1
TELEGRAM_SENDER_WORKERS = 20
TELEGRAM_DEFAULT_TIMEOUT_SECONDS = 30

WS_RECONNECT_INITIAL_DELAY = 1.0
WS_RECONNECT_MAX_DELAY = 60.0
WS_RECONNECT_FACTOR = 2.0


def load_env() -> None:
    """Load .env from the project root if python-dotenv is installed."""
    if not load_dotenv:
        return
    env_path = PROJECT_ROOT / ".env"
    load_dotenv(env_path)
    load_dotenv()


def get_bitquery_token() -> str:
    return os.environ.get("BITQUERY_TOKEN", "").strip()


def get_telegram_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("POLYBIT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
