# Mandatory Code Modification & Historical Preservation Policy (Hard Rule)

This rule is mandatory and must be strictly referenced and enforced on EVERY code review, refactoring, bug fix, optimization, and modification in this repository.

## 1. Mandatory Comment & Intent Analysis Before Making Any Change
* Before editing, modifying, or touching any line of code, the agent **MUST thoroughly read and analyze all existing comments, docstrings, annotations, and commit notes** surrounding that code.
* The agent **MUST determine why the original change was made**:
  * What specific bug, market condition, incident, edge case, or quant research finding led to this code?
  * What failure mode did it protect against?
* If a comment documents a past incident, bug fix, or statistical finding (e.g. `# Evidence 2026-08-24: 3/3 gap_and_go shorts losers`, `# fix(#61): bind checkpoint restoration`), the agent must strictly account for and preserve that invariant. **Never break, overwrite, or discard historical intent.**

## 2. Never Delete Replaced Code — Preserve by Commenting Out
* **DO NOT DELETE existing or legacy code outright when modifying or replacing it.**
* When altering logic, refactoring an algorithm, adjusting a calculation, fixing a defect, or replacing a function block:
  1. **Comment out the previous code in place** instead of deleting it.
  2. Clearly delineate the previous implementation so future developers, agents, and auditors can immediately inspect what was previously running and compare the behavior.
  3. Write the new implementation directly adjacent to the commented-out legacy block.

### Example Format:
> Canonical example: commented-out previous block delineated by `PREVIOUS IMPLEMENTATION` / `NEW IMPLEMENTATION` markers with what/why/how comments (see git history for instances).

## 3. High-Standard Explanatory Comments on New Code
* Every change must include detailed, institutional-grade comments directly above or alongside the new code:
  * **What** the new code does.
  * **Why** the change was necessary (the exact deficiency, numerical instability, or limitation in the previous implementation).
  * **How** the new implementation resolves the issue while preserving the invariants of the original code.
* Do not leave unannotated or mysterious diffs. Every modification must be self-explanatory to an institutional quant auditor.
