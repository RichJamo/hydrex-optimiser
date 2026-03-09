---
description: "Use when creating or editing operational Python code in scripts, analysis, src, data, or config paths. Enforces mandatory Hydrex optimizer architecture, safety checks, logging, CLI output, and validation workflow."
name: "Hydrex Python Operations Conventions"
applyTo: "scripts/**/*.py, analysis/**/*.py, src/**/*.py, data/**/*.py, config/**/*.py"
---

# Hydrex Python Operations Conventions

- Keep architecture boundaries intact: data fetching writes to SQLite/cache layers, and analysis/optimization code reads from stored data instead of re-fetching on-chain data in analysis paths.
- Centralize runtime configuration in `config/settings.py` and related config modules. Do not introduce new ad-hoc environment variable reads across business logic.
- Use module-level loggers (`logger = logging.getLogger(__name__)`) and structured logging for operations and diagnostics. Do not use `print()` in production or automation flows.
- Validate critical inputs early (epochs, addresses, amounts, file paths, flags) and raise explicit exceptions with actionable error messages.
- For boundary or voting-time logic, use on-chain epoch timing sources and existing boundary-monitor patterns; do not rely only on wall-clock assumptions.
- Preserve operational safety defaults: dry-run first, explicit opt-in for broadcast or destructive actions, and pre-flight checks before side effects.
- Reuse existing DB access layers and models (`src/database.py`, `src/data_access.py`) before adding direct SQL or duplicate access patterns.
- Keep long-running scripts observable: emit progress logs, honor unbuffered output expectations, and make resume behavior explicit when processing ranges.
- Use Rich components (`rich.console.Console`, tables, panels, progress) for user-facing CLI output in scripts and tools.
- When adding or changing scripts, update command examples and verification steps in `VALIDATION_COMMANDS.md` and related runbooks when behavior changes.
- Maintain script docstrings and CLI help text so operators can run commands safely without reading source internals.
