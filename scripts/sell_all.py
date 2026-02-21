from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, CreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import SELL
from py_clob_client.constants import POLYGON
import os, json, time

client = ClobClient("https://clob.polymarket.com", key=os.getenv("PRIVATE_KEY"), chain_id=POLYGON)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f"USDC before: ${int(bal.get('balance', 0)) / 1e6:.2f}")

with open("/app/data/scalp_positions.json") as f:
    positions = json.load(f)
print(f"Positions: {len(positions)}")

for p in positions:
    tid = p["token_id"]
    b = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid))
    raw = int(b.get("balance", 0))
    shares = raw // 1000000
    if shares < 1:
        print(f"  SKIP no shares: {p['title'][:50]}")
        continue
    book = client.get_order_book(tid)
    bids = getattr(book, "bids", [])
    best_bid = float(bids[-1].price) if bids else 0
    if best_bid < 0.01:
        print(f"  SKIP no bid: {p['title'][:50]}")
        continue
    try:
        args = OrderArgs(token_id=tid, price=round(best_bid, 2), size=shares, side=SELL)
        opts = CreateOrderOptions(tick_size=str(p.get("tick_size", "0.01")), neg_risk=p.get("neg_risk", False))
        signed = client.create_order(args, options=opts)
        result = client.post_order(signed, OrderType.FAK)
        oid = result.get("orderID", "")
        pnl = round((best_bid - p["buy_price"]) * shares, 2)
        print(f"  SOLD {shares}@${best_bid:.2f} PnL=${pnl:+.2f} {p['title'][:50]}")
    except Exception as e:
        print(f"  ERR: {e} | {p['title'][:50]}")

time.sleep(2)
bal2 = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f"USDC after: ${int(bal2.get('balance', 0)) / 1e6:.2f}")
json.dump([], open("/app/data/scalp_positions.json", "w"))
print("Cleared")
