---
description: "Use when running terminal commands in Hydrex to keep command execution and output parsing stable in this environment."
name: "Hydrex Terminal Reliability"
applyTo: "**/*"
---

# Hydrex Terminal Reliability

- Prefer short commands over long chained one-liners; split complex flows into multiple commands.
- Do not use shell heredocs for multi-line Python in this repository. Write Python to a script file and run it directly.
- For ad-hoc checks, prefer short `python -c` commands.
- For repeatable diagnostics, create a small script under `scripts/` (for example `_diagnostic_*.py`) and execute it directly.
- Prefer `rg` for file and text search.
- When command output is large, pipe through `head`, `tail`, or focused filters to keep output readable.
- If terminal formatting is corrupted, suggest running `fixterm` before retrying commands.
- Avoid assumptions about shell state; use explicit paths for scripts and key files when practical.
