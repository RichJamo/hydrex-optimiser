"""Which Voter claim functions the claim script uses for a claim context.

Found 2026-09-15 when switching to VOTE_FROM=signer: the *ToRecipientByAddress claim
functions revert NotApprovedOrOwner() for a plain wallet even when it claims its own
rewards to itself. claimFees/claimBribes(address[],address[][]) work, and a traced call
showed the Voter passing msg.sender to Bribe.getRewardForAddress.

Contract under test (voter_claim_call_spec):
  - signer == claim_for == recipient (case-insensitive) -> two-argument self-claim
    functions, called with (bribes, tokens).
  - anything else -> recipient functions, called with (bribes, tokens, claim_for, recipient).
  - every signature returned exists in the VoterV5 ABI.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claim_and_swap_rewards.py"

SIGNER = "0xAB75E66C63307396FE8456Ea7c42CBBF3CF36298"
ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"
COLD = "0x00000000000000000000000000000000000000c0"
BRIBES, TOKENS = ["0xb1"], [["0xt1"]]


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("claim_and_swap_rewards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _voter_function_signatures():
    abi = json.loads((ROOT / "voterv5_abi.json").read_text())
    return {
        f"{f['name']}({','.join(i['type'] for i in f['inputs'])})"
        for f in abi
        if f.get("type") == "function"
    }


def test_self_claim_uses_two_argument_functions(mod):
    sigs, build = mod.voter_claim_call_spec(SIGNER, SIGNER.lower(), SIGNER.upper().replace("0X", "0x"))
    assert sigs == {"fees": "claimFees(address[],address[][])", "bribes": "claimBribes(address[],address[][])"}
    assert build(BRIBES, TOKENS) == (BRIBES, TOKENS)


@pytest.mark.parametrize("claim_for,recipient", [(ESCROW, ESCROW), (SIGNER, COLD), (ESCROW, SIGNER)])
def test_other_contexts_use_recipient_functions(mod, claim_for, recipient):
    sigs, build = mod.voter_claim_call_spec(SIGNER, claim_for, recipient)
    assert sigs["bribes"].startswith("claimBribesToRecipientByAddress(")
    assert sigs["fees"].startswith("claimFeesToRecipientByAddress(")
    assert build(BRIBES, TOKENS) == (BRIBES, TOKENS, claim_for, recipient)


def test_all_signatures_exist_in_voter_abi(mod):
    known = _voter_function_signatures()
    for table in (mod.VOTER_SELF_CLAIM_SIGNATURES, mod.VOTER_RECIPIENT_CLAIM_SIGNATURES):
        for signature in table.values():
            assert signature in known, signature


def test_default_claim_source_follows_vote_from(mod):
    expected = "voter" if mod.VOTE_FROM == "signer" else "escrow"
    assert mod.DEFAULT_CLAIM_SOURCE == expected
