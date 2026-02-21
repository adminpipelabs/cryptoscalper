from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OpenOrderParams
from py_clob_client.constants import POLYGON
import os

client = ClobClient("https://clob.polymarket.com", key=os.getenv("PRIVATE_KEY"), chain_id=POLYGON)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

try:
    client.cancel_all()
    print("All orders cancelled")
except Exception as e:
    print(f"cancel_all: {e}")

bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f"USDC: ${int(bal.get('balance', 0)) / 1e6:.2f}")
