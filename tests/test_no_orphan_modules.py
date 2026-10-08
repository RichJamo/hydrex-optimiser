"""Guards against prototype/diagnostic code creeping back into the tree.

Contract under test: every ``.py`` file under ``src/``, ``scripts/``, ``analysis/`` and
``data/`` (other than ``__init__.py``) must be reachable from one of the weekly
procedure's kept entry points, listed below exactly as the retire-prototype-modules
work order's "Settled decisions" table names them (source: docs/OPERATIONS_RUNBOOK.md,
docs/VALIDATION_COMMANDS.md, docs/LOCAL_DB_BACKUP_RUNBOOK.md, data/fetchers/README.md,
and the scripts those launch).

Reachability is computed without importing anything: this file parses each source file
with ``ast`` for ``import``/``from ... import ...`` statements, and separately scans raw
file text for any other reached file's dotted module path or ".py" basename, since a
file can be used without being imported (``subprocess``, a shell script, a hardcoded
path built from ``os.path.join(..., "some_script.py")``).

A file with no reachable path from a kept entry point is dead: nothing in the weekly
procedure runs it. That is the condition this test exists to catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = ("src", "scripts", "analysis", "data")

# Exactly the "Kept entry points" from the work order's Settled decisions table.
ENTRY_POINTS = [
    "scripts/boundary_monitor.py",
    "scripts/auto_voter.py",
    "scripts/fetch_cg_ref_prices.py",
    "scripts/refresh_token_prices.py",
    "scripts/claim_and_swap_rewards.py",
    "scripts/run_postmortem_review.py",
    "scripts/set_epoch_boundary_manual.py",
    "scripts/repair_token_metadata.py",
    "scripts/export_boundary_optimal_allocation.py",
    "scripts/preboundary_epoch_review.py",
    "scripts/record_actual_rewards.py",
    "scripts/audit_routing_price_divergence.py",
    "scripts/measure_token_liquidity.py",
    "scripts/repo_cleanup_audit.py",
    "scripts/weekly_allocation_review.py",
    "scripts/shell/run_preboundary_analysis_pipeline.sh",
    "scripts/shell/run_auto_voter_safe.sh",
    "scripts/shell/backup_local_db.sh",
    "data/fetchers/fetch_live_snapshot.py",
    "data/fetchers/sync_gauges.py",
    "data/fetchers/fetch_epoch_bribes_multicall.py",
    "data/fetchers/fetch_boundary_votes.py",
    "data/fetchers/fetch_preboundary_snapshots.py",
]

# PARKED (see the delegated run's RUN-REPORT and runlog): data/fetchers/README.md lists
# these three as "Active Scripts" / canonical pipeline steps, but none is in the work
# order's kept-entry-point list above, none is imported by any kept file, and the
# runbooks never invoke them directly or by path. That is a live document naming a file
# outside the kept list, which the work order says to park rather than guess about.
# Treated as extra roots so the files stay (undeleted) without this test asserting they
# are actually used by the weekly procedure -- that call is Rich's, not this run's.
PARKED_ROOTS = [
    "data/fetchers/fetch_epoch_boundaries.py",
    "data/fetchers/fetch_gauge_bribe_mapping.py",
    "data/fetchers/init_preboundary_schema.py",
]


def _all_py_files() -> list[str]:
    files = []
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            if p.name == "__init__.py":
                continue
            files.append(p.relative_to(ROOT).as_posix())
    return sorted(files)


def _module_name(relpath: str) -> str:
    """"data/fetchers/fetch_boundary_votes.py" -> "data.fetchers.fetch_boundary_votes"."""
    return ".".join(Path(relpath).with_suffix("").parts)


def _module_to_relpath(dotted: str) -> str | None:
    """Resolve a dotted module path to a real file under SCAN_DIRS, if one exists."""
    top = dotted.split(".", 1)[0]
    if top not in SCAN_DIRS:
        return None
    rel = Path(*dotted.split("."))
    as_module = rel.with_suffix(".py")
    if (ROOT / as_module).is_file():
        return as_module.as_posix()
    as_package = rel / "__init__.py"
    if (ROOT / as_package).is_file():
        return as_package.as_posix()
    return None


def _imported_modules(tree: ast.AST) -> set[str]:
    """Every dotted module path a file's import statements could resolve to."""
    candidates: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                candidates.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # no relative imports exist under SCAN_DIRS today
            if not node.module:
                continue
            candidates.add(node.module)
            for alias in node.names:
                # "from data.fetchers import sync_gauges" -- sync_gauges may be a
                # submodule file rather than an attribute of fetchers/__init__.py.
                candidates.add(f"{node.module}.{alias.name}")
    return candidates


def _referenced_as_string(text: str, all_files: list[str]) -> set[str]:
    """Files whose dotted module path or .py basename appears literally in `text`."""
    hits = set()
    for relpath in all_files:
        basename = Path(relpath).name
        dotted = _module_name(relpath)
        if basename in text or dotted in text:
            hits.add(relpath)
    return hits


def _compute_reached() -> tuple[set[str], list[str]]:
    all_files = _all_py_files()
    reached: set[str] = set()
    worklist: list[str] = []

    for entry in ENTRY_POINTS + PARKED_ROOTS:
        if entry.endswith(".py"):
            reached.add(entry)
            worklist.append(entry)
        else:
            # Shell entry points aren't scanned by ast, but their text can still name
            # python files by path (e.g. "scripts/export_boundary_optimal_allocation.py").
            text = (ROOT / entry).read_text(encoding="utf-8")
            for hit in _referenced_as_string(text, all_files):
                if hit not in reached:
                    reached.add(hit)
                    worklist.append(hit)

    while worklist:
        relpath = worklist.pop()
        full_path = ROOT / relpath
        text = full_path.read_text(encoding="utf-8")

        tree = ast.parse(text, filename=relpath)
        for dotted in _imported_modules(tree):
            resolved = _module_to_relpath(dotted)
            if resolved and resolved not in reached:
                reached.add(resolved)
                worklist.append(resolved)

        for hit in _referenced_as_string(text, all_files):
            if hit not in reached:
                reached.add(hit)
                worklist.append(hit)

    return reached, all_files


def test_every_module_is_reachable_from_a_kept_entry_point():
    reached, all_files = _compute_reached()
    unreached = sorted(set(all_files) - reached)
    assert not unreached, (
        "These .py files under src/, scripts/, analysis/ or data/ are not reachable "
        "(by import or by string reference) from any kept entry point, and are "
        "therefore not used by the weekly procedure:\n  " + "\n  ".join(unreached)
    )


def test_entry_points_exist():
    missing = [e for e in ENTRY_POINTS + PARKED_ROOTS if not (ROOT / e).is_file()]
    assert not missing, f"Kept entry points are missing from disk: {missing}"
