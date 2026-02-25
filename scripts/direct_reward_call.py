#!/usr/bin/env python3
import argparse
from dotenv import dotenv_values
from web3 import Web3

BRIBE_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "", "type": "address"},
            {"internalType": "uint256", "name": "", "type": "uint256"},
        ],
        "name": "rewardData",
        "outputs": [
            {"internalType": "uint256", "name": "periodFinish", "type": "uint256"},
            {"internalType": "uint256", "name": "rewardsPerEpoch", "type": "uint256"},
            {"internalType": "uint256", "name": "lastUpdateTime", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Single direct rewardData RPC call")
    parser.add_argument("--bribe", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--epoch-arg", type=int, required=True)
    parser.add_argument("--block", type=int, required=True)
    args = parser.parse_args()

    rpc = dotenv_values(".env").get("RPC_URL")
    w3 = Web3(Web3.HTTPProvider(rpc))

    contract = w3.eth.contract(address=Web3.to_checksum_address(args.bribe), abi=BRIBE_ABI)

    print(f"bribe={args.bribe}")
    print(f"token={args.token}")
    print(f"epoch_arg={args.epoch_arg}")
    print(f"block={args.block}")

    try:
        period_finish, rewards_per_epoch, last_update = contract.functions.rewardData(
            Web3.to_checksum_address(args.token),
            int(args.epoch_arg),
        ).call(block_identifier=int(args.block))

        print("result:")
        print(f"  periodFinish={int(period_finish)}")
        print(f"  rewardsPerEpoch={int(rewards_per_epoch)}")
        print(f"  lastUpdateTime={int(last_update)}")
    except Exception as exc:
        print(f"error: {exc}")


if __name__ == "__main__":
    main()
