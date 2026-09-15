"""Signer wallet loading shared by the vote scripts."""

import os

from eth_account import Account


def load_wallet(private_key_source: str) -> Account:
    """Load wallet from private key source: raw key or file path."""
    if os.path.isfile(private_key_source):
        with open(private_key_source, "r") as f:
            private_key = f.read().strip()
    else:
        private_key = private_key_source

    # Remove 0x prefix if present
    if private_key.startswith("0x"):
        private_key = private_key[2:]

    return Account.from_key(private_key)
