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

	# Optimizer quality filters (can be overridden via env)
	# Minimum average realized ROI per 1k votes (USD) for a gauge to be included.
	# Gauges with no history pass through (sentinel 999 used when field absent).
	ROI_FLOOR_PER_1K = float(os.getenv("ROI_FLOOR_PER_1K", "0.25") or "0.25")
	# Gauges with current_votes above this threshold are capped at
	# HIGH_COMPETITION_VOTE_CAP_RATIO of total voting_power to force diversification.
	HIGH_COMPETITION_VOTES_THRESHOLD = int(
		os.getenv("HIGH_COMPETITION_VOTES_THRESHOLD", "5000000") or "5000000"
	)
	HIGH_COMPETITION_VOTE_CAP_RATIO = float(
		os.getenv("HIGH_COMPETITION_VOTE_CAP_RATIO", "0.05") or "0.05"
	)

	# Late-vote risk adjustment: inflate current_votes before optimization to
	# discount pools that historically attract large last-minute vote injections.
	# Multipliers are derived from historical avg_late_pct (1 + avg_late/100),
	# capped at 1.5.  Source: _diagnostic_late_vote_analysis.py (10-31 epoch runs).
	# Keys are GAUGE addresses (not pool addresses).
	LATE_VOTE_RISK_MULTIPLIERS: dict = {
		# Critical — avg dilution > 20%
		"0x0a2918e8034737576fc9877c741f628876dcf491": 1.5,   # pool=0x761a383f avg late +52.7%, avg dil -24.1%
		"0x1ff58d66eaefab1f4da18ce67cc23b33e3cb2447": 1.4,   # pool=0xafa8740b avg late +43.0%, avg dil -22.2%
		# High — avg dilution 10-20%
		"0x11eda610e510d3c10da15eb121dc520da29e9e69": 1.3,   # pool=0x8323ba10 avg late +27.9%, avg dil -10.5%
		"0x5182f20434aed147f1bf54ca111a2ad844e84d26": 1.2,   # pool=0xb55fab1b avg late +22.9%, avg dil -16.0%
		"0x50b33b06d77dc6ceb02527901dcecab50ab654ce": 1.2,   # pool=0xdc2b01bc avg late +22.3%, avg dil -14.2%
		"0x6fbae41d4af145a22278819bcebe0aad012bd359": 1.2,   # pool=0xa4b2401d avg late +22.6%, avg dil -13.3%
		"0x5ce84085ed97c69d20b506f10080666e781e5d62": 1.2,   # pool=0x70ec9203 avg late +19.9%, avg dil -12.8%
		"0xd5a8c8f2235751136772f6436d1b87f00d603e2b": 1.2,   # pool=0xd68f485e avg late +18.6%, avg dil  -9.1%
		"0x6b16d036a8575279dfb48e6032195f8edfc99d88": 1.15,  # pool=0x1ad9e615 avg late +15.7%, avg dil -12.0%
		"0x2a3821aa3271a149c80ad38b77c3d13fdfa43799": 1.15,  # pool=0xf3dfab70 avg late +16.3%, avg dil -11.0%
		"0x3d0eab5ad5a440af7a53c433552bcdf3f2711297": 1.15,  # pool=0x034196ae avg late +16.1%, avg dil -10.7%
		"0xa67f987038fb80c9ac34d00904311c7cb9ca06da": 1.15,  # pool=0xa13e3f67 avg late +14.2%, avg dil -10.7%
		"0x2587773bf497fa36bd6493319b7ed22d093b121e": 1.15,  # pool=0xd6989c6f avg late +16.0%, avg dil -10.6%
		"0x596ee96871816f34b1ddee0b4a865a5c5763392f": 1.15,  # pool=0xd604cf30 avg late +14.4%, avg dil  -8.1%
		"0xf7d1363a45061b4c0863399d1ad7a7fa4149f56f": 1.15,  # pool=0x02ab4a24 avg late +15.0%, avg dil  -8.0%
		# Medium — avg dilution 8-10%
		"0x632f2d41ba9e6e80035d578ddd48b019e4403f86": 1.1,   # pool=0x19ff3505 avg late +12.0%, avg dil  -8.8%
		"0xcaa5c1fd6c04fc8bd0ca63277aa7def823c8e1ca": 1.1,   # pool=0x96f4124e avg late +12.0%, avg dil  -8.8%
		"0xbd17b39020965a01242c8f5ea427065098edab09": 1.1,   # pool=0xcfc45c18 avg late +10.8%, avg dil  -8.4%
		"0x1cfb4445af11d290846fb3e088da354071527310": 1.1,   # pool=0xaa554b31 avg late +13.1%, avg dil  -9.0%
	}

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
