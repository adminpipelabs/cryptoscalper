"""
Crypto Scalper v9 — Data API reconciliation + Builder relayer (gasless)
"""
import os, json, time, logging, threading, requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, request as flask_request, jsonify, Response
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, OrderType, CreateOrderOptions,
    BalanceAllowanceParams, AssetType,
)
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.constants import POLYGON
import httpx

load_dotenv()

_RAW_PROXY = os.getenv("RESIDENTIAL_PROXY_URL") or os.getenv("PROXY_URL") or ""
if _RAW_PROXY and "-country-" not in _RAW_PROXY:
    _PROXY_URL = _RAW_PROXY.replace("residential_proxy1:", "residential_proxy1-country-gb:")
else:
    _PROXY_URL = _RAW_PROXY
if _PROXY_URL:
    _OrigClient = httpx.Client
    class _ProxiedClient(_OrigClient):
        def __init__(self, **kwargs):
            if "proxy" not in kwargs and "proxies" not in kwargs:
                kwargs["proxy"] = _PROXY_URL
            super().__init__(**kwargs)
    httpx.Client = _ProxiedClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scalper")

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
BID_PRICE = 0.25
BID_AMOUNT = 5.0
POLL_SECONDS = 15
MIN_TIME_LEFT = 120
PORT = int(os.getenv("SCALP_PORT", "8081"))
ASSETS = ["eth", "btc", "sol"]
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
RPC_URL = os.getenv("RPC_URL", "https://polygon-bor-rpc.publicnode.com")
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ABI = [
    {"inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],
     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},
                {"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],
     "name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"operator","type":"address"}],
     "name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],
     "name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"},
]
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
POSITIONS_FILE = os.path.join(DATA_DIR, "scalp_positions.json")
CLOSED_FILE = os.path.join(DATA_DIR, "scalp_closed.json")

BUILDER_KEY = os.getenv("POLY_BUILDER_API_KEY", "")
BUILDER_SECRET = os.getenv("POLY_BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.getenv("POLY_BUILDER_PASSPHRASE", "")
RECONCILE_INTERVAL = 120  # seconds between Data API reconciliation sweeps

clob = None
w3 = None
w3_account = None
ctf_contract = None
neg_risk_adapter = None
relay_client = None
bot_paused = False
positions = []
closed = []
stats = {"wins": 0, "losses": 0, "pnl": 0.0}
cache = {"bal": 0, "bids": {}, "last_reconcile": 0}
flask_app = Flask(__name__)

# ── Persistence ──

def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f: json.dump(data, f, indent=2)

# ── Market discovery ──

def find_current_markets():
    now = int(time.time())
    current_slot = (now // 900) * 900
    next_slot = current_slot + 900
    markets = []
    for slot in [current_slot, next_slot]:
        window_end = slot + 900
        time_left = window_end - now
        if time_left < MIN_TIME_LEFT:
            continue
        for asset in ASSETS:
            slug = f"{asset}-updown-15m-{slot}"
            try:
                r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=5)
                data = r.json()
                if not data:
                    continue
                event = data[0]
                mkt = event.get("markets", [{}])[0]
                if mkt.get("closed") or not mkt.get("active"):
                    continue
                tokens = mkt.get("clobTokenIds", "")
                outcomes = mkt.get("outcomes", "")
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if len(tokens) < 2 or len(outcomes) < 2:
                    continue
                up_idx = outcomes.index("Up") if "Up" in outcomes else 0
                down_idx = outcomes.index("Down") if "Down" in outcomes else 1
                markets.append({
                    "slug": slug, "asset": asset, "title": event.get("title", ""),
                    "end_ts": window_end, "time_left": time_left,
                    "up_token": tokens[up_idx], "down_token": tokens[down_idx],
                    "condition_id": mkt.get("conditionId", ""),
                    "market_id": mkt.get("id", ""),
                    "tick_size": float(mkt.get("orderPriceMinTickSize", 0.01)),
                    "neg_risk": bool(mkt.get("negRisk", False)),
                })
            except Exception as e:
                log.debug("Discovery fail %s: %s", slug, e)
    return markets

# ── Balance helpers ──

def usdc_balance():
    try:
        b = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return int(b.get("balance", 0)) / 1e6
    except Exception: return 0.0

def token_balance_onchain(token_id):
    """Authoritative on-chain CTF balance."""
    try:
        return ctf_contract.functions.balanceOf(w3_account.address, int(token_id)).call() // 1_000_000
    except Exception as e:
        log.warning("On-chain balance failed %s: %s", str(token_id)[:20], e)
        return -1

def token_balance(token_id):
    """On-chain first, CLOB fallback only if RPC fails."""
    onchain = token_balance_onchain(token_id)
    if onchain >= 0:
        return onchain
    try:
        b = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id))
        return int(b.get("balance", 0)) // 1_000_000
    except Exception:
        return 0

