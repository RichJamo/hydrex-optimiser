"""
Configuration constants and settings for Hydrex Optimiser.

Centralizes all configuration including:
- Contract addresses
- RPC endpoints
- Database paths
- Call parameters
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ═══ Contract Addresses ═══
VOTER_ADDRESS = os.getenv("VOTER_ADDRESS", "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b")
VE_ADDRESS = os.getenv("VE_ADDRESS", "0x25B2ED7149fb8A05f6eF9407d9c8F878f59cd1e1")
HYDREX_REWARDS_DISTRIBUTOR_ADDRESS = os.getenv("HYDREX_REWARDS_DISTRIBUTOR_ADDRESS", "")

# ═══ Hydrex Swap Infrastructure ═══
HYDREX_ROUTER_ADDRESS = os.getenv(
    "HYDREX_ROUTER_ADDRESS", "0x6f4bE24d7dC93b6ffcBAb3Fd0747c5817Cea3F9e"
)  # Hydrex router for exactInputSingle swaps (direct mode)
HYDREX_MULTI_ROUTER_ADDRESS = os.getenv(
    "HYDREX_MULTI_ROUTER_ADDRESS", "0x599bFa1039C9e22603F15642B711D56BE62071f4"
)  # Hydrex multi-router for executeSwaps batch calldata (router-batch mode)
HYDREX_SWAP_DEPLOYER_ADDRESS = os.getenv(
    "HYDREX_SWAP_DEPLOYER_ADDRESS", "0x0000000000000000000000000000000000000000"
)  # exactInputSingle deployer param; zero-address works on live successful swaps
HYDREX_SWAP_EXECUTION_MODE = os.getenv(
    "HYDREX_SWAP_EXECUTION_MODE", "direct"
).strip().lower()  # Swap execution mode: direct | router-batch
HYDREX_ROUTING_API_URL = os.getenv(
    "HYDREX_ROUTING_API_URL", "https://router.api.hydrex.fi"
).rstrip("/")  # Hydrex routing API base URL for multi-quote
HYDREX_ROUTING_SOURCE = os.getenv(
    "HYDREX_ROUTING_SOURCE", "KYBERSWAP"
).strip()  # DEX aggregator source: KYBERSWAP | ZEROX | OPENOCEAN or CSV
HYDREX_ROUTING_SLIPPAGE_BPS = int(os.getenv("HYDREX_ROUTING_SLIPPAGE_BPS", "50"))  # Slippage in BPS (50 = 0.5%)
HYDREX_ROUTING_ORIGIN = os.getenv(
    "HYDREX_ROUTING_ORIGIN", "hydrex-optimiser"
).strip()  # Origin label for routing attribution
HYDREX_ROUTING_PRICE_CHUNK_SIZE = int(
    os.getenv("HYDREX_ROUTING_PRICE_CHUNK_SIZE", "10")
)  # Routing /quote/multi chunk size for token pricing
HYDREX_ROUTING_RETRY_MAX = int(
    os.getenv("HYDREX_ROUTING_RETRY_MAX", "3")
)  # Retry attempts for retriable routing statuses (429/503)
HYDREX_ROUTING_BACKOFF_BASE_SECONDS = float(
    os.getenv("HYDREX_ROUTING_BACKOFF_BASE_SECONDS", "1.5")
)  # Exponential backoff base seconds for retriable routing statuses
HYDREX_ROUTING_SINGLE_RETRY_DELAY_SECONDS = float(
    os.getenv("HYDREX_ROUTING_SINGLE_RETRY_DELAY_SECONDS", "0.05")
)  # Small delay between single-token fallback requests
HYDREX_ROUTING_SKIP_TOKENS = os.getenv(
    "HYDREX_ROUTING_SKIP_TOKENS", ""
)  # CSV token addresses to skip for routing price fetch
HYDREX_ROUTING_COINGECKO_FALLBACK_TOKENS = os.getenv(
    "HYDREX_ROUTING_COINGECKO_FALLBACK_TOKENS", ""
)  # CSV token addresses to bypass routing and fetch via CoinGecko
HYDREX_ROUTING_DEFER_TOKENS = os.getenv(
    "HYDREX_ROUTING_DEFER_TOKENS", ""
)  # CSV token addresses to route after normal tokens
HYDREX_PRICE_REFRESH_MAX_FAILURES = int(
    os.getenv("HYDREX_PRICE_REFRESH_MAX_FAILURES", "0")
)  # Max token price refresh failures allowed before abort
HYDREX_FACTORY_ADDRESS = "0x36077D39cdC65E1e3FB65810430E5b2c4D5fA29E"  # Factory/deployer param for router
USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # Base mainnet USDC
DUST_THRESHOLD_USD = 1.0  # Minimum $1 USD before swap (skip if below)
SLIPPAGE_START_PCT = 0.5  # Initial slippage tolerance
SWAP_RETRY_COUNT = 5  # Number of slippage retry attempts (increment by 1% each)
SWAP_DEADLINE_SECONDS = 600  # Swap deadline (10 minutes)
HYDREX_SWAP_SKIP_TOKENS = os.getenv(
    "HYDREX_SWAP_SKIP_TOKENS", ""
)  # Comma-separated symbols/addresses to skip in Phase 4 swaps
DELEGATED_INFLIGHT_MAX_RETRIES = int(
    os.getenv("DELEGATED_INFLIGHT_MAX_RETRIES", "6")
)  # Extra retries for delegated-account in-flight limit errors
DELEGATED_INFLIGHT_RETRY_SECONDS = float(
    os.getenv("DELEGATED_INFLIGHT_RETRY_SECONDS", "6")
)  # Backoff between delegated-account retry attempts
PENDING_NONCE_WAIT_SECONDS = int(
    os.getenv("PENDING_NONCE_WAIT_SECONDS", "90")
)  # Max wait for pending nonce to drain before next send
PENDING_NONCE_POLL_SECONDS = float(
    os.getenv("PENDING_NONCE_POLL_SECONDS", "2")
)  # Poll interval while waiting for pending nonce drain

# ═══ User Configuration ═══
MY_ESCROW_ADDRESS = os.getenv("MY_ESCROW_ADDRESS", "")
YOUR_TOKEN_ID = os.getenv("YOUR_TOKEN_ID", "")
ESCROW_ADDRESS = os.getenv("ESCROW_ADDRESS", MY_ESCROW_ADDRESS)

# ═══ RPC Configuration ═══
RPC_URL = os.getenv("RPC_URL", "https://base-mainnet.g.alchemy.com/v2/")

# ═══ Database ═══
def _resolve_database_path() -> str:
    configured_path = os.getenv("DATABASE_PATH")
    default_path = Path("data/db/data.db")

    if not configured_path:
        return str(default_path)

    configured = Path(configured_path)
    legacy_paths = {"data.db", "data/data.db"}

    if configured_path in legacy_paths and not configured.exists() and default_path.exists():
        return str(default_path)

    return configured_path


DATABASE_PATH = _resolve_database_path()

# ═══ Constants ═══
ONE_E18 = 10**18
SCALE_32 = 10**32
WEEK = 604800  # 7 days in seconds

# ═══ Pool Configuration ═══
# Gauges excluded from voting (zero/unpriced reward history or other known issues)
# Use lowercase gauge address. Add entries with a comment explaining the reason.
GAUGE_DENYLIST: set = {
    "0x25c10987091f98bff0f48a5bd24d7b3bf3419c52",  # epochs 1775692800, 1773878400: repeated $0 reward (unpriced tokens)
    "0x5d08b7cdb98ad2db2c5b24c32f7c32ad7ff19379",  # epochs 1775692800, 1774483200, 1773878400: repeated $0 reward (unpriced tokens)
    "0x42b49967d38da5c4070336ce1cca91a802a11e8c",  # epoch 1773878400: 128k votes, $0 reward (unpriced tokens)
    "0x46bba290006233b0eda8fc6d6b4e66eb02115774",  # epoch 1774483200: 1k votes, $0 reward (unpriced tokens)
    "0xe63cd99406e98d909ab6d702b11dd4cd31a425a2",  # epoch 1773273600: 36k votes, $0 reward (unpriced tokens)
    "0x8a6ca1a3b3f97562c804e5b85489aaa1f7bc8e27",  # 4 epochs: avg 18% of predicted reward (pred ~$1.2k, act ~$270)
    "0x71354cf35384e8de09cad28d4ff4ef8acd6600ef",  # 2 epochs: avg 1% of predicted reward (pred ~$1.2k, act ~$9)
    "0xd1184e9c1f05d78b65f977e12c3d4641c2e511a9",  # 2 epochs: avg 1% of predicted reward (pred ~$780, act ~$12)
}

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
