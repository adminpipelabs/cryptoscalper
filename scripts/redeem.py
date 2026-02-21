import os, json, requests
from web3 import Web3
from dotenv import load_dotenv
load_dotenv()

CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ABI = [
    {"inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],
     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},
                {"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],
     "name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"},
]

w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
acct = w3.eth.account.from_key(os.environ["PRIVATE_KEY"])
ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=CTF_ABI)

print("Wallet:", acct.address)
print("MATIC:", w3.from_wei(w3.eth.get_balance(acct.address), "ether"))

with open("/app/data/scalp_positions.json") as f:
    positions = json.load(f)

print(f"Positions: {len(positions)}")

nonce = w3.eth.get_transaction_count(acct.address)
gas_price = w3.eth.gas_price

redeemed_cids = set()
for p in positions:
    cid = p.get("condition_id", "")
    tid = p["token_id"]
    if not cid or cid in redeemed_cids:
        print(f"  SKIP {p['asset']} {p['side']} (already redeemed this condition)")
        continue

    bal = ctf.functions.balanceOf(acct.address, int(tid)).call()
    shares = bal // 1_000_000
    print(f"{p['asset'].upper()} {p['side']}: {shares} shares | cid={cid[:16]}...")

    if bal == 0:
        print("  No tokens, skip")
        continue

    # Check if market is resolved
    r = requests.get(f"https://gamma-api.polymarket.com/markets/{p['market_id']}", timeout=5)
    mdata = r.json()
    print(f"  closed={mdata.get('closed')} winner={mdata.get('winnerOutcome')}")

    try:
        tx = ctf.functions.redeemPositions(
            Web3.to_checksum_address(USDC),
            b"\x00" * 32,
            Web3.to_bytes(hexstr=cid),
            [1, 2]
        ).build_transaction({
            "from": acct.address,
            "nonce": nonce,
            "gas": 300_000,
            "maxFeePerGas": int(gas_price * 1.5),
            "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        })
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  TX sent: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        print(f"  REDEEMED: {'OK' if receipt.status == 1 else 'FAILED'}")
        redeemed_cids.add(cid)
        nonce += 1
    except Exception as e:
        print(f"  REDEEM ERROR: {e}")
        nonce += 1

# Final balance
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
from py_clob_client.constants import POLYGON
c = ClobClient("https://clob.polymarket.com", key=os.environ["PRIVATE_KEY"], chain_id=POLYGON)
c.set_api_creds(c.create_or_derive_api_creds())
b = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f"\nFinal USDC: ${int(b.get('balance',0))/1e6:.2f}")
