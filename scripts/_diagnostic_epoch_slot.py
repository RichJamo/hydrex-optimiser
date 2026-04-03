"""
Diagnostic: verify which bribe epoch slot is being read, and whether it was
frozen before our live T-120s snapshot, by checking minter.active_period().
"""
import os
import sqlite3
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

w3 = Web3(Web3.HTTPProvider(os.getenv("RPC_URL")))

MINTER_ADDRESS = os.getenv("MINTER_ADDRESS", "")
WEEK = 604800

VOTE_EPOCH = 1774483200         # epoch our votes targeted
BOUNDARY_TS = 1775088000        # April 2 boundary  (vote_epoch + WEEK)
LIVE_BLOCK = 44149268           # Phase 1 snapshot block  (T-120s)
BOUNDARY_BLOCK = 44149331       # actual flip block
PREV_BOUNDARY_BLOCK = 43846933  # March 26 flip block

MINTER_ABI = [
    {"inputs": [], "name": "active_period", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "period",         "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

if not MINTER_ADDRESS:
    print("MINTER_ADDRESS not set in .env")
    exit(1)

minter = w3.eth.contract(address=Web3.to_checksum_address(MINTER_ADDRESS), abi=MINTER_ABI)

for label, block in [
    ("March 26 flip block -1", PREV_BOUNDARY_BLOCK - 1),
    ("March 26 flip block   ", PREV_BOUNDARY_BLOCK),
    ("Live snapshot block T-120s", LIVE_BLOCK),
    ("Boundary block T+0         ", BOUNDARY_BLOCK),
]:
    ap = minter.functions.active_period().call(block_identifier=block)
    write_key = ap + WEEK
    print(f"{label} (block {block}): active_period={ap}  write_key={write_key}  vote_epoch_slot_frozen={ap >= VOTE_EPOCH}")

# Also check: are the raw rewardsPerEpoch identical at live block vs boundary block
# for the top pool, to confirm whether the slot was frozen or still accumulating
TOP_POOL_BRIBE = ""  # will be loaded from DB
TOP_POOL_TOKEN = ""

conn = sqlite3.connect("data/db/data.db")
row = conn.execute(
    """SELECT s.bribe_contract, s.reward_token
       FROM live_reward_token_samples s
       WHERE s.snapshot_ts=1775087889
       ORDER BY s.rewards_normalized DESC LIMIT 1"""
).fetchone()
conn.close()

if row:
    TOP_POOL_BRIBE, TOP_POOL_TOKEN = row
    print(f"\nTop bribe/token: {TOP_POOL_BRIBE} / {TOP_POOL_TOKEN}")
    BRIBE_ABI = [
        {"inputs": [{"type": "address"}, {"type": "uint256"}],
         "name": "rewardData",
         "outputs": [{"name": "periodFinish", "type": "uint256"},
                     {"name": "rewardsPerEpoch", "type": "uint256"},
                     {"name": "lastUpdateTime", "type": "uint256"}],
         "stateMutability": "view", "type": "function"}
    ]
    bribe = w3.eth.contract(address=Web3.to_checksum_address(TOP_POOL_BRIBE), abi=BRIBE_ABI)
    for label, block in [("live block", LIVE_BLOCK), ("boundary block", BOUNDARY_BLOCK)]:
        rd = bribe.functions.rewardData(Web3.to_checksum_address(TOP_POOL_TOKEN), VOTE_EPOCH).call(block_identifier=block)
        print(f"  {label}: periodFinish={rd[0]}  rewardsPerEpoch={rd[1]}  lastUpdateTime={rd[2]}")
