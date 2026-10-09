#!/usr/bin/env python3
"""
Claim and Swap Rewards: Phase 1-6 - Discovery, Claim, Swap, Persistence, and Weekly Reporting.

Orchestrates batch reward claiming and USDC swap execution:

PHASE 1: Wallet Integration & Preflight Checks
  - Load wallet from 1Password CLI, file, environment variable, or raw key
  - Validate RPC connectivity, chain ID (8453), signer nonce, gas price
  - Preflight checks ensure safe execution environment

PHASE 2: Claim Target Construction & Discovery
  - Resolve target epoch (auto-detect latest closed or user override)
  - Discover voted gauges from executed_allocations or fallback to alive gauges
  - Map gauges to internal/external bribe contracts via gauges table
  - Enumerate reward tokens from BribeV2 contracts via bulk rewardTokens() queries
  - Build claim summary table (Rich output)
  - Export JSON artifact for Phase 3 (Batch Claim Execution) handoff

Safety Features:
  - Dry-run mode by default (--dry-run=true explicit flag required)
  - Wallet loading with 3-source fallback (1Password → file/env → error)
  - 1Password integration via op CLI subprocess (FileNotFoundError handled)
  - Preflight validation before business logic
  - Read-only discovery phase (no transactions, no DB writes)
  - Comprehensive module-level logging
  - Rich console output for CLI clarity

Configuration:
    - Hydrex Router: 0x6f4bE24d7dC93b6ffcBAb3Fd0747c5817Cea3F9e
  - Hydrex Factory/Deployer: 0x36077D39cdC65E1e3FB65810430E5b2c4D5fA29E
  - VoterV5: 0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b
  - USDC (Base): 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913
  - Dust threshold: $1 USD
  - Slippage: 0.5% starting (retries up to 5% via Phase 4)

Usage (Dry-Run, Read-Only):
  python scripts/claim_and_swap_rewards.py \\
    --wallet "op://vault/item/field" \\
    --epoch 100 \\
    --dry-run true

Usage (Phase 3+ requires explicit --broadcast flag):
  python scripts/claim_and_swap_rewards.py \\
    --wallet "op://vault/item/field" \\
    --epoch 100 \\
    --broadcast true

Next Phase: Phase 3 (Batch Claim Execution)
  - Constructs claimBribes/claimFees batch calls
  - Estimates gas with 1.2× buffer
  - Submits transactions for claim execution
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from typing import Dict, List

from dotenv import load_dotenv
from eth_utils import to_checksum_address
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import VOTER_ABI
from src.claims.wallet import (
    load_wallet,
    load_wallet_from_1password,
    load_wallet_from_file_or_env,
    preflight_checks,
)
from src.claims.reclaim_guard import (
    CLAIM_SOURCE_AUTO,
    CLAIM_SOURCES,
    ERC20_TRANSFER_TOPIC,
    RECLAIM_GUARD_BLOCK,
    RECLAIM_GUARD_FORCED,
    RECLAIM_GUARD_NOT_APPLICABLE,
    RECLAIM_GUARD_PROCEED,
    ClaimMovedNothingError,
    assert_claims_moved_tokens,
    choose_claim_source,
    count_reward_transfers,
    evaluate_reclaim_guard,
    resolve_claim_source,
    summarize_claim_transfers,
)
from src.claims.reporting import (
    generate_weekly_rollup,
    persist_phase_results,
    print_weekly_rollup,
    write_weekly_rollup_csv,
    write_weekly_rollup_json,
)
from src.claims.swaps import (
    build_swap_execution_summary_table,
    build_swap_intents,
    execute_router_batch_swaps,
    execute_swap_intents,
)
from src.claims.discovery import (
    build_claim_summary,
    discover_voted_gauges,
    enumerate_reward_tokens_from_bribes,
    ensure_reward_token_metadata,
    export_claim_artifact,
    map_gauges_to_bribes,
    resolve_manual_claim_gauges,
    resolve_target_epoch,
)
from src.claims.claim_execution import (
    DISTRIBUTOR_ABI,
    ESCROW_ABI,
    VOTER_RECIPIENT_CLAIM_SIGNATURES,
    VOTER_SELF_CLAIM_SIGNATURES,
    build_claim_execution_summary_table,
    execute_claim_batches,
    execute_distributor_claim,
    execute_escrow_claim_rewards,
    invert_reward_tokens_to_bribes,
    preflight_claim_authorization,
    preflight_distributor_claim_authorization,
    voter_claim_call_spec,
)
from config.settings import (
    DATABASE_PATH,
    DUST_THRESHOLD_USD,
    ESCROW_ADDRESS,
    HYDREX_FACTORY_ADDRESS,
    HYDREX_REWARDS_DISTRIBUTOR_ADDRESS,
    HYDREX_ROUTER_ADDRESS,
    HYDREX_SWAP_EXECUTION_MODE,
    RPC_URL,
    SLIPPAGE_START_PCT,
    SWAP_DEADLINE_SECONDS,
    SWAP_RETRY_COUNT,
    USDC_ADDRESS,
    VE_ADDRESS,
    VOTER_ADDRESS,
    WEEK,
)

load_dotenv()

# ═══ Logging Setup ═══
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

console = Console()


def parse_bool(value: str) -> bool:
    """Parse common truthy/falsey CLI values."""
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


# ═══ Main Orchestration ═══
def main():
    """
    Phase 1-4 Orchestration:

    1. Parse arguments
    2. Initialize Web3 connection
    3. Load wallet (1Password → file/env → error)
    4. Run preflight checks
    5. Resolve target epoch
    6. Discover voted gauges
    7. Map gauges to bribes
    8. Enumerate reward tokens
    9. Display claim summary (Rich table)
    10. Export JSON artifact for Phase 3
    """
    parser = argparse.ArgumentParser(
        description="Claim and Swap Rewards: Phase 1-6 (Discovery, Claim, Swap, Persistence, Reporting)"
    )

    parser.add_argument(
        "--wallet",
        type=str,
        default=None,
        help="Wallet source: op://vault/item/field | /path/to/key | $ENV_VAR | raw_key",
    )

    parser.add_argument(
        "--epoch",
        type=int,
        default=None,
        help="Target epoch (auto-detect if not provided)",
    )

    parser.add_argument(
        "--dry-run",
        type=str,
        default="true",
        help="Dry-run mode (default: true). No transactions are broadcast unless --broadcast is set.",
    )

    parser.add_argument(
        "--output",
        type=str,
        default="runs/claim_and_swap_artifact.json",
        help=(
            "Output artifact file (JSON). The default sits in the git-ignored runs/ "
            "folder and is overwritten each run; pass a path to keep a copy"
        ),
    )

    parser.add_argument(
        "--refresh-reward-token-cache",
        action="store_true",
        help="Bypass DB cache and re-enumerate reward tokens from on-chain bribe contracts",
    )

    parser.add_argument(
        "--broadcast",
        action="store_true",
        help="Broadcast Phase 3 claim transactions (default is dry-run only)",
    )

    parser.add_argument(
        "--claim-mode",
        type=str,
        default="all",
        choices=["all", "fees", "bribes"],
        help="Which claim calls to run in Phase 3",
    )

    parser.add_argument(
        "--claim-source",
        type=str,
        default=CLAIM_SOURCE_AUTO,
        choices=[CLAIM_SOURCE_AUTO, *CLAIM_SOURCES],
        help=(
            "Claim source contract for Phase 3. Default 'auto' resolves from veNFT "
            "ownership on-chain: escrow-owned -> escrow, signer-owned or approved -> voter. "
            "VOTE_FROM does not decide this — delegation does not move reward accrual."
        ),
    )

    parser.add_argument(
        "--escrow-address",
        type=str,
        default=ESCROW_ADDRESS,
        help="Escrow contract address for claimRewards(...) (defaults to ESCROW_ADDRESS/MY_ESCROW_ADDRESS env)",
    )

    parser.add_argument(
        "--rewards-distributor-address",
        type=str,
        default=HYDREX_REWARDS_DISTRIBUTOR_ADDRESS,
        help="HydrexRewardsDistributor address (required when --claim-source distributor)",
    )

    parser.add_argument(
        "--distributor-token-id",
        type=int,
        default=None,
        help="ve tokenId to use with distributor claim(tokenId)",
    )

    parser.add_argument(
        "--skip-claims",
        action="store_true",
        help="Skip Phase 3 claim execution and proceed to Phase 4 swap planning/execution",
    )

    parser.add_argument(
        "--claim-batch-size",
        type=int,
        default=20,
        help="Number of bribe contracts per claim transaction batch",
    )

    parser.add_argument(
        "--claim-for",
        type=str,
        default=None,
        help="Address to claim for (defaults to signer address)",
    )

    parser.add_argument(
        "--claim-recipient",
        type=str,
        default=None,
        help="Recipient of claimed tokens (defaults to signer address)",
    )

    parser.add_argument(
        "--gauge-addresses",
        type=str,
        default="",
        help="Optional comma/newline/space separated gauge allowlist for targeted claims",
    )

    parser.add_argument(
        "--pool-addresses",
        type=str,
        default="",
        help="Optional comma/newline/space separated pool-address allowlist for targeted claims",
    )

    parser.add_argument(
        "--enable-swaps",
        action="store_true",
        help="Enable Phase 4 swaps for tokens above dust threshold",
    )

    parser.add_argument(
        "--swap-recipient",
        type=str,
        default=None,
        help="Recipient for USDC output swaps (defaults to signer address)",
    )

    parser.add_argument(
        "--swap-mode",
        type=str,
        default=None,
        choices=["direct", "router-batch"],
        help=(
            "Swap execution mode override: "
            "'direct' = per-token exactInputSingle (legacy default); "
            "'router-batch' = multi-quote + single executeSwaps tx via Hydrex routing API. "
            "Defaults to HYDREX_SWAP_EXECUTION_MODE env var (default: direct)."
        ),
    )

    parser.add_argument(
        "--swap-max-intents",
        type=int,
        default=0,
        help=(
            "Limit Phase 4 swaps to top-N intents by USD value (0 = no limit). "
            "Useful for focused router-batch troubleshooting runs."
        ),
    )

    parser.add_argument(
        "--write-run-log",
        action="store_true",
        help="Persist Phase 3/4 run rows into claim_swap_execution_log table",
    )

    parser.add_argument(
        "--weekly-report",
        action="store_true",
        help="Generate Phase 6 weekly rollup report from claim_swap_execution_log",
    )

    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Run Phase 6 report generation only (skip wallet/RPC/claim/swap steps)",
    )

    parser.add_argument(
        "--report-lookback-days",
        type=int,
        default=7,
        help="Lookback window for weekly report aggregation",
    )

    parser.add_argument(
        "--report-json-output",
        type=str,
        default="weekly_claim_swap_report.json",
        help="Phase 6 JSON rollup output path",
    )

    parser.add_argument(
        "--report-csv-output",
        type=str,
        default="weekly_claim_swap_report_swaps.csv",
        help="Phase 6 CSV swap rollup output path",
    )

    parser.add_argument(
        "--loglevel",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the already-claimed guard and allow re-running Phase 3 for an epoch that has prior success rows",
    )

    args = parser.parse_args()

    # Adjust logging level
    logging.getLogger().setLevel(getattr(logging, args.loglevel))

    logger.info("═══ Claim and Swap Rewards: Phase 1-6 ═══")
    dry_run = (not args.broadcast) or parse_bool(args.dry_run)
    if args.broadcast and parse_bool(args.dry_run):
        dry_run = False

    logger.info(f"Dry-run: {dry_run}")
    logger.info(f"Broadcast enabled: {args.broadcast}")

    if args.report_only:
        logger.info("Phase 6 report-only mode enabled")
        conn = sqlite3.connect(DATABASE_PATH)
        report = generate_weekly_rollup(conn, max(1, args.report_lookback_days))
        print_weekly_rollup(report)
        write_weekly_rollup_json(report, args.report_json_output)
        write_weekly_rollup_csv(report, args.report_csv_output)
        logger.info(
            f"Phase 6 report outputs written: {args.report_json_output}, {args.report_csv_output}"
        )
        conn.close()
        return

    try:
        # Initialize Web3
        logger.info(f"Connecting to RPC: {RPC_URL}")
        w3 = Web3(Web3.HTTPProvider(RPC_URL))

        # Load wallet
        logger.info("Phase 1: Loading wallet...")
        signer = load_wallet(args.wallet)
        signer_address = to_checksum_address(signer.address)

        # Preflight checks
        logger.info("Phase 1: Running preflight checks...")
        preflight_checks(w3, signer)

        # Connect to database
        logger.info(f"Connecting to database: {DATABASE_PATH}")
        if not os.path.exists(DATABASE_PATH):
            raise FileNotFoundError(f"Database not found: {DATABASE_PATH}")

        conn = sqlite3.connect(DATABASE_PATH)

        # Phase 2: Epoch resolution
        logger.info("Phase 2: Resolving target epoch...")
        target_epoch = resolve_target_epoch(conn, args.epoch)

        # F2: Pre-claim epoch guard — abort if Phase 3 claims already completed
        # for this epoch to prevent accidental double-claims.
        _prior_claims = conn.execute(
            """
            SELECT COUNT(*), MAX(run_ts)
            FROM claim_swap_execution_log
            WHERE epoch = ? AND phase = 'phase3_claim' AND status = 'success'
            """,
            (target_epoch,),
        ).fetchone()
        _prior_count = int(_prior_claims[0] or 0)
        _guard_decision, _guard_reason = evaluate_reclaim_guard(
            prior_success_count=_prior_count,
            skip_claims=args.skip_claims,
            force=args.force,
        )
        if _guard_decision == RECLAIM_GUARD_NOT_APPLICABLE:
            logger.info("Epoch %s: %s", target_epoch, _guard_reason)
        elif _guard_decision == RECLAIM_GUARD_FORCED:
            logger.warning("Epoch %s: %s", target_epoch, _guard_reason)
        elif _guard_decision == RECLAIM_GUARD_BLOCK:
            _prior_ts = _prior_claims[1]
            import datetime as _dt

            _prior_dt = _dt.datetime.utcfromtimestamp(_prior_ts).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            console.print(
                f"[bold red]✗ Epoch {target_epoch} already has {_prior_count} Phase 3 claim success "
                f"rows (last run {_prior_dt}). Aborting to prevent double-claim.[/bold red]\n"
                "  Pass [bold]--force[/bold] to override this guard."
            )
            conn.close()
            return

        # Phase 2: Gauge discovery / manual override
        if args.gauge_addresses or args.pool_addresses:
            logger.info("Phase 2: Resolving manual claim target override...")
            gauges = resolve_manual_claim_gauges(
                conn,
                gauge_addresses=args.gauge_addresses,
                pool_addresses=args.pool_addresses,
            )
        else:
            logger.info("Phase 2: Discovering voted gauges...")
            gauges = discover_voted_gauges(conn, target_epoch, signer_address)

        if not gauges:
            console.print(
                Panel(
                    "[yellow]Warning:[/yellow] No gauges found for epoch {target_epoch}",
                    title="Claim Targets",
                )
            )
            conn.close()
            return

        # Phase 2: Bribe mapping
        logger.info("Phase 2: Mapping gauges to bribes...")
        gauge_to_bribes = map_gauges_to_bribes(conn, gauges)

        # Flatten bribe addresses for token enumeration
        all_bribes = set()
        for internal, external in gauge_to_bribes.values():
            if internal:
                all_bribes.add(internal)
            if external:
                all_bribes.add(external)

        # Phase 2: Token enumeration
        logger.info("Phase 2: Enumerating reward tokens...")
        if args.refresh_reward_token_cache:
            logger.info(
                "Reward token cache refresh requested: forcing on-chain enumeration"
            )
            reward_tokens = enumerate_reward_tokens_from_bribes(
                w3,
                conn,
                list(all_bribes),
                force_onchain_refresh=True,
            )
        else:
            reward_tokens = enumerate_reward_tokens_from_bribes(
                w3,
                conn,
                list(all_bribes),
                force_onchain_refresh=False,
            )

        # Display summary
        logger.info("Phase 2: Building claim summary...")
        build_claim_summary(gauges, gauge_to_bribes, reward_tokens)

        claim_results: List[Dict] = []
        if args.skip_claims:
            logger.info("Phase 3 skipped (--skip-claims enabled)")
        else:
            explicit_claim_source = (
                None if args.claim_source == CLAIM_SOURCE_AUTO else args.claim_source
            )
            claim_source, claim_source_reason = resolve_claim_source(
                w3=w3,
                signer_address=signer_address,
                explicit=explicit_claim_source,
                escrow_address=args.escrow_address,
                ve_address=VE_ADDRESS,
            )
            logger.info(
                f"Phase 3: Preparing claims via source={claim_source} ({claim_source_reason})"
            )

            token_by_bribe = invert_reward_tokens_to_bribes(reward_tokens)
            fee_bribes: Dict[str, List[str]] = {}
            external_bribes: Dict[str, List[str]] = {}
            for _, (internal_bribe, external_bribe) in gauge_to_bribes.items():
                if internal_bribe and internal_bribe in token_by_bribe:
                    fee_bribes[internal_bribe] = token_by_bribe[internal_bribe]
                if external_bribe and external_bribe in token_by_bribe:
                    external_bribes[external_bribe] = token_by_bribe[external_bribe]

            if claim_source == "escrow":
                escrow_address = args.escrow_address
                if not escrow_address:
                    raise ValueError(
                        "--escrow-address is required when --claim-source escrow (or set ESCROW_ADDRESS/MY_ESCROW_ADDRESS in .env)"
                    )

                if not fee_bribes and not external_bribes:
                    logger.info(
                        "No fee/bribe addresses discovered for escrow claimRewards"
                    )
                    claim_results = []
                else:
                    escrow_contract = w3.eth.contract(
                        address=to_checksum_address(escrow_address),
                        abi=ESCROW_ABI,
                    )
                    claim_results = execute_escrow_claim_rewards(
                        w3=w3,
                        escrow_contract=escrow_contract,
                        signer=signer,
                        fee_bribes=fee_bribes,
                        external_bribes=external_bribes,
                        claim_mode=args.claim_mode,
                        broadcast=args.broadcast and not dry_run,
                    )

            elif claim_source == "distributor":
                if not args.rewards_distributor_address:
                    raise ValueError(
                        "--rewards-distributor-address is required when --claim-source distributor"
                    )
                if args.distributor_token_id is None:
                    raise ValueError(
                        "--distributor-token-id is required when --claim-source distributor"
                    )

                distributor_contract = w3.eth.contract(
                    address=to_checksum_address(args.rewards_distributor_address),
                    abi=DISTRIBUTOR_ABI,
                )
                # Distributor claims may return a token not present in bribe-derived token cache.
                try:
                    distributor_token_address = to_checksum_address(
                        distributor_contract.functions.token().call()
                    )
                    ensure_reward_token_metadata(
                        w3=w3,
                        conn=conn,
                        reward_tokens=reward_tokens,
                        token_address=distributor_token_address,
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not resolve distributor token() metadata for Phase 4 swaps: {e}"
                    )

                preflight_distributor_claim_authorization(
                    distributor_contract=distributor_contract,
                    signer=signer,
                    token_id=args.distributor_token_id,
                )
                claim_results = execute_distributor_claim(
                    w3=w3,
                    distributor_contract=distributor_contract,
                    signer=signer,
                    distributor_token_id=args.distributor_token_id,
                    broadcast=args.broadcast and not dry_run,
                )
            else:
                claim_recipient = (
                    to_checksum_address(args.claim_recipient)
                    if args.claim_recipient
                    else signer_address
                )
                claim_for = (
                    to_checksum_address(args.claim_for)
                    if args.claim_for
                    else signer_address
                )

                voter_contract = w3.eth.contract(
                    address=to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI
                )
                preflight_claim_authorization(
                    voter_contract=voter_contract,
                    signer=signer,
                    claim_for=claim_for,
                    recipient=claim_recipient,
                    fee_bribes=fee_bribes,
                    external_bribes=external_bribes,
                    claim_mode=args.claim_mode,
                )
                claim_results = execute_claim_batches(
                    w3=w3,
                    voter_contract=voter_contract,
                    signer=signer,
                    claim_for=claim_for,
                    recipient=claim_recipient,
                    fee_bribes=fee_bribes,
                    external_bribes=external_bribes,
                    claim_batch_size=args.claim_batch_size,
                    broadcast=args.broadcast and not dry_run,
                    claim_mode=args.claim_mode,
                )
            build_claim_execution_summary_table(claim_results)

            # A claim that transfers nothing still returns status=1. Without this check
            # a wrong --claim-source is indistinguishable from a working claim.
            try:
                # Name the address the transfers were actually counted against: the
                # Voter path pays --claim-recipient, the escrow/distributor paths pay
                # the signer.
                counted_recipient = (
                    to_checksum_address(args.claim_recipient)
                    if claim_source == "voter" and args.claim_recipient
                    else signer_address
                )
                assert_claims_moved_tokens(
                    claim_results, counted_recipient, claim_source
                )
            except ClaimMovedNothingError as e:
                if args.force:
                    logger.warning("--force passed: %s", e)
                else:
                    console.print(f"[bold red]✗ {e}[/bold red]")
                    conn.close()
                    raise

        # Phase 4: Build and execute swaps
        swap_results: List[Dict] = []
        if args.enable_swaps:
            logger.info("Phase 4: Building swap intents...")
            swap_intents = build_swap_intents(
                w3=w3,
                conn=conn,
                signer_address=signer_address,
                reward_tokens=reward_tokens,
            )
            if args.swap_max_intents and args.swap_max_intents > 0:
                original_count = len(swap_intents)
                swap_intents = swap_intents[: args.swap_max_intents]
                logger.info(
                    "Phase 4: Limiting swap intents to top %d by USD value "
                    "(from %d total)",
                    len(swap_intents),
                    original_count,
                )
            logger.info(f"Phase 4: Generated {len(swap_intents)} swap intents")

            swap_recipient = (
                to_checksum_address(args.swap_recipient)
                if args.swap_recipient
                else signer_address
            )

            # Determine execution mode: CLI flag overrides env/config default
            swap_mode = (args.swap_mode or HYDREX_SWAP_EXECUTION_MODE).strip().lower()
            logger.info("Phase 4 swap execution mode: %s", swap_mode)

            if swap_mode == "router-batch":
                logger.info(
                    "Phase 4: Using router-batch mode (POST /quote/multi + single executeSwaps tx)"
                )
                batch_result = execute_router_batch_swaps(
                    w3=w3,
                    signer=signer,
                    swap_recipient=swap_recipient,
                    intents=swap_intents,
                    broadcast=args.broadcast and not dry_run,
                )
                # Normalise into the same list shape used by the rest of the pipeline
                status = batch_result.get("status", "error")
                swap_results = [
                    {
                        "mode": "router-batch",
                        "symbol": "BATCH",
                        "token": "batch",
                        "status": status,
                        "tx_hash": batch_result.get("tx_hash"),
                        "usdc_recipient": batch_result.get("usdc_recipient"),
                        "usdc_received": batch_result.get("usdc_received"),
                        "usdc_received_raw": batch_result.get("usdc_received_raw"),
                        "error": batch_result.get("error"),
                        "legs": batch_result.get("legs", []),
                        "approvals": batch_result.get("approvals", []),
                        "intents_count": batch_result.get("intents_count"),
                    }
                ]
                # Rich summary table for batch mode
                batch_table = Table(
                    title="Phase 4 Batch Swap Summary", header_style="bold cyan"
                )
                batch_table.add_column("Field")
                batch_table.add_column("Value")
                batch_table.add_row("Mode", "router-batch")
                batch_table.add_row("Status", status)
                batch_table.add_row("Legs", str(len(batch_result.get("legs", []))))
                batch_table.add_row(
                    "USDC Received",
                    (
                        f"{batch_result.get('usdc_received', 0):.6f}"
                        if batch_result.get("usdc_received")
                        else "-"
                    ),
                )
                batch_table.add_row(
                    "USDC Recipient",
                    batch_result.get("usdc_recipient") or swap_recipient,
                )
                batch_table.add_row(
                    "executeSwaps Tx", batch_result.get("tx_hash") or "-"
                )
                if batch_result.get("error"):
                    batch_table.add_row("[red]Error[/red]", batch_result["error"])
                console.print(batch_table)
            else:
                swap_results = execute_swap_intents(
                    w3=w3,
                    signer=signer,
                    swap_recipient=swap_recipient,
                    intents=swap_intents,
                    broadcast=args.broadcast and not dry_run,
                    continue_on_error=True,
                )
                build_swap_execution_summary_table(swap_results)
        else:
            logger.info("Phase 4 swaps disabled (enable with --enable-swaps)")

        # Phase 5: Persistence for weekly review
        run_ts = int(time.time())
        if args.write_run_log:
            persist_phase_results(
                conn=conn,
                run_ts=run_ts,
                epoch=target_epoch,
                claim_results=claim_results,
                swap_results=swap_results,
            )
            logger.info("Phase 5: Persisted run rows to claim_swap_execution_log")
        else:
            logger.info("Phase 5 persistence disabled (enable with --write-run-log)")

        if args.weekly_report:
            report = generate_weekly_rollup(conn, max(1, args.report_lookback_days))
            print_weekly_rollup(report)
            write_weekly_rollup_json(report, args.report_json_output)
            write_weekly_rollup_csv(report, args.report_csv_output)
            logger.info(
                f"Phase 6 report outputs written: {args.report_json_output}, {args.report_csv_output}"
            )

        # Export artifact
        logger.info(f"Exporting claim artifact...")
        export_claim_artifact(
            args.output,
            target_epoch,
            signer_address,
            gauges,
            gauge_to_bribes,
            reward_tokens,
            claim_results=claim_results,
            swap_results=swap_results,
        )

        # Final summary
        summary_text = f"""
    Phase 1-6 Complete: Discovery + Claim + Swap + Persistence + Reporting

Epoch: {target_epoch}
Signer: {signer_address}
Gauges: {len(gauges)}
Reward Tokens: {len(reward_tokens)}
Bribe Contracts: {len(all_bribes)}
Claim Batches: {len(claim_results)}
Swap Results: {len(swap_results)}

Next Phase: Phase 7+ (operational polish)
    - Expand runbook recovery commands
    - Add focused integration tests

Artifact: {args.output}
"""

        console.print(
            Panel(summary_text.strip(), title="✓ Phase 1-6 Complete", style="green")
        )

        logger.info("Phase 1-6 completed successfully")
        conn.close()

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(1)

    except Exception as e:
        logger.error(f"Error in Phase 1-6 flow: {e}", exc_info=True)
        console.print(
            Panel(
                f"[red]Error:[/red] {e}",
                title="Phase 1-6 Failed",
                style="red",
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
