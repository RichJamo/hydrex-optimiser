"""Configuration compatibility layer and shared exports."""

import json
import os
import time
from pathlib import Path
from typing import Any, List, Optional

from dotenv import load_dotenv

from .settings import DATABASE_PATH, VOTER_ADDRESS, WEEK

load_dotenv()


def _load_abi(file_name: str) -> List[Any]:
	root = Path(__file__).resolve().parent.parent
	abi_path = root / file_name
	try:
		with abi_path.open("r", encoding="utf-8") as handle:
			return json.load(handle)
	except Exception:
		return []


class Config:
	RPC_URL = os.getenv("RPC_URL", "")
	RPC_TIMEOUT = int(os.getenv("RPC_TIMEOUT", "30") or "30")

	SUBGRAPH_URL = os.getenv("SUBGRAPH_URL", "")
	ANALYTICS_SUBGRAPH_URL = os.getenv("ANALYTICS_SUBGRAPH_URL", "")

	DATABASE_PATH = DATABASE_PATH
	VOTER_ADDRESS = VOTER_ADDRESS

	MY_ESCROW_ADDRESS = os.getenv("MY_ESCROW_ADDRESS", "")
	YOUR_VOTING_POWER = int(os.getenv("YOUR_VOTING_POWER", "0") or "0")

	COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
	PRICE_CACHE_TTL = int(os.getenv("PRICE_CACHE_TTL", "300") or "300")

	MIN_VOTE_ALLOCATION = int(os.getenv("MIN_VOTE_ALLOCATION", "1000") or "1000")
	MAX_GAUGES_TO_VOTE = int(os.getenv("MAX_GAUGES_TO_VOTE", "10") or "10")

	EPOCH_DURATION = WEEK

	@staticmethod
	def get_current_epoch_timestamp(now_ts: Optional[int] = None) -> int:
		now = int(now_ts if now_ts is not None else time.time())
		return (now // Config.EPOCH_DURATION) * Config.EPOCH_DURATION

	@staticmethod
	def is_in_safe_voting_window(now_ts: Optional[int] = None) -> bool:
		now = int(now_ts if now_ts is not None else time.time())
		epoch_start = Config.get_current_epoch_timestamp(now)
		saturday_1800_offset = (3 * 24 * 3600) + (18 * 3600)
		return now >= (epoch_start + saturday_1800_offset)


VOTER_ABI = _load_abi("voterv5_abi.json")
BRIBE_ABI = _load_abi("bribev2_abi.json")

__all__ = ["Config", "VOTER_ABI", "BRIBE_ABI"]
