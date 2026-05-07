# PolyBit — Polymarket Telegram Alerts Bot

<p align="center">
  <a href="https://t.me/PolyBit_Polymarket_Bot">
    <img src="https://img.shields.io/badge/Add%20PolyBit%20on%20Telegram-26A5E4?style=for-the-badge&logo=telegram&logoColor=white" alt="Add PolyBit on Telegram" height="44">
  </a>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="https://github.com/python-telegram-bot/python-telegram-bot"><img src="https://img.shields.io/badge/python--telegram--bot-v21-26A5E4?style=flat-square&logo=telegram&logoColor=white" alt="python-telegram-bot v21"></a>
  <a href="https://docs.bitquery.io/docs/examples/prediction-market/prediction-market-api/"><img src="https://img.shields.io/badge/data-Bitquery-FF6B35?style=flat-square" alt="Powered by Bitquery"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License"></a>
</p>

A Telegram bot that streams Polymarket prediction-market data via the
[Bitquery Prediction Market API](https://docs.bitquery.io/docs/examples/prediction-market/prediction-market-api/)
and lets users:

- Browse top markets by **volume**, **unique traders**, or **trade count**.
- Search markets by keyword or paste a Polymarket event URL.
- Open per-market detail cards with current outcome prices, 1-hour stats,
  and a one-tap "Set Alert on this Market" button.
- Configure custom alerts on **trade size**, **share price**, **specific
  trader wallet**, and/or **specific market** — any filter you skip is a
  wildcard.
- Receive realtime trade notifications as alerts fire, with inline links
  to Polymarket, PolygonScan, and the trader's Polymarket profile.
- Deep-link from any external site (e.g.
  [DEXrabbit](https://dexrabbit.bitquery.io/polymarket-predictions))
  straight to a specific market screen via
  `https://t.me/<bot>?start=market_<MarketId>`.

Storage is flat JSON files on disk — no database required, multi-user safe
via per-store `asyncio.Lock` and atomic writes.

## Stack

- **Python 3.11+** with `asyncio`
- [`python-telegram-bot v21`](https://github.com/python-telegram-bot/python-telegram-bot)
- [`gql`](https://github.com/graphql-python/gql) for Bitquery GraphQL
  (WebSocket subscriptions + HTTP queries)
- [`httpx`](https://www.python-httpx.org/) for the Polymarket Gamma API
  (used only to resolve canonical Polymarket event URLs)

All market data — titles, prices, volume, traders, trade counts, real-time
trade events — comes from Bitquery. The Gamma API is only consulted to map
`condition_id → /event/<slug>` for the "View on Polymarket" links.

## Quick start (local dev)

```bash
git clone https://github.com/<you>/Polymarket-price-telegram-bot.git
cd Polymarket-price-telegram-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — paste BITQUERY_TOKEN and TELEGRAM_BOT_TOKEN

python -m polybit
```

You'll need:

1. A **Bitquery API token** — grab one from
   [account.bitquery.io](https://account.bitquery.io/user/api_v2/access_tokens).
   Free tier is plenty for a single bot.
2. A **Telegram bot token** — talk to
   [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, copy
   the `123456:ABC…` token it gives you.

That's it. Run `python -m polybit`, open your bot in Telegram, send
`/start`. The bot logs to stdout.

## Project layout

```
polybit/
  __main__.py       Entry point (python -m polybit)
  main.py           Wires up Bitquery streamer + Telegram app + sender
  bot.py            Telegram handlers, navigation, Add Alert wizard
  bitquery.py       WebSocket streamer + HTTP client + Gamma resolver
  queries.py        GraphQL query strings
  formatting.py     HTML message builders, keyboards, URL helpers
  store.py          UserStore / AlertStore (JSON file persistence)
  matcher.py        Trade → alert filter matching
  sender.py         Outbound Telegram queue with per-chat throttling
  config.py         Centralized config / env loading
data/               users.json + alerts.json (gitignored)
assets/             Welcome banner image
```

## Deployment

The repo ships with a Render Blueprint (`render.yaml`) for one-click
deploys. See [DEPLOY.md](./DEPLOY.md) for step-by-step instructions
including the persistent-disk setup needed to survive redeploys without
losing user/alert data.

Quick summary for Render:

- Background Worker (Starter, $7/mo)
- 1 GB Persistent Disk mounted at `/var/data` ($0.25/mo)
- Set env vars: `BITQUERY_TOKEN`, `TELEGRAM_BOT_TOKEN`, `POLYBIT_DATA_DIR=/var/data`
- Push to GitHub → Render auto-deploys

For self-hosting on a VPS (Hetzner, DigitalOcean, etc.), use `systemd` to
keep the bot running. A sample unit file is in `DEPLOY.md`.

## Deep linking from external sites

External sites can deep-link users into the bot at a specific market with:

```
https://t.me/<your_bot_username>?start=market_<MarketId>
```

`<MarketId>` is the Polymarket numeric market id (the `Question.MarketId`
field in any Bitquery prediction-market response). The bot handles the
payload in `polybit/bot.py:start()` and renders the matching market card
directly — no welcome screen between the click and the data.

## Built-in commands

| Command | What it does |
|---|---|
| `/start` | Opens the main menu (or, with a deep-link payload, the matching market) |
| `/topmarkets` | Top markets in the last 1 hour by volume |
| `/search` | Search markets by keyword or paste a Polymarket URL |
| `/myalerts` | View / manage your active alerts |
| `/addalert` | Create a new alert |
| `/cancel` | Cancel the current input wizard |
| `/stop` | Stop receiving alerts (unregister) |
| `/help` | Command reference |

## License

MIT — see [LICENSE](./LICENSE).

## Credits

Built on top of the [Bitquery](https://bitquery.io) Prediction Market API.
Powered by [Polymarket](https://polymarket.com) on-chain data.
