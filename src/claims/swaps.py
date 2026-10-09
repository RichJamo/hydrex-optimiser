"""Phase 4 of claim_and_swap_rewards.py: building and executing USDC swaps."""

import json
import logging
import math
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from eth_account import Account
from eth_utils import to_checksum_address
from rich.console import Console
from rich.table import Table
from web3 import Web3

from config.settings import (
    DELEGATED_INFLIGHT_MAX_RETRIES,
    DELEGATED_INFLIGHT_RETRY_SECONDS,
    DUST_THRESHOLD_USD,
    HYDREX_MULTI_ROUTER_ADDRESS,
    HYDREX_ROUTER_ADDRESS,
    HYDREX_ROUTING_API_URL,
    HYDREX_ROUTING_ORIGIN,
    HYDREX_ROUTING_SLIPPAGE_BPS,
    HYDREX_ROUTING_SOURCE,
    HYDREX_SWAP_DEPLOYER_ADDRESS,
    HYDREX_SWAP_SKIP_TOKENS,
    PENDING_NONCE_POLL_SECONDS,
    PENDING_NONCE_WAIT_SECONDS,
    SLIPPAGE_START_PCT,
    SWAP_DEADLINE_SECONDS,
    SWAP_RETRY_COUNT,
    USDC_ADDRESS,
)

from src.claims.wallet import CHAIN_ID
from src.claims.reclaim_guard import (
    is_delegated_inflight_error,
    is_nonce_too_low_error,
    signed_tx_raw_bytes,
    wait_for_pending_nonce_drain,
    wait_for_receipt,
)

logger = logging.getLogger(__name__)

console = Console()

# Standard ERC20 ABI (minimal for token operations)
ERC20_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ROUTER_ABI = [
    {
        "inputs": [],
        "name": "poolDeployer",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "address", "name": "deployer", "type": "address"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {
                        "internalType": "uint256",
                        "name": "amountOutMinimum",
                        "type": "uint256",
                    },
                    {
                        "internalType": "uint160",
                        "name": "limitSqrtPrice",
                        "type": "uint160",
                    },
                ],
                "internalType": "struct ISwapRouter.ExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"}
        ],
        "stateMutability": "payable",
        "type": "function",
    },
]

