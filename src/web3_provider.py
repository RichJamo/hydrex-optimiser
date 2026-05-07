"""
Lazy singleton Web3 provider for hydrex-optimiser.

All code that needs an on-chain connection should use get_w3() rather than
constructing a Web3 instance directly.  This ensures:
  - Exactly one HTTPProvider is created per process (cheap, but cleaner)
  - RPC_URL and RPC_TIMEOUT are always sourced from config
  - The provider is not instantiated at import time (safe in test environments)

Usage
-----
    from src.web3_provider import get_w3

    w3 = get_w3()
    block = w3.eth.block_number
"""

from __future__ import annotations

from typing import Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from config import Config

_w3: Optional[Web3] = None


def get_w3() -> Web3:
    """
    Return the process-wide Web3 instance, creating it on first call.

    The instance is configured with:
    - Config.RPC_URL  (raises ValueError if blank)
    - Config.RPC_TIMEOUT seconds for HTTP requests
    - PoA extra-data middleware (required for Base mainnet)
    """
    global _w3
    if _w3 is not None:
        return _w3

    rpc_url = Config.RPC_URL
    if not rpc_url:
        raise ValueError(
            "RPC_URL is not set.  Add it to your .env file or environment before "
            "calling get_w3()."
        )

    provider = Web3.HTTPProvider(
        rpc_url,
        request_kwargs={"timeout": Config.RPC_TIMEOUT},
    )
    w3 = Web3(provider)
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    _w3 = w3
    return _w3


def reset_w3() -> None:
    """
    Clear the cached provider.  Intended for use in tests only.

    After calling this, the next get_w3() call will create a fresh provider,
    which allows tests to inject a different RPC_URL via monkeypatching Config.
    """
    global _w3
    _w3 = None
