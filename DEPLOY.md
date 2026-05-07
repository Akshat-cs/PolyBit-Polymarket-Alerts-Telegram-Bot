# Deploying PolyBit

PolyBit is a long-running Python process that holds a WebSocket open to
Bitquery and serves Telegram updates. It is *not* an HTTP service, so
deploy targets that only run web servers (e.g. Vercel, Cloudflare
Workers, Render Web Service) won't work.

You need:

1. Persistent storage for `data/users.json` and `data/alerts.json` (so
   redeploys don't wipe user state).
2. An always-on process supervisor that restarts on crash.

Two deploy paths covered below: **Render** (zero-ops, $7.25/mo) and
**self-hosted Linux VPS** (cheaper, e.g. Hetzner CPX11 ~$5/mo).

---

## Option 1 — Render

### Prerequisites

- A GitHub repo with this codebase.
- A [Render](https://render.com) account connected to GitHub.

### Deploy via Blueprint (recommended)

The repo includes a `render.yaml` Blueprint. Render will use it to
auto-create the worker + persistent disk + env-var slots.

1. Render dashboard → **New +** → **Blueprint**.
2. Pick this repo. Render reads `render.yaml`.
3. Confirm the preview: a Worker named `polybit` and a 1 GB disk named
   `polybit-data` mounted at `/var/data`.
4. Render prompts for the two `sync: false` secrets (kept in Render only,
   never in the repo):
   - `BITQUERY_TOKEN`
   - `TELEGRAM_BOT_TOKEN`
5. Click **Apply**. First build is ~3–5 min.

### Deploy manually (no Blueprint)

Use this if Blueprint gives any trouble.

1. Dashboard → **New +** → **Background Worker** (NOT Web Service).
2. Connect repo, pick branch.
3. Settings:
   - **Name:** `polybit`
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python -m polybit`
   - **Plan:** Starter ($7/mo)
4. Add environment variables:

   | Key | Value |
   |---|---|
   | `BITQUERY_TOKEN` | *(your token)* |
   | `TELEGRAM_BOT_TOKEN` | *(your token)* |
   | `POLYBIT_LOG_LEVEL` | `INFO` |
   | `POLYBIT_DATA_DIR` | `/var/data` |

5. Add Disk:
   - **Name:** `polybit-data`
   - **Mount Path:** `/var/data` (must match `POLYBIT_DATA_DIR`)
   - **Size:** `1` GB

### Why a persistent disk

Render's container filesystem is **wiped on every redeploy and on most
restarts**. Without a persistent disk, the JSON stores are lost every
time you push code. The 1 GB disk costs $0.25/mo and survives redeploys.

The bot reads `POLYBIT_DATA_DIR` (see `polybit/config.py`) and writes
`users.json` + `alerts.json` directly to it. We use a few MB at most;
1 GB is just the minimum unit Render bills.

### Verify after deploy

1. **Logs tab** should show `Subscribed to Polymarket trades`.
2. Open your bot in Telegram → `/start` → welcome banner.
3. Set an alert via the bot. Render dashboard → **Shell** tab on the
   worker:
   ```sh
   ls -la /var/data
   cat /var/data/alerts.json
   ```
   You should see your alert. **That confirms the disk is wired.**
4. Push any tiny commit to redeploy. After redeploy, run `/myalerts` —
   your alert is still there.

### Cost

```
Background Worker (Starter):  $7.00/mo
Persistent Disk (1 GB):       $0.25/mo
                              -------
                              $7.25/mo
```

---

## Option 2 — Self-hosted Linux VPS (Hetzner / DigitalOcean / etc.)

Cheaper at scale (one VPS can host multiple bots). Outline for a fresh
Ubuntu/Debian server:

### One-time setup

```bash
# As root, bootstrap user + Python:
adduser polybit
apt update && apt install -y python3.11 python3.11-venv git

# Switch to the polybit user:
su - polybit

git clone https://github.com/<you>/Polymarket-price-telegram-bot.git
cd Polymarket-price-telegram-bot

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# nano .env — fill in BITQUERY_TOKEN and TELEGRAM_BOT_TOKEN

# Test once to make sure it boots:
python -m polybit
# Ctrl-C after you confirm it works
```

### `systemd` unit (auto-restart on crash, autostart on reboot)

Create `/etc/systemd/system/polybit.service` (as root):

```ini
[Unit]
Description=PolyBit Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=polybit
WorkingDirectory=/home/polybit/Polymarket-price-telegram-bot
EnvironmentFile=/home/polybit/Polymarket-price-telegram-bot/.env
ExecStart=/home/polybit/Polymarket-price-telegram-bot/.venv/bin/python -m polybit
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Then:

```bash
systemctl daemon-reload
systemctl enable --now polybit
systemctl status polybit
journalctl -u polybit -f      # tail logs
```

### Backups

The JSON files are tiny (a few MB at most), but you should still back
them up. Easiest: `cron` job that pushes `data/` to S3 / Backblaze B2 /
even a `git` repo nightly. Sample crontab:

```cron
# Nightly backup of data/ to a private bucket
0 3 * * * cd /home/polybit/Polymarket-price-telegram-bot && tar czf /tmp/polybit-data.tgz data/ && aws s3 cp /tmp/polybit-data.tgz s3://your-bucket/polybit/$(date +\%F).tgz
```

If you're on Hetzner, also flip on **Backups** in the Cloud Console
(+20% of VPS price) — it snapshots the whole disk daily.

### Running multiple bots on one VPS

Same VPS can host N bots. Per-bot:

```
/home/polybit/bot1/  (clone, .env, .venv, data/)
/home/polybit/bot2/  (...)
```

And per-bot `systemd` units: `polybit-bot1.service`, `polybit-bot2.service`,
each with its own `WorkingDirectory` and `EnvironmentFile`. `systemctl
restart polybit-bot3` only touches that one.

---

## Migrating local data to production

If you have alerts in your local `data/alerts.json` you want to keep:

1. Deploy first (production starts with empty `data/`).
2. **Render:** Shell tab on the worker → `cat > /var/data/alerts.json`,
   paste contents of local `alerts.json`, Ctrl-D. Repeat for `users.json`.
   Restart the worker.
3. **VPS:** `scp data/*.json polybit@<server>:~/Polymarket-price-telegram-bot/data/`,
   then `systemctl restart polybit`.

---

## Going public-repo checklist

Before flipping the GitHub repo to public:

- ✅ `.env` is gitignored (already is).
- ✅ `data/` is gitignored (already is).
- ✅ No real tokens committed anywhere in git history. (If you ever
  committed `.env` by accident, **rotate both tokens** before going
  public — Bitquery dashboard for the API token, BotFather `/revoke` for
  the Telegram one. Once a token is in public git history, it's leaked
  forever.)
- ✅ `.env.example` documents the required env vars (no real values).
- ✅ `README.md` and `DEPLOY.md` explain how a stranger can clone and run
  their own copy.
- ✅ License file present (`LICENSE`).

The Render deploy steps don't change for public vs private repos — Render
treats both the same.
