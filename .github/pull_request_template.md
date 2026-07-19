## 🔗 Linked Issue
Fixes #

## 📋 Root Cause
<!-- What broke and why? Be specific. -->

## 📁 Affected Files
<!-- List every file changed and why -->
- 

## 🔍 Call Site Audit
<!-- For ANY signature change (new arg, renamed param, removed field): paste grep results -->
```
grep -r "<ChangedClass>(" backend/
```
Results:
- All call sites updated? YES / NO

## ✅ Hard Rules Checklist (CONTRIBUTING.md — mandatory, no exceptions)
- [ ] **Rule 1** — I have read the full file(s) I modified AND every test that imports them
- [ ] **Rule 2** — A regression test is included that FAILS on pre-fix code and PASSES after
- [ ] **Rule 3** — I have grepped all call sites of any changed interface and updated every one
- [ ] **Rule 4** — This is a branch PR (not a direct push to `master`)
- [ ] **Rule 5** — PR description is complete: root cause, affected files, tests added
- [ ] **Rule 6** — If `AccountContext` / `RiskManager` / `PortfolioManager` signature changed, all instantiation sites are verified
- [ ] **Rule 7** — `pytest backend/tests/` passes with zero failures and zero unexpected skips
- [ ] **Rule 8** — Commit message follows `type(scope): description (#issue)` format

> ⛔ Do not merge if any box above is unchecked. These rules exist because of a preventable regression on 2026-07-17. See CONTRIBUTING.md.

## 🧪 Test Evidence
<!-- Paste pytest output or summary showing all tests pass -->
```

```
