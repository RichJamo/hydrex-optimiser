"""Files a visitor can browse must not tie the voting wallet to its owner.

The README publishes weekly earnings and the owner's profile names him, so commit 266d5dd
replaced the signer and escrow addresses, the veNFT id and our transaction hashes in
public files with <signer>, <escrow>, <veNFT id> and <tx redacted>. Nothing enforced it:
on 2026-10-15 a freshly written epoch record carried five transaction hashes and was
caught only by eye before commit.

Contract under test:
1. No 64-hex-digit string (a transaction hash) appears in README.md, docs/, data/epochs/
   or data/postmortem/. Always runs.
2. None of the identifiers listed in REDACTED_IDENTIFIERS appears in any tracked file.
   The list lives in the git-ignored .env (comma-separated: signer address, escrow
   address, veNFT id), because writing it here would publish it. Skips where .env does
   not set it, e.g. in CI.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_PATHS = ["README.md", "docs", "data/epochs", "data/postmortem"]
TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}\b")


def _tracked_files(*pathspecs):
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [ROOT / name for name in listed.split("\0") if name]


def _text(path):
    try:
        return path.read_text()
    except (UnicodeDecodeError, FileNotFoundError):
        return None  # binary, or deleted in the working tree but still tracked


def _redacted_identifiers():
    raw = dotenv_values(ROOT / ".env").get("REDACTED_IDENTIFIERS") or os.environ.get(
        "REDACTED_IDENTIFIERS", ""
    )
    return [value.strip().lower() for value in raw.split(",") if value.strip()]


def test_public_files_are_found():
    # Guards against the pathspecs silently matching nothing, which would pass below.
    assert len(_tracked_files(*PUBLIC_PATHS)) > 10


def test_no_transaction_hashes_in_public_files():
    offending = []
    for path in _tracked_files(*PUBLIC_PATHS):
        text = _text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for match in TX_HASH.finditer(line):
                offending.append(f"{path.relative_to(ROOT)}:{number}: {match.group()}")
    assert not offending, (
        "transaction hashes in public files; replace each with <tx redacted>:\n"
        + "\n".join(offending)
    )


def test_no_wallet_identifiers_in_tracked_files():
    identifiers = _redacted_identifiers()
    if not identifiers:
        pytest.skip("REDACTED_IDENTIFIERS is not set in .env (expected in CI)")
    patterns = {
        identifier: re.compile(rf"(?<![0-9a-z]){re.escape(identifier)}(?![0-9a-z])")
        for identifier in identifiers
    }
    offending = []
    for path in _tracked_files():
        text = _text(path)
        if text is None:
            continue
        lowered = text.lower()
        for identifier, pattern in patterns.items():
            if pattern.search(lowered):
                # Report the file only: echoing the identifier into CI output would leak it.
                offending.append(str(path.relative_to(ROOT)))
                break
    assert not offending, (
        "a REDACTED_IDENTIFIERS value appears in these tracked files; replace it with "
        "<signer>, <escrow> or <veNFT id>:\n" + "\n".join(offending)
    )
