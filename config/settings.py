"""
Configuration constants and settings for Hydrex Optimiser.

Centralizes all configuration including:
- Contract addresses
- RPC endpoints
- Database paths
- Call parameters
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ═══ Contract Addresses ═══
VOTER_ADDRESS = os.getenv("VOTER_ADDRESS", "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b")
VE_ADDRESS = os.getenv("VE_ADDRESS", "0x25B2ED7149fb8A05f6eF9407d9c8F878f59cd1e1")

# ═══ User Configuration ═══
YOUR_ADDRESS = os.getenv("YOUR_ADDRESS", "")
YOUR_TOKEN_ID = os.getenv("YOUR_TOKEN_ID", "")

# ═══ RPC Configuration ═══
RPC_URL = os.getenv("RPC_URL", "https://base-mainnet.g.alchemy.com/v2/")

# ═══ Database ═══
DATABASE_PATH = os.getenv("DATABASE_PATH", "data.db")

# ═══ Constants ═══
ONE_E18 = 10**18
SCALE_32 = 10**32
WEEK = 604800  # 7 days in seconds

# ═══ Pool Configuration ═══
# Mapping of pool addresses to pool names (populated by fetchers, can be overridden)
KNOWN_POOLS = {
    "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": "HYDX/USDC",
    "0xef96ec76eeb36584fc4922e9fa268e0780170f33": "kVCM/USDC",
    "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": "WETH/USDC",
}

# ═══ Fallback Legacy Pool Shares (for pre-flip estimation when contract data unavailable) ═══
LEGACY_POOL_SHARES = {
    "HYDX/USDC": 0.085994,
    "kVCM/USDC": 0.016156,
    "WETH/USDC": 0.036020,
}
