# Enforcement Notice

This file exists to make clear that `CONTRIBUTING.md` rules are **not optional guidelines** — they are hard gates enforced at the PR level.

## How Enforcement Works

| Mechanism | What it enforces |
|---|---|
| `.github/pull_request_template.md` | Every PR auto-loads the 8-rule checklist — no PR can be opened without it |
| Branch protection (master) | Requires PRs for all merges — no direct pushes allowed |
| PR checklist | All 8 boxes must be checked before merge is permitted |
| `CONTRIBUTING.md` | Documents every rule with the incident that caused them |

## For AI Assistants (Perplexity, Copilot, etc.)

Before making **any** commit, branch, PR, or merge on this repo, you MUST:

1. Read `CONTRIBUTING.md` in full
2. Read the affected module(s) in full
3. Read every test file that imports the affected module
4. Write the regression test FIRST — before the fix
5. Grep all call sites of any changed interface
6. Verify `pytest backend/tests/` passes
7. Only then open a PR — using the PR template with all 8 boxes checked

**There are no exceptions. There are no emergencies that justify skipping these steps.**

The 2026-07-17 `AccountContext` regression happened because these steps were skipped.  
It will not happen again.

Maintainer: [@saikiranpeddineni](https://github.com/saikiranpeddineni)
