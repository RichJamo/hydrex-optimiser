# Data Fetchers (Canonical)

This folder contains the **active fetch pipeline** for boundary and pre-boundary analysis.

Legacy fetchers were moved to [data/fetchers/archive](data/fetchers/archive) to reduce confusion.

## Canonical Pipeline

Run in this order for a full refresh:

1. `fetch_epoch_boundaries.py`
   - Populates `epoch_boundaries`
   - Source of truth for epoch ↔ boundary block ↔ vote_epoch mapping

2. `fetch_epoch_bribes_multicall.py`
   - Without offsets: writes boundary snapshots to `boundary_reward_snapshots`
   - With `--offset-blocks 1,20`: writes pre-boundary snapshots to `boundary_reward_samples`

3. `fetch_boundary_votes.py`
   - Without offsets: writes boundary votes to `boundary_gauge_values`
   - With `--offset-blocks 1,20`: writes pre-boundary votes to `boundary_vote_samples`

4. `fetch_preboundary_snapshots.py` (optional pre-boundary model pipeline)
   - Writes to `preboundary_*` tables

## Active Scripts

- `fetch_epoch_boundaries.py`
- `fetch_epoch_bribes_multicall.py`
- `fetch_boundary_votes.py`
- `fetch_gauge_bribe_mapping.py`
- `fetch_preboundary_snapshots.py`
- `init_preboundary_schema.py`

## Tables Used by Active Pipeline

- `epoch_boundaries`
- `boundary_reward_snapshots`
- `boundary_gauge_values`
- `boundary_reward_samples`
- `boundary_vote_samples`
- `gauge_bribe_mapping`
- `bribe_reward_tokens`
- `token_metadata`

## Quick Commands

Boundary snapshots:

```bash
PYTHONUNBUFFERED=1 venv/bin/python -m data.fetchers.fetch_epoch_bribes_multicall \
  --all-epochs --ignore-whitelist

PYTHONUNBUFFERED=1 venv/bin/python -m data.fetchers.fetch_boundary_votes \
  --end-epoch 1771459200 --weeks 23 --active-source db
```

Pre-boundary snapshots (1 and 20 blocks before):

```bash
PYTHONUNBUFFERED=1 venv/bin/python -m data.fetchers.fetch_epoch_bribes_multicall \
  --all-epochs --ignore-whitelist --offset-blocks 1,20

PYTHONUNBUFFERED=1 venv/bin/python -m data.fetchers.fetch_boundary_votes \
  --end-epoch 1771459200 --weeks 23 --active-source db --offset-blocks 1,20
```

## Archived Fetchers

Moved to [data/fetchers/archive](data/fetchers/archive):

- `fetch_bribes.py`
- `fetch_votes.py`
- `fetch_ve_state.py`
- `fetch_epoch_bribes.py`
- `fetch_boundary_snapshots.py`

These are retained for history/reference only and are not part of the current workflow.