# ── Data API (Polymarket's authoritative position tracker) ──

def data_api_positions():
    """Fetch open positions from Polymarket Data API — the source of truth."""
    try:
        r = requests.get(f"{DATA_API}/positions", params={"user": w3_account.address.lower()}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug("Data API positions error: %s", e)
    return []

def data_api_value():
    """Fetch total portfolio value from Data API."""
    try:
        r = requests.get(f"{DATA_API}/value", params={"user": w3_account.address.lower()}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data:
                return data[0].get("value", 0)
    except Exception:
        pass
    return 0

def reconcile_positions():
    """
    Compare bot's internal position list with Polymarket's Data API.
    Adopt any positions the bot lost track of and redeem any redeemable ones.
    """
    now = time.time()
    if now - cache.get("last_reconcile", 0) < RECONCILE_INTERVAL:
        return
    cache["last_reconcile"] = now

    api_positions = data_api_positions()
    if not api_positions:
        return

    tracked_tokens = {p["token_id"] for p in positions}
    changed = False

    for ap in api_positions:
        token_id = ap.get("asset", "")
        condition_id = ap.get("conditionId", "")
        redeemable = ap.get("redeemable", False)
        size = ap.get("size", 0)
        outcome = ap.get("outcome", "")
        title = ap.get("title", "")
        slug = ap.get("slug", "")
        cur_price = ap.get("curPrice", 0)

        if redeemable and condition_id:
            log.info("RECONCILE: redeemable position found — %s %s (%.0f tokens @ $%.2f)", title[:40], outcome, size, cur_price)
            redeem_position(condition_id)
            time.sleep(2)
            actual = token_balance_onchain(token_id) if token_id else -1
            if actual == 0:
                pnl = round(size * 1.0 - size * float(ap.get("avgPrice", BID_PRICE)), 2)
                closed.append({
                    "token_id": token_id, "condition_id": condition_id,
                    "side": outcome, "asset": slug.split("-")[0] if slug else "?",
                    "title": title, "size": size, "cost": round(size * float(ap.get("avgPrice", BID_PRICE)), 2),
                    "exit_type": "won" if cur_price >= 0.99 else "reconciled",
                    "exit_price": cur_price, "pnl": pnl,
                    "closed_at": datetime.now(timezone.utc).isoformat(),
                    "source": "data_api_reconcile",
                })
                stats["pnl"] += pnl
                stats["wins"] += 1
                log.info("RECONCILE REDEEMED: %s %s | P&L $%+.2f", title[:40], outcome, pnl)
                if token_id in tracked_tokens:
                    positions[:] = [p for p in positions if p["token_id"] != token_id]
                changed = True
                continue

        if token_id and token_id not in tracked_tokens and size > 0:
            asset_name = slug.split("-")[0] if slug else "?"
            end_ts_str = ap.get("endDate", "")
            positions.append({
                "token_id": token_id, "buy_order_id": "adopted",
                "buy_price": float(ap.get("avgPrice", BID_PRICE)),
                "size": int(size), "cost": round(size * float(ap.get("avgPrice", BID_PRICE)), 2),
                "side": outcome, "asset": asset_name, "title": title, "slug": slug,
                "market_id": "", "condition_id": condition_id,
                "tick_size": 0.01, "neg_risk": ap.get("negativeRisk", False),
                "end_ts": int(time.time()) + 900,
                "sell_order_id": None, "sell_price": None, "status": "held",
                "placed_at": datetime.now(timezone.utc).isoformat(),
                "source": "data_api_adopted",
            })
            log.info("RECONCILE ADOPTED: %s %s — %.0f tokens (was untracked)", title[:40], outcome, size)
            changed = True

    if changed:
        save_json(POSITIONS_FILE, positions)
        save_json(CLOSED_FILE, closed[-500:])

# ── Order book helpers ──

def get_book(token_id):
    try:
        book = clob.get_order_book(token_id)
        bids = getattr(book, "bids", [])
        asks = getattr(book, "asks", [])
        return {"best_bid": float(bids[-1].price) if bids else 0, "best_ask": float(asks[0].price) if asks else 0}
    except Exception: return {"best_bid": 0, "best_ask": 0}

def place_gtc_buy(token_id, price, size, tick, neg_risk):
    try:
        args = OrderArgs(token_id=token_id, price=round(price, 2), size=int(size), side=BUY)
        signed = clob.create_order(args, options=CreateOrderOptions(tick_size=str(tick), neg_risk=neg_risk))
        r = clob.post_order(signed, OrderType.GTC)
        return r.get("orderID", "")
    except Exception as e:
        log.error("GTC buy fail: %s", e)
        return ""

def fak_sell(token_id, price, size, tick, neg_risk):
    try:
        args = OrderArgs(token_id=token_id, price=round(price, 2), size=int(size), side=SELL)
        signed = clob.create_order(args, options=CreateOrderOptions(tick_size=str(tick), neg_risk=neg_risk))
        r = clob.post_order(signed, OrderType.FAK)
        return r.get("orderID", "")
    except Exception as e:
        log.error("FAK sell fail: %s", e)
        return ""

def cancel_order(order_id):
    try: clob.cancel(order_id); return True
    except Exception: return False

def order_status(order_id):
    try:
        o = clob.get_order(order_id)
        if o:
            s = o.get("status", "")
            if s in ("MATCHED", "FILLED"): return "FILLED"
            if s in ("INVALID", "CANCELLED"): return "CANCELLED"
            return s
    except Exception: pass
    return "UNKNOWN"

def get_market_winner(market_id):
    try:
        r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=5)
        mdata = r.json()
        winner = mdata.get("winnerOutcome")
        if winner:
            return winner
        outcomes = mdata.get("outcomes", [])
        prices = mdata.get("outcomePrices", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        for i, price in enumerate(prices):
            if float(price) >= 0.99 and i < len(outcomes):
                return outcomes[i]
    except Exception:
        pass
    return None

# ── Position lifecycle ──

def check_and_close_position(p, exit_reason):
    actual = token_balance(p["token_id"])
    if actual > 0:
        p["size"] = actual
        p["status"] = "held"
        log.info("ACTUALLY FILLED %s %s: %d @ $%.2f (was %s)", p["asset"].upper(), p["side"], actual, p["buy_price"], exit_reason)
        return False
    st = order_status(p["buy_order_id"])
    if st == "FILLED":
        actual2 = token_balance_onchain(p["token_id"])
        if actual2 > 0:
            p["size"] = actual2
            p["status"] = "held"
            log.info("ORDER FILLED %s %s (on-chain %d)", p["asset"].upper(), p["side"], actual2)
            return False
        p["status"] = "held"
        p["size"] = int(p.get("cost", BID_AMOUNT) / p.get("buy_price", BID_PRICE))
        log.info("ORDER FILLED %s %s (CLOB=filled, keeping held)", p["asset"].upper(), p["side"])
        return False
    if st == "UNKNOWN":
        log.warning("STATUS UNKNOWN %s %s, keeping position", p["asset"].upper(), p["side"])
        return False
    cancel_order(p["buy_order_id"])
    time.sleep(1)
    recheck = token_balance_onchain(p["token_id"])
    if recheck > 0:
        p["size"] = recheck
        p["status"] = "held"
        log.info("POST-CANCEL RECOVERY %s %s: %d tokens on-chain", p["asset"].upper(), p["side"], recheck)
        return False
    p["status"] = "done"
    p["exit_type"] = exit_reason
    p["pnl"] = 0
    p["exit_price"] = 0
    p["closed_at"] = datetime.now(timezone.utc).isoformat()
    closed.append(p)
    log.info("%s %s %s (confirmed 0 on-chain)", exit_reason.upper(), p["asset"].upper(), p["side"])
    return True

# ── Redemption (gasless via Builder relayer when available, else direct tx) ──

def _redeem_via_relayer(condition_id, token_id=None):
    """Gasless redeem through Polymarket Builder relayer."""
    try:
        from py_builder_relayer_client.models import SafeTransaction, OperationType
        # All scalper markets are neg_risk — use NegRiskAdapter
        bal = ctf_contract.functions.balanceOf(w3_account.address, int(token_id)).call() if token_id else 0
        amounts = [bal, 0]  # try as Yes outcome
        redeem_data = neg_risk_adapter.encode_abi(
            abi_element_identifier="redeemPositions",
            args=[Web3.to_bytes(hexstr=condition_id), amounts]
        )
        tx = SafeTransaction(
            to=NEG_RISK_ADAPTER,
            operation=OperationType.Call,
            data=redeem_data,
            value="0",
        )
        response = relay_client.execute([tx], f"Redeem {condition_id[:16]}")
        result = response.wait()
        if result:
            log.info("REDEEMED (gasless) condition %s...", condition_id[:16])
            return True
        log.error("RELAYER REDEEM FAILED %s...", condition_id[:16])
        return False
    except Exception as e:
        log.error("RELAYER REDEEM ERROR: %s — falling back to direct tx", e)
        return _redeem_direct(condition_id)

def _redeem_direct(condition_id, token_id=None):
    """Direct on-chain redeem (EOA pays gas)."""
    try:
        gas_price = w3.eth.gas_price
        nonce = w3.eth.get_transaction_count(w3_account.address)
        # All scalper markets are neg_risk — use NegRiskAdapter
        bal = ctf_contract.functions.balanceOf(w3_account.address, int(token_id)).call() if token_id else 0
        amounts = [bal, 0]  # try as Yes outcome
        tx = neg_risk_adapter.functions.redeemPositions(
            Web3.to_bytes(hexstr=condition_id),
            amounts,
        ).build_transaction({
            "from": w3_account.address, "nonce": nonce, "gas": 400_000,
            "maxFeePerGas": int(gas_price * 1.5),
            "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        })
        signed = w3_account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        if receipt.status == 1:
            log.info("REDEEMED (direct) condition %s...", condition_id[:16])
            return True
        log.error("REDEEM REVERTED %s...", condition_id[:16])
        return False
    except Exception as e:
        log.error("REDEEM ERROR: %s", e)
        return False

def redeem_position(condition_id, token_id=None):
    if relay_client:
        return _redeem_via_relayer(condition_id, token_id=token_id)
    return _redeem_direct(condition_id, token_id=token_id)

# ── Order placement ──

def place_bids(market):
    asset = market["asset"].upper()
    tick = market["tick_size"]
    neg = market["neg_risk"]
    size = int(BID_AMOUNT / BID_PRICE)
    for side, token_key in [("Up", "up_token"), ("Down", "down_token")]:
        token_id = market[token_key]
        if any(p["token_id"] == token_id and p["status"] in ("pending", "held") for p in positions):
            continue
        bal = usdc_balance()
        if bal < BID_AMOUNT:
            log.warning("SKIP %s %s: balance $%.2f < $%.2f", asset, side, bal, BID_AMOUNT)
            continue
        oid = place_gtc_buy(token_id, BID_PRICE, size, tick, neg)
        if oid:
            positions.append({
                "token_id": token_id, "buy_order_id": oid, "buy_price": BID_PRICE,
                "size": size, "cost": round(BID_PRICE * size, 2), "side": side,
                "asset": market["asset"], "title": market["title"], "slug": market["slug"],
                "market_id": market["market_id"], "condition_id": market["condition_id"],
                "tick_size": tick, "neg_risk": neg, "end_ts": market["end_ts"],
                "sell_order_id": None, "sell_price": None, "status": "pending",
                "placed_at": datetime.now(timezone.utc).isoformat(),
            })
            log.info("BID %s %s: %d @ $%.2f ($%.2f) [ends %d]", asset, side, size, BID_PRICE, BID_AMOUNT, market["end_ts"])
    save_json(POSITIONS_FILE, positions)

def cancel_stale_bids():
    now = int(time.time())
    changed = False
    for p in list(positions):
        if p["status"] == "pending" and now > p["end_ts"] - 30:
            check_and_close_position(p, "expired")
            changed = True
    if changed:
        positions[:] = [p for p in positions if p["status"] != "done"]
        save_json(POSITIONS_FILE, positions); save_json(CLOSED_FILE, closed[-500:])

def manage():
    now = int(time.time())
    changed = False
    redeemed_cids = set()
    for p in list(positions):
        if p["status"] == "done":
            continue

        if p["status"] == "pending":
            actual = token_balance(p["token_id"])
            if actual > 0:
                p["size"] = actual
                p["status"] = "held"
                log.info("FILLED %s %s: %d @ $%.2f", p["asset"].upper(), p["side"], actual, p["buy_price"])
                changed = True
                continue
            st = order_status(p["buy_order_id"])
            if st == "FILLED":
                actual2 = token_balance_onchain(p["token_id"])
                if actual2 > 0:
                    p["size"] = actual2
                    p["status"] = "held"
                    log.info("FILLED %s %s: %d @ $%.2f (on-chain)", p["asset"].upper(), p["side"], actual2, p["buy_price"])
                    changed = True
                else:
                    p["status"] = "held"
                    p["size"] = int(p.get("cost", BID_AMOUNT) / p.get("buy_price", BID_PRICE))
                    log.info("FILLED %s %s (CLOB=filled, keeping held)", p["asset"].upper(), p["side"])
                    changed = True
            elif st == "CANCELLED":
                actual3 = token_balance_onchain(p["token_id"])
                if actual3 > 0:
                    p["size"] = actual3
                    p["status"] = "held"
                    log.info("CANCEL-BUT-FILLED %s %s: %d on-chain", p["asset"].upper(), p["side"], actual3)
                    changed = True
                elif actual3 == -1:
                    log.warning("CANCEL check RPC fail %s %s, keeping", p["asset"].upper(), p["side"])
                else:
                    p["status"] = "done"
                    p["exit_type"] = "cancelled"
                    p["pnl"] = 0
                    p["exit_price"] = 0
                    p["closed_at"] = datetime.now(timezone.utc).isoformat()
                    closed.append(p)
                    log.info("CANCELLED %s %s (confirmed 0 on-chain)", p["asset"].upper(), p["side"])
                    changed = True

        if p["status"] == "held" and now > p["end_ts"] + 60:
            actual = token_balance_onchain(p["token_id"])
            if actual == -1:
                log.warning("RPC fail %s %s, skip cycle", p["asset"].upper(), p["side"])
                continue
            if actual > 0:
                cid = p.get("condition_id", "")
                if cid and cid not in redeemed_cids:
                    redeem_position(cid)
                    redeemed_cids.add(cid)
                    time.sleep(2)
                    actual = token_balance_onchain(p["token_id"])
                    if actual is None or actual > 0:
                        log.info("REDEEM sent, tokens remain %s %s (%s), retry next cycle", p["asset"].upper(), p["side"], actual)
                        continue
            if actual == 0:
                p["status"] = "done"
                p["closed_at"] = datetime.now(timezone.utc).isoformat()
                winner = get_market_winner(p["market_id"])
                if winner == p["side"]:
                    p["exit_type"] = "won"
                    p["exit_price"] = 1.0
                    p["pnl"] = round(p["size"] * 1.0 - p["cost"], 2)
                elif winner:
                    p["exit_type"] = "lost"
                    p["exit_price"] = 0.0
                    p["pnl"] = round(-p["cost"], 2)
                else:
                    p["exit_type"] = "resolved"
                    p["exit_price"] = 0
                    p["pnl"] = round(-p["cost"], 2)
                closed.append(p)
                stats["pnl"] += p["pnl"]
                if p["exit_type"] == "won":
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1
                log.info("RESOLVED %s %s: %s | P&L $%.2f", p["asset"].upper(), p["side"], p["exit_type"], p["pnl"])
                changed = True
    if changed:
        positions[:] = [p for p in positions if p["status"] != "done"]
        save_json(POSITIONS_FILE, positions); save_json(CLOSED_FILE, closed[-500:])

def compute_trade_pnl():
    total = stats["pnl"]
    now = int(time.time())
    for p in positions:
        if p["status"] == "held" and now > p.get("end_ts", 0):
            winner = get_market_winner(p.get("market_id", ""))
            if winner == p["side"]:
                total += round(p["size"] * 1.0 - p["cost"], 2)
            elif winner:
                total += round(-p["cost"], 2)
    return round(total, 2)

# ── Dashboard & API ──

@flask_app.route("/")
def dash():
    with open("/app/dashboard.html") as f:
        resp = Response(f.read(), content_type="text/html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

@flask_app.route("/api/status")
def api_status():
    pos_data = []
    for p in positions:
        d = dict(p)
        d["bid"] = cache["bids"].get(p.get("token_id"))
        pos_data.append(d)
    trade_pnl = compute_trade_pnl()
    open_cost = sum(p.get("cost", 0) for p in positions if p["status"] in ("held", "pending"))
    portfolio_value = data_api_value()
    return jsonify({
        "bal": cache["bal"],
        "paused": bot_paused,
        "pos": pos_data,
        "closed": closed[-50:],
        "stats": {
            "wins": stats["wins"],
            "losses": stats["losses"],
            "pnl": stats["pnl"],
            "trade_pnl": trade_pnl,
            "open_cost": open_cost,
            "portfolio_value": portfolio_value,
            "builder_relayer": relay_client is not None,
        },
        "timezone": "UTC",
    })

@flask_app.route("/api/pause", methods=["POST"])
def api_pause():
    global bot_paused
    bot_paused = True
    log.info("BOT PAUSED by user")
    return jsonify({"success": True, "paused": True})

@flask_app.route("/api/resume", methods=["POST"])
def api_resume():
    global bot_paused
    bot_paused = False
    log.info("BOT RESUMED by user")
    return jsonify({"success": True, "paused": False})

@flask_app.route("/api/reconcile", methods=["POST"])
def api_reconcile():
    """Manual trigger for Data API reconciliation."""
    cache["last_reconcile"] = 0
    reconcile_positions()
    return jsonify({"msg": "Reconciliation complete", "positions": len(positions)})

@flask_app.route("/api/sell", methods=["POST"])
def api_sell():
    tid = flask_request.get_json().get("token_id", "")
    p = next((x for x in positions if x["token_id"] == tid), None)
    if not p:
        return jsonify({"err": "Not found"})
    if p["status"] == "pending":
        if not check_and_close_position(p, "cancelled"):
            save_json(POSITIONS_FILE, positions)
            return jsonify({"msg": "Bid was actually filled — now held"})
        positions[:] = [x for x in positions if x["status"] != "done"]
        save_json(POSITIONS_FILE, positions)
        return jsonify({"msg": "Bid cancelled"})
    actual = token_balance_onchain(tid)
    if actual < 1:
        return jsonify({"err": "No shares on-chain"})
    book = get_book(tid)
    best = book["best_bid"]
    if best < 0.01:
        return jsonify({"err": "No bids in book"})
    oid = fak_sell(tid, best, actual, p["tick_size"], p["neg_risk"])
    if oid:
        p["status"] = "done"
        p["exit_type"] = "manual_sell"
        p["exit_price"] = best
        p["pnl"] = round(best * actual - p["cost"], 2)
        p["closed_at"] = datetime.now(timezone.utc).isoformat()
        stats["pnl"] += p["pnl"]
        if p["pnl"] >= 0:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
        closed.append(p)
        positions[:] = [x for x in positions if x["status"] != "done"]
        save_json(POSITIONS_FILE, positions)
        return jsonify({"msg": "Sold %d @ $%.2f | P&L $%.2f" % (actual, best, p["pnl"])})
    return jsonify({"err": "Sell failed"})

@flask_app.route("/api/cancel", methods=["POST"])
def api_cancel():
    tid = flask_request.get_json().get("token_id", "")
    p = next((x for x in positions if x["token_id"] == tid), None)
    if not p:
        return jsonify({"err": "Not found"})
    if p["status"] == "pending":
        if not check_and_close_position(p, "cancelled"):
            save_json(POSITIONS_FILE, positions)
            return jsonify({"msg": "Bid was actually filled — now held"})
        positions[:] = [x for x in positions if x["status"] != "done"]
        save_json(POSITIONS_FILE, positions)
        return jsonify({"msg": "Bid cancelled"})
    return jsonify({"err": "Not a pending bid"})

# ── Builder relayer init ──

def init_builder_relayer():
    """Initialize Builder relayer for gasless transactions (optional)."""
    global relay_client
    if not (BUILDER_KEY and BUILDER_SECRET and BUILDER_PASSPHRASE):
        log.info("Builder relayer: disabled (no POLY_BUILDER_* env vars)")
        return
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
        builder_config = BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=BUILDER_KEY, secret=BUILDER_SECRET, passphrase=BUILDER_PASSPHRASE,
            )
        )
        relay_client = RelayClient(
            "https://relayer-v2.polymarket.com", 137, PRIVATE_KEY, builder_config
        )
        # Deploy Safe wallet if not yet deployed
        safe_addr = relay_client.get_expected_safe()
        if not relay_client.get_deployed(safe_addr):
            log.info("Deploying Safe wallet %s via relayer...", safe_addr)
            resp = relay_client.deploy()
            resp.wait()
            log.info("Safe wallet deployed: %s", safe_addr)
        else:
            log.info("Safe wallet already deployed: %s", safe_addr)
        log.info("Builder relayer: ENABLED (gasless redemptions)")
    except ImportError as e:
        log.warning("Builder relayer: import error — %s, using direct tx", e)
    except Exception as e:
        log.warning("Builder relayer init failed: %s — using direct tx", e)

# ── Main loop ──

def run():
    global clob, w3, w3_account, ctf_contract, neg_risk_adapter, positions, closed
    log.info("Scalper v9 | $%.0f @ $%.2f | %s | Data API + Builder relayer", BID_AMOUNT, BID_PRICE, "+".join(ASSETS))
    clob = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=POLYGON)
    creds = clob.create_or_derive_api_creds()
    clob.set_api_creds(creds)
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    w3_account = w3.eth.account.from_key(PRIVATE_KEY)
    ctf_contract = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    neg_risk_adapter = w3.eth.contract(
        address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
        abi=[{"inputs": [{"name": "_conditionId", "type": "bytes32"}, {"name": "_amounts", "type": "uint256[]"}],
              "name": "redeemPositions", "outputs": [], "stateMutability": "nonpayable", "type": "function"}])
    # Ensure CTF approval for NegRiskAdapter
    try:
        _approved = ctf_contract.functions.isApprovedForAll(w3_account.address, NEG_RISK_ADAPTER).call()
        if not _approved:
            log.info("Setting CTF approval for NegRiskAdapter...")
            _n = w3.eth.get_transaction_count(w3_account.address)
            _atx = ctf_contract.functions.setApprovalForAll(NEG_RISK_ADAPTER, True).build_transaction({
                "from": w3_account.address, "nonce": _n, "gas": 100_000,
                "maxFeePerGas": int(w3.eth.gas_price * 1.5),
                "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),})
            _s = w3_account.sign_transaction(_atx)
            w3.eth.send_raw_transaction(_s.raw_transaction)
            log.info("CTF approved for NegRiskAdapter")
    except Exception as e:
        log.warning("Approval check failed: %s", e)
    init_builder_relayer()
    log.info("CLOB+Web3 ready | USDC: $%.2f | wallet: %s", usdc_balance(), w3_account.address)
    positions = load_json(POSITIONS_FILE, [])
    closed = load_json(CLOSED_FILE, [])
    log.info("Restored %d pos, %d closed", len(positions), len(closed))
    for c in closed:
        if c.get("exit_type") == "won":
            stats["wins"] += 1
        elif c.get("exit_type") == "lost":
            stats["losses"] += 1
        stats["pnl"] += c.get("pnl", 0)
    log.info("Stats: %d W / %d L | trade P&L $%+.2f", stats["wins"], stats["losses"], stats["pnl"])

    reconcile_positions()
    log.info("Initial reconciliation done — portfolio value: $%.2f", data_api_value())

    while True:
        try:
            bal = usdc_balance()
            cache["bal"] = bal
            for p in positions:
                try:
                    cache["bids"][p["token_id"]] = get_book(p["token_id"])["best_bid"]
                except Exception:
                    pass
            cancel_stale_bids()
            manage()
            reconcile_positions()
            markets = find_current_markets()
            now_ts = int(time.time())
            tl = ((now_ts // 900) * 900 + 900) - now_ts
            pnl = compute_trade_pnl()
            paused_tag = " PAUSED" if bot_paused else ""
            log.info("-- tick -- %d pos | $%.2f | %d mkts | P&L $%+.2f | %dW/%dL | window %dm%ds%s --",
                     len(positions), bal, len(markets), pnl, stats["wins"], stats["losses"], tl // 60, tl % 60, paused_tag)
            if not bot_paused:
                for mkt in markets:
                    place_bids(mkt)
            else:
                log.info("Paused — skipping bid placement")
        except Exception as e:
            log.error("Loop error: %s", e)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=PORT, debug=False), daemon=True).start()
    run()
