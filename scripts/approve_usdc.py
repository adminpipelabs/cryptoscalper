"""One-time: approve USDC.e for Polymarket exchange contracts on Polygon."""
import os
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

RPC = os.getenv("RPC_URL", "https://polygon-bor-rpc.publicnode.com")
PK = os.getenv("PRIVATE_KEY")

w3 = Web3(Web3.HTTPProvider(RPC))
acct = w3.eth.account.from_key(PK)
addr = acct.address

USDC = w3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
APPROVE_AMOUNT = 10**12

APPROVE_ABI = [
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

usdc = w3.eth.contract(address=USDC, abi=APPROVE_ABI)

EXCHANGES = {
    "CTF Exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "NegRisk Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "NegRisk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}

gas_price = w3.eth.gas_price
boosted = int(gas_price * 1.5)
print(f"Gas price: {gas_price / 1e9:.1f} gwei, using {boosted / 1e9:.1f} gwei")

nonce = w3.eth.get_transaction_count(addr, "pending")
print(f"Starting nonce: {nonce}")

for name, exchange in EXCHANGES.items():
    spender = w3.to_checksum_address(exchange)
    current = usdc.functions.allowance(addr, spender).call()
    print(f"\n{name}: allowance={current}")

    if current > 10**9:
        print("  Already approved")
        continue

    tx = usdc.functions.approve(spender, APPROVE_AMOUNT).build_transaction({
        "from": addr,
        "nonce": nonce,
        "gas": 100000,
        "gasPrice": boosted,
        "chainId": 137,
    })

    signed = w3.eth.account.sign_transaction(tx, PK)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    print(f"  Status: {'OK' if receipt.status == 1 else 'FAILED'} gas={receipt.gasUsed}")
    nonce += 1

print("\nFinal allowances:")
for name, exchange in EXCHANGES.items():
    spender = w3.to_checksum_address(exchange)
    allow = usdc.functions.allowance(addr, spender).call()
    print(f"  {name}: {allow}")
