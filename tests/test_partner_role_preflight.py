"""Pre-flight check that the vote signer still holds PARTNER_ROLE on the escrow.

The escrow (0x768a675B…) is an OpenZeppelin AccessControl contract and the vote path is
gated on PARTNER_ROLE (0x2f049b28…). That grant lives on-chain and the DEFAULT_ADMIN_ROLE
holder can revoke it without any change to this repo, so a run can look correctly
configured and still revert at broadcast. The check must fail closed at startup.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "boundary_monitor.py"

ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"
SIGNER = "0xAB75E66C63307396FE8456Ea7c42CBBF3CF36298"


def _load():
    spec = importlib.util.spec_from_file_location("boundary_monitor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


class _FakeEth:
    def __init__(self, result=None, exc=None):
        self._result, self._exc = result, exc
        self.calls = []

    def call(self, tx):
        self.calls.append(tx)
        if self._exc:
            raise self._exc
        return self._result


class _FakeW3:
    def __init__(self, result=None, exc=None):
        self.eth = _FakeEth(result, exc)

    @staticmethod
    def keccak(text=""):
        from web3 import Web3 as _W
        return _W.keccak(text=text)


def test_role_held_returns_true(mod):
    w3 = _FakeW3(result=(1).to_bytes(32, "big"))
    ok, detail = mod.check_signer_partner_role(w3, ESCROW, SIGNER)
    assert ok is True
    assert "holds PARTNER_ROLE" in detail


def test_role_absent_returns_false(mod):
    """A revoked role must fail closed, not pass silently."""
    w3 = _FakeW3(result=(0).to_bytes(32, "big"))
    ok, detail = mod.check_signer_partner_role(w3, ESCROW, SIGNER)
    assert ok is False
    assert "does NOT hold PARTNER_ROLE" in detail


def test_rpc_failure_is_reported_not_raised(mod):
    """An RPC error must be surfaced as a failed check, never propagated."""
    w3 = _FakeW3(exc=RuntimeError("connection reset"))
    ok, detail = mod.check_signer_partner_role(w3, ESCROW, SIGNER)
    assert ok is False
    assert "could not verify PARTNER_ROLE" in detail
    assert "connection reset" in detail


@pytest.mark.parametrize("escrow,signer", [("", SIGNER), (ESCROW, ""), ("", "")])
def test_missing_configuration_fails_closed(mod, escrow, signer):
    ok, detail = mod.check_signer_partner_role(mod and _FakeW3((1).to_bytes(32, "big")), escrow, signer)
    assert ok is False
    assert "not configured" in detail


def test_call_targets_the_escrow_with_the_partner_role_id(mod):
    """Guards against checking the wrong role or the wrong contract."""
    w3 = _FakeW3(result=(1).to_bytes(32, "big"))
    mod.check_signer_partner_role(w3, ESCROW, SIGNER)
    tx = w3.eth.calls[0]
    assert tx["to"].lower() == ESCROW.lower()
    payload = tx["data"].hex() if hasattr(tx["data"], "hex") else tx["data"]
    assert mod.PARTNER_ROLE[2:] in payload.lower()
    assert SIGNER[2:].lower() in payload.lower()


def test_partner_role_constant_matches_onchain_value(mod):
    """Pinned from PARTNER_ROLE() on the deployed escrow, 2026-09-04."""
    assert mod.PARTNER_ROLE == (
        "0x2f049b28665abd79bc83d9aa564dba6b787ac439dba27b48e163a83befa9b260"
    )
