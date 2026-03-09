---
description: "Create a new Hydrex operational Python script scaffold with logging, Rich CLI output, argument parsing, safety defaults, and validation checklist."
name: "Create Hydrex Operational Script"
argument-hint: "Target path + purpose, e.g. scripts/new_monitor.py monitor gauge rewards"
agent: "agent"
---

Create a production-ready Hydrex operational Python script at the requested target path.

Requirements:

- Follow repository instruction files, especially:
  - [Hydrex Python Operations](../instructions/hydrex-python-operations.instructions.md)
  - [Hydrex Python Tests](../instructions/hydrex-python-tests.instructions.md)
  - [Hydrex Docs and Runbook Sync](../instructions/hydrex-docs-runbook-sync.instructions.md)
- Add:
  - module docstring with purpose and usage examples
  - `argparse` CLI with safe defaults
  - module-level logger (`logging.getLogger(__name__)`)
  - Rich console output for user-facing status/results
  - input validation and clear exception messages
  - dry-run-first flow for any side effects
- Reuse existing config and data-access layers; do not duplicate environment parsing or direct SQL patterns.
- If behavior introduces or changes operator workflow, update `VALIDATION_COMMANDS.md` with a runnable example.

Output format:

1. Files created/updated.
2. Short rationale for major design choices.
3. Suggested validation command(s).