MULTI_ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "router", "type": "address"},
                    {
                        "internalType": "address",
                        "name": "inputAsset",
                        "type": "address",
                    },
                    {
                        "internalType": "address",
                        "name": "outputAsset",
                        "type": "address",
                    },
                    {
                        "internalType": "uint256",
                        "name": "inputAmount",
                        "type": "uint256",
                    },
                    {
                        "internalType": "uint256",
                        "name": "minOutputAmount",
                        "type": "uint256",
                    },
                    {"internalType": "bytes", "name": "callData", "type": "bytes"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "string", "name": "origin", "type": "string"},
                    {"internalType": "address", "name": "referral", "type": "address"},
                    {
                        "internalType": "uint256",
                        "name": "referralFeeBps",
                        "type": "uint256",
                    },
                ],
                "internalType": "struct HydrexMultiRouter.SwapData[]",
                "name": "swaps",
                "type": "tuple[]",
            },
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "executeSwaps",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def fetch_token_price_usd(
    conn: sqlite3.Connection, token_address: str
) -> Optional[float]:
    """Fetch USD price for token from local cache table."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT usd_price
        FROM token_prices
        WHERE lower(token_address) = lower(?)
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (token_address,),
    )
    row = cursor.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def build_swap_intents(
    w3: Web3,
    conn: sqlite3.Connection,
    signer_address: str,
    reward_tokens: Dict[str, Dict],
) -> List[Dict]:
    """Build swap intents for non-USDC tokens above USD dust threshold."""
    intents: List[Dict] = []
    usdc_addr = to_checksum_address(USDC_ADDRESS)
    skip_entries = {
        entry.strip().lower()
        for entry in HYDREX_SWAP_SKIP_TOKENS.split(",")
        if entry.strip()
    }

    for token_addr, token_info in reward_tokens.items():
        token_cs = to_checksum_address(token_addr)
        if token_cs == usdc_addr:
            continue

        symbol = token_info.get("symbol", "UNKNOWN")
        if token_cs.lower() in skip_entries or symbol.lower() in skip_entries:
            logger.info(
                "Skipping %s (%s) - configured in HYDREX_SWAP_SKIP_TOKENS",
                symbol,
                token_cs,
            )
            continue

        token_contract = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
        raw_balance = token_contract.functions.balanceOf(signer_address).call()
        if raw_balance <= 0:
            continue

        decimals = int(token_info.get("decimals", 18))
        balance_units = raw_balance / (10**decimals)

        usd_price = fetch_token_price_usd(conn, token_cs)
        if usd_price is None:
            logger.warning(
                f"Skipping {symbol} ({token_cs}) - no USD price in token_prices cache"
            )
            continue

        usd_value = balance_units * usd_price
        if usd_value < DUST_THRESHOLD_USD:
            logger.info(
                f"Skipping {symbol} ({token_cs}) - below dust threshold "
                f"${usd_value:.4f} < ${DUST_THRESHOLD_USD:.2f}"
            )
            continue

        expected_usdc_out = usd_value
        intent = {
            "token": token_cs,
            "symbol": symbol,
            "decimals": decimals,
            "balance_raw": int(raw_balance),
            "balance_units": balance_units,
            "usd_price": usd_price,
            "usd_value": usd_value,
            "expected_usdc_out": expected_usdc_out,
        }
        intents.append(intent)

    intents.sort(key=lambda x: x["usd_value"], reverse=True)
    return intents


def send_contract_transaction(
    w3: Web3,
    signer: Account,
    tx: Dict,
) -> Tuple[str, int]:
    """Sign, send, and wait for receipt for a prepared transaction."""
    signed = w3.eth.account.sign_transaction(tx, signer.key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx_raw_bytes(signed))
    receipt = wait_for_receipt(w3, tx_hash)
    return tx_hash.hex(), receipt.status


def build_swap_execution_summary_table(results: List[Dict]) -> None:
    """Render concise Phase 4 swap execution summary."""
    if not results:
        return

    table = Table(title="Phase 4 Swap Execution Summary", header_style="bold cyan")
    table.add_column("Token")
    table.add_column("USD", justify="right")
    table.add_column("Attempts", justify="right")
    table.add_column("Status")
    table.add_column("Tx Hash")

    for r in results:
        table.add_row(
            r.get("symbol", "UNKNOWN"),
            f"{r.get('usd_value', 0):.2f}",
            str(r.get("attempts", 0)),
            r.get("status", "-"),
            (r.get("tx_hash", "-")[:18] + "...") if r.get("tx_hash") else "-",
        )

    console.print(table)


def execute_swap_intents(
    w3: Web3,
    signer: Account,
    swap_recipient: str,
    intents: List[Dict],
    broadcast: bool,
    continue_on_error: bool = True,
) -> List[Dict]:
    """Execute Phase 4 swaps with exact approvals and slippage retry ladder."""
    results: List[Dict] = []
    if not intents:
        logger.info("No swap intents generated for Phase 4")
        return results

    router_addr = to_checksum_address(HYDREX_ROUTER_ADDRESS)
    router_code = w3.eth.get_code(router_addr)
    if len(router_code) == 0:
        raise RuntimeError(
            f"Hydrex router address has no contract bytecode on chain {CHAIN_ID}: {router_addr}. "
            "Aborting swaps to avoid no-op transactions."
        )

    router = w3.eth.contract(address=router_addr, abi=ROUTER_ABI)
    usdc_addr = to_checksum_address(USDC_ADDRESS)
    usdc_code = w3.eth.get_code(usdc_addr)
    if len(usdc_code) == 0:
        raise RuntimeError(
            f"USDC address has no contract bytecode on chain {CHAIN_ID}: {usdc_addr}."
        )

    nonce = w3.eth.get_transaction_count(signer.address)
    gas_price = w3.eth.gas_price

    for intent in intents:
        token = intent["token"]
        symbol = intent["symbol"]
        amount_in = int(intent["balance_raw"])
        expected_usdc_out_raw = int(math.floor(intent["expected_usdc_out"] * 1_000_000))

        swap_result = {
            "token": token,
            "symbol": symbol,
            "amount_in": amount_in,
            "usd_value": intent["usd_value"],
            "status": "skipped",
            "attempts": 0,
        }

        for attempt in range(SWAP_RETRY_COUNT):
            slippage = SLIPPAGE_START_PCT + attempt
            min_out = int(expected_usdc_out_raw * (1 - slippage / 100.0))
            deadline = int(time.time()) + SWAP_DEADLINE_SECONDS

            swap_result["attempts"] = attempt + 1
            swap_result["slippage_pct"] = slippage
            swap_result["amount_out_minimum"] = max(min_out, 0)

            if not broadcast:
                logger.info(
                    f"DRY RUN swap {symbol}: amount_in={amount_in} "
                    f"min_out={swap_result['amount_out_minimum']} slippage={slippage:.2f}%"
                )
                swap_result["status"] = "dry_run"
                break

            token_addr = to_checksum_address(token)
            token_code = w3.eth.get_code(token_addr)
            if len(token_code) == 0:
                raise RuntimeError(
                    f"Token address has no contract bytecode on chain {CHAIN_ID}: {token_addr}"
                )

            token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
            router_allowance = token_contract.functions.allowance(
                signer.address,
                router_addr,
            ).call()
            needs_approve = router_allowance < amount_in

            transient_retries = 0
            attempt_finished = False
            while transient_retries <= DELEGATED_INFLIGHT_MAX_RETRIES:
                try:
                    wait_for_pending_nonce_drain(
                        w3,
                        signer.address,
                        timeout_seconds=PENDING_NONCE_WAIT_SECONDS,
                        poll_seconds=PENDING_NONCE_POLL_SECONDS,
                    )
                    nonce = w3.eth.get_transaction_count(signer.address, "pending")
                    gas_price = w3.eth.gas_price

                    if needs_approve:
                        approve_tx = token_contract.functions.approve(
                            to_checksum_address(HYDREX_ROUTER_ADDRESS),
                            amount_in,
                        ).build_transaction(
                            {
                                "from": signer.address,
                                "chainId": CHAIN_ID,
                                "nonce": nonce,
                                "gas": 120000,
                                "gasPrice": gas_price,
                            }
                        )
                        _, approve_status = send_contract_transaction(
                            w3, signer, approve_tx
                        )
                        nonce += 1
                        if approve_status != 1:
                            raise RuntimeError("approve transaction reverted")
                        needs_approve = False

                    swap_deployer = to_checksum_address(HYDREX_SWAP_DEPLOYER_ADDRESS)
                    swap_params = (
                        to_checksum_address(token),
                        usdc_addr,
                        swap_deployer,
                        to_checksum_address(swap_recipient),
                        deadline,
                        amount_in,
                        swap_result["amount_out_minimum"],
                        0,
                    )
                    swap_tx = router.functions.exactInputSingle(
                        swap_params
                    ).build_transaction(
                        {
                            "from": signer.address,
                            "chainId": CHAIN_ID,
                            "nonce": nonce,
                            "gas": 900000,
                            "gasPrice": gas_price,
                            "value": 0,
                        }
                    )
                    tx_hash, swap_status = send_contract_transaction(
                        w3, signer, swap_tx
                    )
                    nonce += 1

                    if swap_status == 1:
                        swap_receipt = w3.eth.get_transaction_receipt(tx_hash)
                        if len(swap_receipt.logs) == 0:
                            raise RuntimeError(
                                "swap transaction mined with zero logs (probable no-op); "
                                "router/address configuration likely invalid"
                            )
                        swap_result["status"] = "success"
                        swap_result["tx_hash"] = tx_hash
                        logger.info(f"Swap success {symbol} tx={tx_hash}")
                        attempt_finished = True
                        break

                    raise RuntimeError("swap transaction reverted")
                except Exception as e:
                    if (
                        is_delegated_inflight_error(e)
                        and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES
                    ):
                        transient_retries += 1
                        logger.warning(
                            "Delegated in-flight limit for %s (retry %s/%s). Backing off %.1fs",
                            symbol,
                            transient_retries,
                            DELEGATED_INFLIGHT_MAX_RETRIES,
                            DELEGATED_INFLIGHT_RETRY_SECONDS,
                        )
                        time.sleep(DELEGATED_INFLIGHT_RETRY_SECONDS)
                        continue

                    if (
                        is_nonce_too_low_error(e)
                        and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES
                    ):
                        transient_retries += 1
                        logger.warning(
                            "Nonce sync race for %s (retry %s/%s). Re-syncing nonce after %.1fs",
                            symbol,
                            transient_retries,
                            DELEGATED_INFLIGHT_MAX_RETRIES,
                            PENDING_NONCE_POLL_SECONDS,
                        )
                        time.sleep(PENDING_NONCE_POLL_SECONDS)
                        continue

                    swap_result["error"] = str(e)
                    logger.warning(
                        f"Swap attempt {attempt + 1}/{SWAP_RETRY_COUNT} failed for {symbol}: {e}"
                    )
                    if attempt == SWAP_RETRY_COUNT - 1:
                        swap_result["status"] = "error"
                    if not continue_on_error:
                        results.append(swap_result)
                        raise
                    attempt_finished = True
                    break

            if swap_result.get("status") == "success":
                break
            if attempt_finished:
                continue

        results.append(swap_result)

    return results


def get_multi_quote(
    intents: List[Dict],
    taker: str,
    slippage_bps: int,
    source: str,
    origin: str,
) -> Dict:
    """
    Call POST /quote/multi on the Hydrex routing API.

    Returns the parsed JSON response dict.
    Raises RuntimeError on HTTP errors or missing transaction fields.

    Args:
        intents:      swap intent objects built by build_swap_intents()
        taker:        wallet that will send the executeSwaps tx (must hold the
                      approved tokens); USDC output lands here before the optional
                      forward step.
        slippage_bps: execution slippage in BPS (50 = 0.5 %)
        source:       comma-separated aggregator sources (e.g. "KYBERSWAP")
        origin:       origin label for routing attribution
    """
    swap_items = [
        {
            "fromTokenAddress": intent["token"],
            "toTokenAddress": USDC_ADDRESS,
            "amount": str(intent["balance_raw"]),
        }
        for intent in intents
    ]

    payload: Dict = {
        "taker": taker,
        "chainId": str(CHAIN_ID),
        "slippage": str(slippage_bps),
        "origin": origin,
        "swaps": swap_items,
    }
    if source:
        payload["source"] = source

    body = json.dumps(payload).encode("utf-8")
    url = f"{HYDREX_ROUTING_API_URL}/quote/multi"

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Origin": "https://router.api.hydrex.fi",
            "Referer": "https://router.api.hydrex.fi/",
        },
        method="POST",
    )

    logger.info(
        "Requesting multi-quote from routing API: %s (%d legs)", url, len(swap_items)
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Routing API HTTP {exc.code} for multi-quote: {body_text[:500]}"
        ) from exc

    if "transaction" not in data or "data" not in data.get("transaction", {}):
        raise RuntimeError(
            f"Routing API response missing transaction.data: {json.dumps(data)[:500]}"
        )

    return data


def execute_router_batch_swaps(
    w3: Web3,
    signer: Account,
    swap_recipient: str,
    intents: List[Dict],
    broadcast: bool,
) -> Dict:
    """
    Phase 4 (router-batch mode): approve tokens, get one multi-quote, send one
    executeSwaps tx to the Hydrex multi-router.

    Flow (matches boss's suggested approach):
      1. Build a single POST /quote/multi call for all eligible tokens.
         taker=swap_recipient so USDC is delivered directly to the cold wallet.
      2. Validate the returned router address against HYDREX_MULTI_ROUTER_ADDRESS.
      3. Approve each input token on the multi-router (skip if allowance sufficient).
      4. Send the single executeSwaps transaction.
      5. Verify USDC balance at swap_recipient increased (non-zero output check).

    Returns a result dict describing the outcome.
    """
    multi_router_addr = to_checksum_address(HYDREX_MULTI_ROUTER_ADDRESS)

    # Validate multi-router has code
    router_code = w3.eth.get_code(multi_router_addr)
    if len(router_code) == 0:
        raise RuntimeError(
            f"Hydrex multi-router has no bytecode on chain {CHAIN_ID}: {multi_router_addr}"
        )

    usdc_addr = to_checksum_address(USDC_ADDRESS)
    signer_addr = to_checksum_address(signer.address)
    recipient_addr = to_checksum_address(swap_recipient)

    recipient_is_signer = signer_addr.lower() == recipient_addr.lower()

    result: Dict = {
        "mode": "router-batch",
        "intents_count": len(intents),
        "status": "skipped",
    }

    if not intents:
        logger.info(
            "Router-batch: no swap intents to execute; skipping routing API call"
        )
        result.update(
            {
                "status": "skipped",
                "error": None,
                "legs": [],
                "approvals": [],
                "usdc_recipient": recipient_addr,
            }
        )
        return result

    if not broadcast:
        # Dry-run: still call the routing API to validate routes exist
        logger.info(
            "DRY RUN router-batch: calling routing API to validate %d swap legs",
            len(intents),
        )
        try:
            quote = get_multi_quote(
                intents,
                # taker must be the address that holds the tokens and sends the tx
                taker=signer_addr,
                slippage_bps=HYDREX_ROUTING_SLIPPAGE_BPS,
                source=HYDREX_ROUTING_SOURCE,
                origin=HYDREX_ROUTING_ORIGIN,
            )
            tx_to = quote["transaction"].get("to", "").lower()
            result.update(
                {
                    "status": "dry_run",
                    "routing_api_router": quote["transaction"].get("to"),
                    "legs": [
                        {
                            "from": s["fromTokenAddress"],
                            "to": s["toTokenAddress"],
                            "amountIn": s.get("amountIn"),
                            "amountOut": s.get("amountOut"),
                            "source": s.get("source"),
                        }
                        for s in quote.get("swaps", [])
                    ],
                    "total_usd": quote.get("totalAmountUsd"),
                }
            )
            logger.info(
                "DRY RUN multi-quote ok: %d legs totalUsd=%s routerTarget=%s",
                len(quote.get("swaps", [])),
                quote.get("totalAmountUsd"),
                quote["transaction"].get("to"),
            )
        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc)
            logger.error("DRY RUN routing API call failed: %s", exc)
        return result

    # --- live broadcast path ---

    # USDC balance before (at signer; USDC lands here after executeSwaps)
    usdc_contract = w3.eth.contract(address=usdc_addr, abi=ERC20_ABI)
    usdc_before = usdc_contract.functions.balanceOf(signer_addr).call()

    # Step 1: Approve tokens on multi-router
    # NOTE: quote is fetched AFTER approvals so calldata is fresh when the tx is mined
    gas_price = w3.eth.gas_price
    nonce = w3.eth.get_transaction_count(signer_addr)

    approval_results = []
    for intent in intents:
        token_addr = to_checksum_address(intent["token"])
        amount_in = int(intent["balance_raw"])
        token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        current_allowance = token_contract.functions.allowance(
            signer_addr, multi_router_addr
        ).call()
        if current_allowance >= amount_in:
            logger.info(
                "Approve %s: already sufficient (%d)",
                intent["symbol"],
                current_allowance,
            )
            approval_results.append(
                {
                    "token": token_addr,
                    "symbol": intent["symbol"],
                    "status": "already_approved",
                }
            )
            continue

        logger.info("Approving %s (%s) on multi-router…", intent["symbol"], token_addr)
        approval_done = False
        transient_retries = 0
        while transient_retries <= DELEGATED_INFLIGHT_MAX_RETRIES:
            try:
                wait_for_pending_nonce_drain(
                    w3,
                    signer_addr,
                    PENDING_NONCE_WAIT_SECONDS,
                    PENDING_NONCE_POLL_SECONDS,
                )
                nonce = w3.eth.get_transaction_count(signer_addr, "pending")
                gas_price = w3.eth.gas_price
                approve_tx = token_contract.functions.approve(
                    multi_router_addr, amount_in
                ).build_transaction(
                    {
                        "from": signer_addr,
                        "chainId": CHAIN_ID,
                        "nonce": nonce,
                        "gas": 120000,
                        "gasPrice": gas_price,
                    }
                )
                approve_hash, approve_status = send_contract_transaction(
                    w3, signer, approve_tx
                )
                nonce += 1
                if approve_status != 1:
                    raise RuntimeError(f"approve reverted for {intent['symbol']}")
                approval_results.append(
                    {
                        "token": token_addr,
                        "symbol": intent["symbol"],
                        "status": "approved",
                        "tx_hash": approve_hash,
                    }
                )
                logger.info("Approved %s tx=%s", intent["symbol"], approve_hash)
                approval_done = True
                break
            except Exception as exc:
                if (
                    is_delegated_inflight_error(exc)
                    and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES
                ):
                    transient_retries += 1
                    logger.warning(
                        "Delegated in-flight limit during approve for %s (retry %s/%s). Backing off %.1fs",
                        intent["symbol"],
                        transient_retries,
                        DELEGATED_INFLIGHT_MAX_RETRIES,
                        DELEGATED_INFLIGHT_RETRY_SECONDS,
                    )
                    time.sleep(DELEGATED_INFLIGHT_RETRY_SECONDS)
                    continue
                if (
                    is_nonce_too_low_error(exc)
                    and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES
                ):
                    transient_retries += 1
                    logger.warning(
                        "Nonce sync race during approve for %s (retry %s/%s). Re-syncing nonce after %.1fs",
                        intent["symbol"],
                        transient_retries,
                        DELEGATED_INFLIGHT_MAX_RETRIES,
                        PENDING_NONCE_POLL_SECONDS,
                    )
                    time.sleep(PENDING_NONCE_POLL_SECONDS)
                    continue

                approval_results.append(
                    {
                        "token": token_addr,
                        "symbol": intent["symbol"],
                        "status": "error",
                        "error": str(exc),
                    }
                )
                logger.error("Approve failed for %s: %s", intent["symbol"], exc)
                result["status"] = "error"
                result["error"] = f"approve failed for {intent['symbol']}: {exc}"
                result["approvals"] = approval_results
                return result

        if not approval_done:
            err = (
                f"approve failed for {intent['symbol']}: "
                "delegated-account retries exhausted"
            )
            approval_results.append(
                {
                    "token": token_addr,
                    "symbol": intent["symbol"],
                    "status": "error",
                    "error": err,
                }
            )
            logger.error(err)
            result["status"] = "error"
            result["error"] = err
            result["approvals"] = approval_results
            return result

    # Step 2: Fetch multi-quote (calldata) immediately before submitting the tx
    # so the embedded deadline in the routing API response is fresh at mining time.
    logger.info("Fetching multi-quote for %d swap legs via routing API…", len(intents))
    logger.info(
        "taker (signer): %s  final USDC recipient: %s", signer_addr, recipient_addr
    )
    active_intents = intents
    unroutable_intents: List[Dict] = []
    try:
        quote = get_multi_quote(
            active_intents,
            taker=signer_addr,
            slippage_bps=HYDREX_ROUTING_SLIPPAGE_BPS,
            source=HYDREX_ROUTING_SOURCE,
            origin=HYDREX_ROUTING_ORIGIN,
        )
    except RuntimeError as _batch_err:
        # If any token in the batch has no route, the whole batch fails with 400.
        # Probe each token individually to identify and exclude the unroutable ones,
        # then retry the batch with only the routable subset.
        if "No valid quotes" not in str(_batch_err) or len(active_intents) <= 1:
            raise
        logger.warning(
            "Batch quote failed (%s). Probing %d tokens individually to identify unroutable ones…",
            _batch_err,
            len(active_intents),
        )
        routable: List[Dict] = []
        for _intent in active_intents:
            try:
                get_multi_quote(
                    [_intent],
                    taker=signer_addr,
                    slippage_bps=HYDREX_ROUTING_SLIPPAGE_BPS,
                    source=HYDREX_ROUTING_SOURCE,
                    origin=HYDREX_ROUTING_ORIGIN,
                )
                routable.append(_intent)
                logger.info(
                    "  ✓ Routable: %s (%s)", _intent["symbol"], _intent["token"]
                )
            except RuntimeError:
                unroutable_intents.append(_intent)
                logger.warning(
                    "  ✗ Unroutable: %s (%s) – excluded from batch",
                    _intent["symbol"],
                    _intent["token"],
                )
        if not routable:
            result["status"] = "error"
            result["error"] = (
                f"All {len(active_intents)} swap intents are unroutable via routing API"
            )
            result["unroutable"] = [
                {"token": i["token"], "symbol": i["symbol"]} for i in unroutable_intents
            ]
            result["approvals"] = approval_results
            return result
        logger.info(
            "Retrying batch quote with %d routable token(s) (dropped %d unroutable)…",
            len(routable),
            len(unroutable_intents),
        )
        active_intents = routable
        quote = get_multi_quote(
            active_intents,
            taker=signer_addr,
            slippage_bps=HYDREX_ROUTING_SLIPPAGE_BPS,
            source=HYDREX_ROUTING_SOURCE,
            origin=HYDREX_ROUTING_ORIGIN,
        )

    quoted_router = to_checksum_address(quote["transaction"]["to"])
    if quoted_router.lower() != multi_router_addr.lower():
        raise RuntimeError(
            f"Routing API returned unexpected router address: {quoted_router}. "
            f"Expected multi-router: {multi_router_addr}. Aborting for safety."
        )

    calldata_hex = quote["transaction"]["data"]

    # Step 3: Send the single executeSwaps transaction
    logger.info(
        "Sending executeSwaps transaction to multi-router %s…", multi_router_addr
    )
    wait_for_pending_nonce_drain(
        w3, signer_addr, PENDING_NONCE_WAIT_SECONDS, PENDING_NONCE_POLL_SECONDS
    )
    nonce = w3.eth.get_transaction_count(signer_addr, "pending")
    gas_price = w3.eth.gas_price

    _estimate_tx = {
        "from": signer_addr,
        "to": multi_router_addr,
        "data": calldata_hex,
        "value": 0,
    }
    try:
        _estimated = w3.eth.estimate_gas(_estimate_tx)
        _gas_limit = int(_estimated * 1.4)
        logger.info(
            "executeSwaps gas estimate: %d  limit (×1.4): %d", _estimated, _gas_limit
        )
    except Exception as _est_exc:
        _gas_limit = 20_000_000
        logger.warning(
            "Gas estimation failed (%s); using fallback gas=%d", _est_exc, _gas_limit
        )

    swap_tx = {
        "from": signer_addr,
        "to": multi_router_addr,
        "data": calldata_hex,
        "chainId": CHAIN_ID,
        "nonce": nonce,
        "gas": _gas_limit,
        "gasPrice": gas_price,
        "value": 0,
    }

    receipt = None
    swap_tx_hash = None
    transient_retries = 0
    while transient_retries <= DELEGATED_INFLIGHT_MAX_RETRIES:
        try:
            wait_for_pending_nonce_drain(
                w3, signer_addr, PENDING_NONCE_WAIT_SECONDS, PENDING_NONCE_POLL_SECONDS
            )
            nonce = w3.eth.get_transaction_count(signer_addr, "pending")
            gas_price = w3.eth.gas_price
            swap_tx.update({"nonce": nonce, "gasPrice": gas_price})

            signed = w3.eth.account.sign_transaction(swap_tx, signer.key)
            raw = signed_tx_raw_bytes(signed)
            swap_tx_hash_bytes = w3.eth.send_raw_transaction(raw)
            swap_tx_hash = swap_tx_hash_bytes.hex()
            nonce += 1
            receipt = wait_for_receipt(w3, swap_tx_hash_bytes)
            logger.info(
                "executeSwaps tx mined: status=%d tx=%s", receipt.status, swap_tx_hash
            )
            break
        except Exception as exc:
            if (
                is_delegated_inflight_error(exc)
                and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES
            ):
                transient_retries += 1
                logger.warning(
                    "Delegated in-flight limit for executeSwaps (retry %s/%s). Backing off %.1fs",
                    transient_retries,
                    DELEGATED_INFLIGHT_MAX_RETRIES,
                    DELEGATED_INFLIGHT_RETRY_SECONDS,
                )
                time.sleep(DELEGATED_INFLIGHT_RETRY_SECONDS)
                continue
            if (
                is_nonce_too_low_error(exc)
                and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES
            ):
                transient_retries += 1
                logger.warning(
                    "Nonce sync race for executeSwaps (retry %s/%s). Re-syncing nonce after %.1fs",
                    transient_retries,
                    DELEGATED_INFLIGHT_MAX_RETRIES,
                    PENDING_NONCE_POLL_SECONDS,
                )
                time.sleep(PENDING_NONCE_POLL_SECONDS)
                continue

            result["status"] = "error"
            result["error"] = f"executeSwaps tx failed: {exc}"
            result["approvals"] = approval_results
            return result

    if receipt is None or swap_tx_hash is None:
        result["status"] = "error"
        result["error"] = "executeSwaps tx failed: delegated-account retries exhausted"
        result["approvals"] = approval_results
        return result

    if receipt.status != 1:
        result["status"] = "error"
        result["error"] = "executeSwaps tx reverted"
        result["tx_hash"] = swap_tx_hash
        result["approvals"] = approval_results
        return result

    if len(receipt.logs) == 0:
        result["status"] = "error"
        result["error"] = "executeSwaps mined with zero logs (probable no-op)"
        result["tx_hash"] = swap_tx_hash
        result["approvals"] = approval_results
        return result

    # Step 3: measure USDC received.
    # Primary method: parse Transfer events from the receipt logs.  This is
    # immune to RPC latency — the receipt already contains the final chain
    # state for this tx, so we don't need a fresh balanceOf call that may
    # read from a node that hasn't propagated the block yet.
    TRANSFER_TOPIC = (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )
    usdc_addr_lower = usdc_addr.lower()
    recipient_addr_lower = recipient_addr.lower()
    usdc_delta = 0
    for log_entry in receipt.logs:
        if (
            log_entry.address.lower() == usdc_addr_lower
            and len(log_entry.topics) == 3
            and log_entry.topics[0].hex() == TRANSFER_TOPIC
            and log_entry.topics[2].hex()[-40:].lower() == recipient_addr_lower[-40:]
        ):
            usdc_delta += int(log_entry.data.hex(), 16)

    # Fallback: if no Transfer logs matched (e.g. USDC contract emits non-standard
    # events), fall back to the balance delta read at the confirmed block number.
    if usdc_delta == 0:
        try:
            usdc_after = usdc_contract.functions.balanceOf(signer_addr).call(
                block_identifier=receipt.blockNumber
            )
            usdc_delta = usdc_after - usdc_before
            logger.info(
                "USDC Transfer log not found — using balance delta at block %d: %d raw (%s USDC)",
                receipt.blockNumber,
                usdc_delta,
                f"{usdc_delta / 1_000_000:.6f}",
            )
        except Exception as _bal_err:
            logger.warning("USDC balance fallback failed: %s", _bal_err)

    logger.info(
        "USDC received by recipient %s: %d raw (%s USDC)",
        recipient_addr,
        usdc_delta,
        f"{usdc_delta / 1_000_000:.6f}",
    )

    result.update(
        {
            "status": "success",
            "tx_hash": swap_tx_hash,
            "usdc_recipient": str(recipient_addr),
            "usdc_received_raw": usdc_delta,
            "usdc_received": usdc_delta / 1_000_000,
            "approvals": approval_results,
            "unroutable": [
                {"token": i["token"], "symbol": i["symbol"]} for i in unroutable_intents
            ],
            "legs": [
                {
                    "from": s["fromTokenAddress"],
                    "to": s["toTokenAddress"],
                    "amountIn": s.get("amountIn"),
                    "amountOut": s.get("amountOut"),
                    "source": s.get("source"),
                }
                for s in quote.get("swaps", [])
            ],
        }
    )

    # Step 4: forward USDC to recipient if it differs from signer
    if not recipient_is_signer and usdc_delta > 0:
        logger.info(
            "Forwarding %s USDC to recipient %s…",
            f"{usdc_delta / 1_000_000:.6f}",
            recipient_addr,
        )
        wait_for_pending_nonce_drain(
            w3, signer_addr, PENDING_NONCE_WAIT_SECONDS, PENDING_NONCE_POLL_SECONDS
        )
        nonce = w3.eth.get_transaction_count(signer_addr, "pending")
        gas_price = w3.eth.gas_price
        forward_tx = usdc_contract.functions.transfer(
            recipient_addr, usdc_delta
        ).build_transaction(
            {
                "from": signer_addr,
                "chainId": CHAIN_ID,
                "nonce": nonce,
                "gas": 120_000,
                "gasPrice": gas_price,
            }
        )
        try:
            fwd_hash, fwd_status = send_contract_transaction(w3, signer, forward_tx)
            if fwd_status != 1:
                raise RuntimeError("USDC forward transfer reverted")
            result["forward_tx_hash"] = fwd_hash
            logger.info("USDC forward tx=%s", fwd_hash)
        except Exception as exc:
            result["forward_error"] = str(exc)
            logger.error("USDC forward failed (swaps succeeded): %s", exc)
    elif recipient_is_signer:
        logger.info("Recipient is signer — no forward needed")

    return result
