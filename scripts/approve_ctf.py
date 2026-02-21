from web3 import Web3
import os
w3 = Web3(Web3.HTTPProvider(os.getenv("RPC_URL")))
acct = w3.eth.account.from_key(os.getenv("PRIVATE_KEY"))
addr = acct.address
CTF = w3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
ABI = [
  {"constant":False,"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"type":"function"},
  {"constant":True,"inputs":[{"name":"owner","type":"address"},{"name":"operator","type":"address"}],"name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],"type":"function"},
]
ctf = w3.eth.contract(address=CTF, abi=ABI)
exchanges = [
  ("CTF", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
  ("NegRisk", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
  ("Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
]
gas_price = w3.eth.gas_price
boosted = int(gas_price * 1.5)
nonce = w3.eth.get_transaction_count(addr, "pending")
pk = os.getenv("PRIVATE_KEY")
for name, ex in exchanges:
    op = w3.to_checksum_address(ex)
    ok = ctf.functions.isApprovedForAll(addr, op).call()
    print(f"{name}: approved={ok}")
    if ok:
        continue
    tx = ctf.functions.setApprovalForAll(op, True).build_transaction({
        "from": addr, "nonce": nonce, "gas": 100000,
        "gasPrice": boosted, "chainId": 137,
    })
    signed = w3.eth.account.sign_transaction(tx, pk)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  tx={h.hex()}")
    r = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    print(f"  status={r.status}")
    nonce += 1
print("Done")
