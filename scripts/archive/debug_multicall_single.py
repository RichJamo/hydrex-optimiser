#!/usr/bin/env python3
from dotenv import dotenv_values
from web3 import Web3
from multicall import Call, Multicall

BRIBE = "0xB69B1C48917cc055c76a93A748b5dAa6eFA39DEe"
TOKEN = "0x00000e7efa313F4E11Bfff432471eD9423AC6B30"
VOTE_EPOCH = 1770854400
BOUNDARY_BLOCK = 42334931


def run(sig: str):
    print(f"\n--- signature: {sig} ---")
    rpc = dotenv_values('.env').get('RPC_URL')
    w3 = Web3(Web3.HTTPProvider(rpc))

    call = Call(
        Web3.to_checksum_address(BRIBE),
        [sig, Web3.to_checksum_address(TOKEN), VOTE_EPOCH],
        [("k", lambda success, value: value if success else None)],
    )

    multi = Multicall([call], _w3=w3, block_id=BOUNDARY_BLOCK, require_success=False)
    out = multi()
    print("raw out:", out)
    print("value:", out.get("k"))
    print("value type:", type(out.get("k")))


if __name__ == "__main__":
    run('rewardData(address,uint256)(uint256,uint256,uint256)')
    run('rewardData(address,uint256)((uint256,uint256,uint256))')
