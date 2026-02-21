# Crypto Scalper — Polymarket 15-min ETH/BTC/SOL Bot

Automated bot that trades 15-minute crypto Up-or-Down prediction markets on Polymarket. Places GTC limit buys at $0.25 on both sides — if the token wins it pays $1.00 (4:1 return), if it loses it pays $0.

## Strategy

Every 15 seconds:
1. **Discover** — Find active 15-min ETH/BTC/SOL Up-or-Down markets via Polymarket Gamma API
2. **Dual-window bidding** — Bid on both the current and next 15-min windows
3. **Place orders** — GTC limit buy at $0.25 on both Up and Down for each asset
4. **Hold to expiry** — Positions ride until the window closes
5. **Auto-redeem** — Winning tokens redeemed on-chain after market resolution

### Economics

| Parameter | Value |
|-----------|-------|
| Bid price | $0.25 per token |
| Tokens per bid | 20 (= $5 per side) |
| Max per cycle | $30 (3 assets x 2 sides x $5) |
| Win payout | $1.00 per token ($20 on 20 tokens) |
| Odds | 4:1 — need >25% win rate to profit |

## Server

| Item | Detail |
|------|--------|
| IP | `46.62.211.255` |
| Container | `vig-scalper` |
| Port | 8081 |
| Dashboard | http://46.62.211.255:8081 |
| Wallet | `0x4ae36dfA7CD02BB87334EDC35639f70981c02F54` |

## Setup

```bash
cp .env.example .env
# Edit .env with your PRIVATE_KEY
pip install -r requirements.txt
python scalper.py
```

## Docker

```bash
docker build -t vig-scalper .
docker run -d \
  --name vig-scalper \
  -p 8081:8081 \
  -v vig-data:/app/data \
  --env-file .env \
  vig-scalper
```

## Deploy

```bash
# Quick update (hot-swap files into running container)
scp scalper.py root@46.62.211.255:/root/vig/scalper.py
scp dashboard.html root@46.62.211.255:/root/vig/dashboard.html
ssh root@46.62.211.255 "docker cp /root/vig/scalper.py vig-scalper:/app/scalper.py && \
  docker cp /root/vig/dashboard.html vig-scalper:/app/dashboard.html && \
  docker restart vig-scalper"
```

## Configuration

| Env Variable | Default | Description |
|-------------|---------|-------------|
| `PRIVATE_KEY` | — | Polygon wallet key (onboarded on Polymarket) |
| `RPC_URL` | `https://polygon-bor-rpc.publicnode.com` | Polygon RPC endpoint |
| `SCALP_BET_SIZE` | 10 | Tokens per bid |
| `SCALP_ASSETS` | eth,btc | Comma-separated asset list |
| `SCALP_POLL_SECONDS` | 15 | Scan interval (seconds) |
| `SCALP_MIN_TIME_LEFT` | 300 | Min seconds remaining to enter a window |
| `SCALP_SELL_OFFSET` | 0.04 | Offset for manual sell pricing |
| `SCALP_PORT` | 8081 | Dashboard port |

## Utility Scripts

| Script | Purpose |
|--------|---------|
| `scripts/approve_ctf.py` | Approve CTF contract for token transfers |
| `scripts/approve_usdc.py` | Approve USDC spending on exchange |
| `scripts/cancel_all.py` | Cancel all open orders |
| `scripts/redeem.py` | Redeem winning tokens |
| `scripts/sell_all.py` | Market sell all held tokens |

## Key APIs & Contracts

| Name | Address/URL |
|------|-------------|
| Polymarket CLOB | `https://clob.polymarket.com` |
| Gamma API | `https://gamma-api.polymarket.com` |
| CTF Contract | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| USDC (Polygon) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| CTF Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| Neg Risk Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |

## Data Files (`/app/data/`)

| File | Description |
|------|-------------|
| `scalp_positions.json` | Active held positions |
| `scalp_closed.json` | Closed/resolved trades |
| `scalp_closed_backup.json` | Old history backup |

## Known Issues

1. **Resolution delay** — Polymarket can take hours to resolve 15-min markets on-chain. Bot retries every 15 seconds automatically.
2. **Phantom CLOB balances** — Stale token balances after settlement; clears eventually.
3. **Cancel bug (fixed v6+)** — Versions v1-v5 mislabeled filled orders as cancelled. Fixed in v6+.
