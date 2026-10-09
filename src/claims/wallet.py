"""Phase 1 of claim_and_swap_rewards.py: wallet loading and preflight checks."""

import logging
import os
import subprocess
from typing import Optional

from eth_account import Account
from eth_utils import to_checksum_address
from web3 import Web3

from config.settings import RPC_URL

logger = logging.getLogger(__name__)

# ═══ Constants ═══
ONE_E18 = 10**18
CHAIN_ID = 8453  # Base mainnet


# ═══ Wallet Loading (Phase 1) ═══
def load_wallet_from_1password(vault_item_field: str) -> Account:
    """
    Load wallet from 1Password CLI.

    vault_item_field format: "vault/item/field"
    Example: "Personal/my_hot_wallet/private_key"

    Calls: op item get vault/item --fields field

    Returns: eth_account.Account object
    Raises: FileNotFoundError if `op` CLI not installed
    Raises: Exception if op CLI fails
    """
    try:
        parts = vault_item_field.split("/")
        if len(parts) != 3:
            raise ValueError(
                f"Invalid format: {vault_item_field}. Expected vault/item/field."
            )

        vault, item, field = parts
        cmd = ["op", "item", "get", f"{vault}/{item}", "--fields", field, "--reveal"]

        logger.info(f"Fetching private key from 1Password: op://{vault_item_field}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            raise Exception(f"op CLI failed: {result.stderr}")

        private_key = result.stdout.strip()
        if not private_key:
            raise ValueError("Empty private key returned from 1Password")

        # Remove 0x prefix if present
        if private_key.startswith("0x"):
            private_key = private_key[2:]

        account = Account.from_key(private_key)
        logger.info(f"Loaded wallet from 1Password: {account.address}")
        return account

    except FileNotFoundError:
        logger.error(
            "op CLI not found. Install 1Password CLI: https://developer.1password.com/docs/cli/"
        )
        raise


def load_wallet_from_file_or_env(source: str) -> Account:
    """
    Load wallet from file path, $ENV_VAR, or raw private key.

    Examples:
      - "/path/to/key.txt" -> reads file
      - "$MY_PK_ENV_VAR" -> reads environment variable
      - "0x..." -> treats as raw key

    Returns: eth_account.Account object
    Raises: FileNotFoundError if file doesn't exist
    Raises: KeyError if env var not found
    """
    source = source.strip()

    # If starts with $, treat as environment variable
    if source.startswith("$"):
        env_var = source[1:]
        private_key = os.getenv(env_var)
        if not private_key:
            raise KeyError(f"Environment variable {env_var} not found")
        logger.info(f"Loaded wallet from env var: {env_var}")

    # If file exists, read it
    elif os.path.isfile(source):
        with open(source, "r") as f:
            private_key = f.read().strip()
        logger.info(f"Loaded wallet from file: {source}")

    # Otherwise treat as raw key
    else:
        private_key = source
        logger.info("Using raw private key source")

    # Remove 0x prefix if present
    if private_key.startswith("0x"):
        private_key = private_key[2:]

    account = Account.from_key(private_key)
    logger.info(f"Loaded wallet: {account.address}")
    return account


def load_wallet(wallet_source: Optional[str]) -> Account:
    """
    Load wallet with 3-source fallback chain:

    1. CLI argument (if provided)
    2. TEST_WALLET_PK environment variable
    3. Error (no wallet source available)

    Returns: eth_account.Account object
    """
    # Priority 1: CLI argument
    if wallet_source:
        if wallet_source.startswith("op://"):
            # 1Password format
            vault_item_field = wallet_source[5:]  # Strip "op://" prefix
            return load_wallet_from_1password(vault_item_field)
        else:
            # File, env var, or raw key
            return load_wallet_from_file_or_env(wallet_source)

    # Priority 2: TEST_WALLET_PK env var
    test_wallet_pk = os.getenv("TEST_WALLET_PK")
    if test_wallet_pk:
        logger.info("Using TEST_WALLET_PK environment variable")
        return load_wallet_from_file_or_env(test_wallet_pk)

    # Priority 3: Error
    raise ValueError(
        "No wallet source provided. Use --wallet flag or set TEST_WALLET_PK env var."
    )


# ═══ Preflight Checks (Phase 1) ═══
def preflight_checks(w3: Web3, signer: Account) -> None:
    """
    Validate execution environment before business logic.

    Checks:
      - RPC connectivity (web3.isConnected())
      - Chain ID is Base mainnet (8453)
      - Signer has valid nonce
      - Gas price is available

    Raises: Exception if any check fails
    """
    logger.info("Running preflight checks...")

    # Check RPC connectivity
    if not w3.is_connected():
        raise Exception("RPC not connected")
    logger.info(f"✓ RPC connected: {RPC_URL}")

    # Check chain ID
    chain_id = w3.eth.chain_id
    if chain_id != CHAIN_ID:
        raise Exception(f"Wrong chain ID: got {chain_id}, expected {CHAIN_ID}")
    logger.info(f"✓ Chain ID correct: {chain_id}")

    # Check signer exists
    signer_address = to_checksum_address(signer.address)
    logger.info(f"✓ Signer address: {signer_address}")

    # Check signer has nonce (exists as EOA)
    try:
        nonce = w3.eth.get_transaction_count(signer_address)
        logger.info(f"✓ Signer nonce: {nonce}")
    except Exception as e:
        raise Exception(f"Failed to fetch signer nonce: {e}")

    # Check gas price available
    try:
        gas_price = w3.eth.gas_price
        logger.info(f"✓ Gas price available: {w3.from_wei(gas_price, 'gwei')} gwei")
    except Exception as e:
        raise Exception(f"Failed to fetch gas price: {e}")

    logger.info("✓ All preflight checks passed")
