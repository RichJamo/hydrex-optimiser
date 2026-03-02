# Benchmarks (2026-02-27)

## A) Cleanup audit snapshot

Command:

```bash
venv/bin/python scripts/repo_cleanup_audit.py
```

Key output:

- archived duplicates at root: `0`
- backup artifacts at root: `0`
- unreferenced DB tables in Python: `0`
- weak-reference candidate: `historical_analysis` (referenced only by `src/database.py`)

## B) Auto voter allocation pricing path benchmark

Method:

- compared old per-gauge reward-token lookup loop vs new aggregated SQL pass
- dataset: latest live snapshot in `data/db/data.db`

| Metric          | Old method | New method |            Delta |
| --------------- | ---------: | ---------: | ---------------: |
| Gauges scored   |        131 |        131 |                - |
| Avg runtime (s) |   0.005400 |   0.002525 | **2.14x faster** |
| p95 runtime (s) |   0.005624 |   0.002598 | **2.16x faster** |

## C) Expanded k sweep (10 -> 20)

Command:

```bash
PYTHONUNBUFFERED=1 venv/bin/python scripts/weekly_allocation_review.py \
  --strategy-tag manual \
  --voting-power 1183272 \
  --k-sweep-max 20 \
  --k-sweep-max-combos 500000 \
  --summary-k-mode best-sweep
```

Summary:

- best observed `k`: `12`
- best optimal return: `$745.78`
- executed return in this epoch snapshot: `$0.00` (no executed rows recorded for `strategy=manual`)

Selected points from curve:

|   k | Optimal Return (USD) | Marginal Uplift vs k-1 |
| --: | -------------------: | ---------------------: |
|  10 |               744.60 |                  +2.42 |
|  11 |               745.54 |                  +0.94 |
|  12 |           **745.78** |                  +0.24 |
|  13 |               745.68 |                  -0.10 |
|  14 |               745.55 |                  -0.13 |
|  20 |               744.30 |        -1.25 (vs k=14) |
