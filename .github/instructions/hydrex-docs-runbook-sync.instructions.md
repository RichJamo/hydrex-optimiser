---
description: "Use when editing operational docs, runbooks, README command sections, or validation command docs in Hydrex. Enforces command accuracy and script-doc synchronization."
name: "Hydrex Docs and Runbook Sync"
applyTo: "docs/**/*.md, README.md, GETTING_STARTED.md, VALIDATION_COMMANDS.md, HARDENING_SUMMARY.md"
---

# Hydrex Docs and Runbook Sync

- Treat docs as operational source-of-truth: command examples must match current script flags and behavior.
- When script interfaces or defaults change, update affected command examples in `VALIDATION_COMMANDS.md` and relevant runbooks in the same change.
- Prefer copy-paste-safe command blocks with explicit env vars and representative arguments.
- Call out safety-critical modes clearly (`--dry-run`, broadcast flags, destructive `--apply` style actions, resume/no-resume behavior).
- Keep pre-flight check steps current for automation flows (RPC, DB, wallet/address, voting power, boundary timing assumptions).
- Do not leave stale file paths, script names, or deprecated flags in docs.
- If behavior is environment-dependent, document assumptions and expected prerequisites next to the command.
- Keep updates minimal but complete: change only impacted sections while preserving existing document structure.
