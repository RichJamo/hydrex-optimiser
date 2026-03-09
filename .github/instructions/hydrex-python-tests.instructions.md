---
description: "Use when creating or editing Python tests for Hydrex scripts, analysis modules, optimizer logic, or integration paths. Enforces deterministic tests, safety-case coverage, and repository test conventions."
name: "Hydrex Python Test Conventions"
applyTo: "**/test_*.py, **/*_test.py, p*_integration_test.py, tests/**/*.py"
---

# Hydrex Python Test Conventions

- Keep tests deterministic: avoid wall-clock dependence, live RPC dependence, and mutable external state unless a test is explicitly marked as live/integration.
- Prefer fixtures and shared helpers for repeated setup; avoid large inline setup blocks duplicated across test files.
- Validate operational safety behavior, not only happy paths: include tests for dry-run defaults, explicit broadcast opt-in, and invalid input rejection.
- Cover edge conditions for epoch/boundary logic (pre-boundary, exact-boundary, post-boundary) and assert expected abort/fallback behavior.
- Mock network/subgraph/database boundaries where possible so unit tests validate logic in isolation.
- Use clear assertion messages and stable expected values for allocations, normalization, rounding, and token amount handling.
- For CLI-oriented scripts, test argument parsing and key command outputs/errors for invalid or missing flags.
- Add regression tests for bugs fixed in voting, reward, or allocation logic to prevent silent behavioral drift.
- Keep test names explicit about scenario and expected outcome.
- When test behavior changes expected operational workflows, align examples in `VALIDATION_COMMANDS.md`.
