"""Guards against prototype/diagnostic code creeping back into the tree.

Contract under test: every ``.py`` file under ``src/``, ``scripts/``, ``analysis/`` and
``data/`` (other than ``__init__.py``) must be reachable from one of the weekly
procedure's kept entry points, listed below exactly as the retire-prototype-modules
work order's "Settled decisions" table names them (source: docs/OPERATIONS_RUNBOOK.md,
docs/VALIDATION_COMMANDS.md, docs/LOCAL_DB_BACKUP_RUNBOOK.md, data/fetchers/README.md,
and the scripts those launch).

Reachability is computed without importing anything: this file parses each source file
with ``ast`` for ``import``/``from ... import ...`` statements, and separately collects
every string-literal *value* in the file (via ``ast``, not raw text) to catch a file used
without being imported (``subprocess``, a hardcoded path built from
``os.path.join(..., "some_script.py")``). Module/function/class docstrings are excluded
from that string-literal scan, so a comment or docstring merely mentioning a filename
(e.g. "we used to call src/dead_helper.py") does not count as a use -- only a string
literal actually embedded in code (a subprocess arg, a path-join component, ...) does.
Shell entry points have no AST, so their raw text is scanned instead; the kept shell
scripts are short and reviewed by hand, so that looseness is accepted there.

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


def _all_py_files() -> list[str]:
    files = []
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            if p.name == "__init__.py":
                continue
            files.append(p.relative_to(ROOT).as_posix())
    return sorted(files)


def _module_name(relpath: str) -> str:
    """ "data/fetchers/fetch_boundary_votes.py" -> "data.fetchers.fetch_boundary_votes"."""
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


def _docstring_literal_ids(tree: ast.AST) -> set[int]:
    """id() of every Constant node that is a module/function/class docstring."""
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))
    return docstring_ids


def _string_literals(tree: ast.AST) -> set[str]:
    """Every string-literal value in `tree`, excluding docstrings.

    Docstrings are excluded deliberately: a comment or docstring that merely mentions a
    filename ("we used to call src/dead_helper.py") must not count as a use, or this
    guard would stay green on exactly the dead code it exists to catch.
    """
    docstring_ids = _docstring_literal_ids(tree)
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_ids:
                continue
            literals.add(node.value)
    return literals


def _referenced_as_string(literals: set[str], all_files: list[str]) -> set[str]:
    """Files whose dotted module path or .py basename appears inside a string literal."""
    hits = set()
    for relpath in all_files:
        basename = Path(relpath).name
        dotted = _module_name(relpath)
        for literal in literals:
            if basename in literal or dotted in literal:
                hits.add(relpath)
                break
    return hits


def _compute_reached() -> tuple[set[str], list[str]]:
    all_files = _all_py_files()
    reached: set[str] = set()
    worklist: list[str] = []

    for entry in ENTRY_POINTS:
        if entry.endswith(".py"):
            reached.add(entry)
            worklist.append(entry)
        else:
            # Shell entry points have no AST; their raw text can still name python
            # files by path (e.g. "scripts/export_boundary_optimal_allocation.py").
            text = (ROOT / entry).read_text(encoding="utf-8")
            for relpath in all_files:
                if Path(relpath).name in text or _module_name(relpath) in text:
                    if relpath not in reached:
                        reached.add(relpath)
                        worklist.append(relpath)

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

        literals = _string_literals(tree)
        for hit in _referenced_as_string(literals, all_files):
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
    missing = [e for e in ENTRY_POINTS if not (ROOT / e).is_file()]
    assert not missing, f"Kept entry points are missing from disk: {missing}"


def test_docstring_mention_does_not_count_as_a_string_reference():
    """A comment-like mention in a docstring must not prove a file is used.

    Regression test for a code-review finding against this file: the first version of
    _referenced_as_string scanned raw file text, so a docstring reading "we used to call
    src/dead_helper.py" was enough to mark dead_helper.py reachable even though nothing
    imports or invokes it. That made the guard fail silently on exactly the dead code it
    exists to catch.
    """
    tree = ast.parse(
        '"""We used to call src/dead_helper.py here, but stopped."""\n' "import os\n"
    )
    literals = _string_literals(tree)
    assert not _referenced_as_string(literals, ["src/dead_helper.py"])


def test_code_string_literal_does_count_as_a_string_reference():
    """A filename embedded in an actual code string (not a docstring) must still count.

    This is the real pattern the kept entry points use, e.g.
    scripts/boundary_monitor.py building `os.path.join(..., "fetch_cg_ref_prices.py")`
    before a subprocess call.
    """
    tree = ast.parse(
        "import os\n"
        'script = os.path.join(os.path.dirname(__file__), "dead_helper.py")\n'
    )
    literals = _string_literals(tree)
    assert _referenced_as_string(literals, ["src/dead_helper.py"]) == {
        "src/dead_helper.py"
    }
