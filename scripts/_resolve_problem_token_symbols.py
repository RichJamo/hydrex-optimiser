"""Resolve and print ERC20 symbols for routing problem tokens. One-shot helper."""
from web3 import Web3
from config import Config  # noqa: E402 (ensure sys.path includes repo root)

w3 = Web3(Web3.HTTPProvider(Config.RPC_URL, request_kwargs={"timeout": 30}))
ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    }
]

PROBLEM_TOKENS = [
    # (address, failure_category)
    ("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf", "no_quote_400"),
    ("0xa88594d404727625a9437c3f886c7643872296ae", "no_quote_400"),
    ("0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42", "no_quote_400"),
    ("0x22af33fe49fd1fa80c7149773dde5890d3c76f3b", "no_quote_400"),
    ("0x18bc5bcc660cf2b9ce3cd51a404afe1a0cbd3c22", "no_quote_400"),
    ("0xcbada732173e39521cdbe8bf59a6dc85a9fc7b8c", "no_quote_400"),
    ("0x138746adfa52909e5920def027f5a8dc1c7effb6", "no_quote_400"),
    ("0x102d758f688a4c1c5a80b116bd945d4455460282", "no_quote_400"),
    ("0xcb585250f852c6c6bf90434ab21a00f02833a4af", "no_quote_400"),
    ("0x311935cd80b76769bf2ecc9d8ab7635b2139cf82", "no_quote_400"),
    ("0xcb17c9db87b595717c857a08468793f5bab6445f", "no_quote_400"),
    ("0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452", "http_400_calldata"),
    ("0xa9f6d9eca1f803854a13cecad0f21d43e007db07", "zero_amount_out"),
    ("0xe3cf8dbcbdc9b220ddead0bd6342e245daff934d", "zero_amount_out"),
    # previous defer list
    ("0x5fc2843838e65eb0b5d33654628f446d54602791", "prev_defer"),
    ("0xc3679395bddfb080fed2e26a54ab224dc582c99a", "prev_defer"),
    ("0xfac77f01957ed1b3dd1cbea992199b8f85b6e886", "prev_defer"),
]

print(f"{'Category':<22}  {'Symbol':<12}  Address")
print("-" * 80)
for addr, category in PROBLEM_TOKENS:
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ABI)
        sym = contract.functions.symbol().call()
        if isinstance(sym, bytes):
            sym = sym.decode("utf-8").rstrip("\x00")
    except Exception as exc:
        sym = f"ERR:{str(exc)[:30]}"
    print(f"{category:<22}  {sym:<12}  {addr}")
