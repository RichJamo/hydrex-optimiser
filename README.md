# Hydrex vote optimiser

Hydrex is a vote-escrow DEX on Base. Each week, holders of locked HYDX vote on which
liquidity pools receive emissions, and in return each voter earns a share of every
pool's bribes and trading fees in proportion to its share of that pool's votes. This
repository runs one voter's weekly cycle end to end, for about 1.81 million votes:

1. decide where the votes should go, from live on-chain bribes, votes and prices;
2. cast the vote in the final four minutes before the weekly flip, and re-cast it
   twice as late bribes and competing votes arrive;
3. claim the rewards and swap them to USDC;
4. review the week: what the vote earned, what the best possible allocation would
   have earned in hindsight, and why the two differ.

## Results

Each week's review compares three figures: what the executed vote actually earned,
what that allocation was entitled to at the boundary, and the best allocation possible
with hindsight (the final, on-chain bribes and votes at the flip, which no vote cast
before the flip can see).

| Epoch opened | Rewards received | Per 1,000 votes | Best possible in hindsight | Share of best |
|---|---:|---:|---:|---:|
| 2026-09-17 | $802.33 | $0.44 | $834.76 | 96% |
| 2026-09-24 | $1,533.94 | $0.85 | $1,641.62 | 93% |
| 2026-10-01 | $1,647.47 | $0.91 | $1,704.30 | 97% |
| 2026-10-08 | $1,284.70 | $0.71 | $1,396.57 | 92% |

The review has compared like with like since 2026-09-17: before then its "best
possible" figure included pools the voter is configured never to vote for. Earlier
weeks earned $382.22 (2026-09-03) and $685.17 (2026-09-10).

In the 2026-10-08 epoch the home internet failed before the flip, so a vote cast seven
hours earlier stood in for the final-minutes vote. Most epochs since late April have a
record in [data/epochs/](data/epochs/), with notes on anything unusual that week.

## How a week runs

**Boundary monitor** ([scripts/boundary_monitor.py](scripts/boundary_monitor.py)).
Runs continuously and times the voting phases from on-chain block timestamps. In the hours
before the flip it pre-fetches token prices, so that the final votes do not wait on
price APIs. It then triggers the voter three times: at 240, 60 and 35 seconds before
the flip. Each later phase re-reads bribes and votes and replaces the earlier vote if
the picture has changed.

**Voter** ([scripts/auto_voter.py](scripts/auto_voter.py)). Reads every gauge's bribes
and current votes, prices each reward token, chooses the allocation, simulates the
transaction and sends it. Separately, a manual backup vote is cast hours earlier each
week, so that a monitor failure costs accuracy rather than the whole week's rewards.

**Claim and swap** ([scripts/claim_and_swap_rewards.py](scripts/claim_and_swap_rewards.py)).
Claims every bribe and fee contract through the escrow that owns the voting NFT, then
swaps all reward tokens to USDC in a single routed transaction. It defaults to a dry
run, and it counts the token transfers each claim produced: a run whose claims all
succeed on-chain but move nothing fails loudly instead of reporting success.

**Post-mortem** ([scripts/run_postmortem_review.py](scripts/run_postmortem_review.py)).
Re-reads bribes and votes at the exact boundary block, values them at the prices the
voter saw when it decided, finds the best allocation in hindsight, and reconciles the
expected token amounts against what was actually received, token by token.

The weekly procedure, with commands, is in
[docs/OPERATIONS_RUNBOOK.md](docs/OPERATIONS_RUNBOOK.md).

## The allocation problem

If a pool carries bribes worth `B` dollars and `V` votes from everyone else, putting
`x` of our votes on it earns

```
B * x / (V + x)
```

Returns diminish as `x` grows, so the best split spreads votes across several pools.
The allocator ([src/optimizer.py](src/optimizer.py)) hands out votes in chunks, each
chunk going to the pool where it adds the most expected dollars. It then compares
allocations across 1 to 50 pools and picks the smallest pool count whose expected
return is within a set tolerance of the best, which keeps the vote simple when extra
pools add little.

Most of the engineering is in making the inputs trustworthy rather than in the
optimisation itself:

- **Prices.** Thinly traded reward tokens can quote far from their real value on the
  DEX router. Router quotes are checked against a CoinGecko reference and replaced when
  they diverge by more than 3x; known offenders are priced from CoinGecko only
  ([src/price_feed.py](src/price_feed.py)).
- **Token metadata.** Decimals are read on-chain and cached. A cached default of 18
  once made several 6- and 8-decimal tokens look worthless; a failed read is no longer
  cached, though a run still falls back to 18 for a token whose read fails.
- **Completeness.** The gauge list is synced from the chain before each vote, so new
  pools are considered as soon as they exist.
- **Network failure.** Each monitor check has a hard wall-clock deadline, because a DNS
  lookup during an outage can block for a minute outside any HTTP timeout. While
  healthy, the monitor pings an outside dead-man's switch, so an outage raises an alert
  even when the monitor itself cannot send one.

## Repository layout

```
abi/            Contract ABIs (Voter, Bribe)
config/         Settings, loaded from .env
src/            Shared library: optimizer, price feed, database, voting-power checks
scripts/        Command-line entry points: monitor, voter, claims, post-mortem
  shell/        Scheduling and wrapper scripts
data/fetchers/  On-chain data collection (bribes, votes, boundary snapshots)
data/epochs/    Weekly results: rewards received and post-mortem figures
analysis/       Review outputs (CSVs) from the post-mortem pipeline
docs/           Runbook, command reference, design notes; docs/archive for history
tests/          pytest suite
```

[ARCHITECTURE.md](ARCHITECTURE.md) describes how the components and the database fit
together.

## Setup

Requires Python 3.9 or later.

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env    # then set RPC_URL and the addresses described in the file
```

The voting wallet is passed as a private key or a path to a key file; the claim script
can also read it from 1Password (`--wallet op://vault/item/field`). Every script that
can send a transaction has a dry-run mode. Start there:

```bash
venv/bin/python scripts/auto_voter.py --dry-run --simulation-block latest
venv/bin/python scripts/claim_and_swap_rewards.py --dry-run true
```

More commands, from dry runs to live broadcasts, are in
[docs/VALIDATION_COMMANDS.md](docs/VALIDATION_COMMANDS.md).

## Tests

```bash
venv/bin/python -m pytest
```

The suite runs on every pull request ([.github/workflows/tests.yml](.github/workflows/tests.yml))
and needs no network access; tests that exercise network behaviour use local fake
servers and injected clocks. Code is
formatted with `black`.
