# Epoch records

One file per weekly epoch: what the votes actually earned, and how that compared
with what the optimiser expected.

- `actual_rewards_epoch_<ts>.json`: the rewards received for the epoch that opened
  at `<ts>` (Unix seconds, Thursday 00:00 UTC). `actual_tokens` and `token_prices`
  are the operator's claimable table after removing duplicate lines; `postmortem`
  holds the boundary review figures; `notes` explains anything unusual that week.
  Load into the database with `scripts/record_actual_rewards.py --epoch <ts>`.
- `claim_runs/`: the claim and swap run artifacts kept for selected epochs
  (`scripts/claim_and_swap_rewards.py --output`), with transaction-level detail.

The weekly procedure that produces these files is in
[docs/OPERATIONS_RUNBOOK.md](../../docs/OPERATIONS_RUNBOOK.md).
