# Alpha-Bot — Contribution Rules

> These rules apply to **all contributors**, including AI assistants (Perplexity, Copilot, etc.).  
> No exceptions. No shortcuts. No merges without full compliance.

---

## ⛔ Hard Rules — AI & Human Contributors

### Rule 1 — Read Before You Touch
Before modifying any module, **read the full file and every existing test that imports it**.  
Do not infer behaviour from commit messages or partial diffs alone.

### Rule 2 — Write the Regression Test First (TDD)
For every bug fix or interface change:
1. Write a test that **reproduces the exact failure** on the pre-fix code.
2. Confirm the test **fails** before the fix is applied.
3. Apply the fix so the test **passes**.
4. The test must live in `backend/tests/` and be named `test_<NNN>_<slug>.py`.

> ❌ No fix commit without a corresponding test commit in the same PR.

### Rule 3 — Audit All Call Sites
Whenever a function or class signature changes (new required argument, renamed parameter, removed field):
- Search **the entire repo** for every call site using `grep` or GitHub code search.
- List all affected files in the PR description.
- Update or add tests for **every** affected call site — not just the one that surfaced the error.

### Rule 4 — No Direct Push to `master`
- All changes go through a **feature/fix branch** → PR → squash-merge.
- Branch naming: `fix/<slug>`, `feat/<slug>`, `chore/<slug>`.
- The PR description must include: root cause, affected files, test coverage added.

### Rule 5 — PR Checklist (must be complete before merge)
```
- [ ] Existing tests pass locally (`pytest backend/tests/`)
- [ ] New regression test added that covers this exact failure
- [ ] All call sites of changed interface updated
- [ ] PR description includes: root cause, affected files, tests added
- [ ] No secrets or credentials committed (secret scan passed)
```

### Rule 6 — Interface Changes Require a Test Update Pass
If `AccountContext`, `RiskManager`, `PortfolioManager`, or any other shared dataclass/class signature changes:
- Run `grep -r "AccountContext(" backend/` (or equivalent) before opening the PR.
- Every instantiation site must be verified and updated.
- `conftest.py` fixtures must be updated to match the new signature.

### Rule 7 — Never Merge a Broken Test Suite
If any existing test fails after your change:
- Fix the test **or** fix the code — do not merge with a failing test.
- Do not mark tests as `xfail` or `skip` to unblock a merge without a documented reason.

### Rule 8 — Commit Message Format
```
<type>(<scope>): <short description> (#<issue>)

<body: root cause and what changed>
```
Types: `fix`, `feat`, `chore`, `docs`, `test`, `refactor`.

---

## Why These Rules Exist

On 2026-07-17, an AI assistant (Perplexity) merged a fix for `AccountContext` missing `rolling_day_trades` in `engine.py` **without**:
- Writing a regression test first
- Auditing all other `AccountContext` call sites
- Verifying the existing test suite against the patched code

This caused silent regression risk across `test_risk.py`, `test_engine.py`, and `conftest.py`.  
These rules exist to ensure this never happens again.

---

## Enforcement

Any AI assistant operating on this repo **must read this file at the start of every session** before making any commits, PRs, or merges.

Maintainer: [@saikiranpeddineni](https://github.com/saikiranpeddineni)
