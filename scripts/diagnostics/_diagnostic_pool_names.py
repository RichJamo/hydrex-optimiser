"""
_diagnostic_pool_names.py

Resolves human-readable names for all pool addresses that appeared in the
multi-epoch dilution analysis, using token0()/token1() on-chain calls.

Prints a complete address->name map.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from web3 import Web3

RPC_URL = os.getenv("RPC_URL", "")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

POOL_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]
ERC20_ABI = [
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"}
]

sym_cache = {}


def sym(addr):
    k = addr.lower()
    if k in sym_cache:
        return sym_cache[k]
    try:
        s = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI).functions.symbol().call()
        sym_cache[k] = s.strip() if s else addr[:6]
    except Exception:
        sym_cache[k] = addr[:6] + ".."
    return sym_cache[k]


def resolve(pool):
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(pool), abi=POOL_ABI)
        t0 = c.functions.token0().call()
        t1 = c.functions.token1().call()
        return f"{sym(t0)}-{sym(t1)}"
    except Exception:
        return pool[:10] + "..."


POOLS = [
    "0x18156ace9940645ebf3602e2b320a77131a70ad1",
    "0xcecf4d16114e601276ba7e8c39a309fbfc605f0e",
    "0x2df4af05f8c4aff0d3fbfc327595dbb7fc6498bf",
    "0x30816a9e6572407a83ba5fd18e145d9dd81540f5",
    "0xb3f0828eb3375b609b49e9fb959472a29cd6e49a",
    "0xf19787f048b3401546aa7a979afa79d555c114dd",
    "0x89f29dd355d74e57389374a2aa5f9518a1e497ac",
    "0x5984bc5ca90388aac221147ca9da419a2a458a9f",
    "0x763adcf71cb195184088a26be01662119e303f5f",
    "0x0e10d9b9873e798e59ecace18eb9c6e220dc111c",
    "0x29262772a1a99f180ac2e70093954cf4adf3cd85",
    "0x84994103403c715bace3bfb42f074f50e5432d55",
    "0xd4610403f0a93611ca76a5847743533268eff793",
    "0x4506b0e9a7b0b06185ff317cef77bc5454b045c9",
    "0xbeef050a7485865a7a8d8ca0cc5f7536b7a3443e",
    "0x721bcd563603a559616dc9fb886a1732186995a4",
    "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29",
    "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2",
    "0x605abd1873737ca9a9ec1cfa52cdfc8ef62c2e1d",
    "0x3f9b863ef4b295d6ba370215bcca3785fcc44f44",
    "0x15951b35d7a8ea6b6ef5718eab2fcdd3ad072451",
    "0x52fde427c2483ba0749140c0b43cfce73ba50d20",
    "0x769bef1459ffc8ccc50b3e014d1ffbaf4c50cb39",
    "0xc874416e1fdee2dc291fe1a4b7b91fb4c5875fc2",
    "0xb20f018dde5a6fe7f93c31da05a5da9efbc52772",
    "0xab5d32ff95b58bd5e77de08738a391973ee88c81",
    "0x9e7e8997db3d2e3ae84e237fd49a4d0c878604fa",
    "0x95fe59cdcbbc619ace31cf1b69691692e77401f8",
    "0xbfdee8d6c37e3173ef808f884f99c0ba35892aa7",
    "0xcc037919bc28498c49b1c367d51ecd29b4b982c4",
    "0xb55fab1b4f8a2f52cca0c21ca3c4c20060454086",
    "0xca260c4633a3834caa9d70e5ea2d78d9290890c6",
    "0xc3f617c8a3da6d286c8f97af946a8f293632c852",
    "0xdf7d47cb2a669852f9e9c3022776ec66edb217ce",
    "0xa528c2462fa74eb1a2335424d69d721e4aed1367",
    "0xbce15e3e9799c9296c631b8fca65386d9554f49a",
    "0xcd280042f629fa2384179911f9cb1f932b9f26c8",
    "0x680581725840958141bb328666d8fc185ac4fa49",
    "0x02ab4a243c229550f6babcfdb106f19b35124df6",
    "0x8772c46a7d10d7189d21eb635a9d271c83f66263",
    "0x8c8063a449eb9f020449fec4d9f0bca845188929",
    "0xef96ec76eeb36584fc4922e9fa268e0780170f33",
    "0xee58348059c9ad6ac345be79c399da0c200627ed",
    "0x4f0a58b2f561cd23e3059e76526125c85e281821",
    "0x0ba69825c4c033e72309f6ac0bde0023b15cc97c",
    "0x4c1aeda9b43efcf1da1d1755b18802aabe90f61e",
    "0xbeefe94c8ad530842bfe7d8b397938ffc1cb83b2",
]

for p in POOLS:
    name = resolve(p)
    print(f"{p}  {name}")
