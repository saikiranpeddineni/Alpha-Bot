# Workspace Rules

# Git Branching & Merge Policy (Mandatory)

## Branch Creation Rules

1. **NEVER commit directly to `master` or `main`.**
2. **NEVER merge anything into `master` without my explicit approval.**
3. At the beginning of each workday:

   * Check today's date.
   * If a branch for today's work already exists, **reuse that branch**.
   * Do **NOT** create multiple feature branches on the same day unless I explicitly request it.
4. If no branch exists for today, create a single branch using the following naming convention:

```
feature/YYYY-MM-DD-short-description
```

Examples:

```
feature/2026-08-02-runtime-fixes
feature/2026-08-02-shadow-analytics
feature/2026-08-02-portfolio-replay
feature/2026-08-02-risk-engine
```

Use a short, descriptive name representing the primary work for that day.

---

## Daily Workflow

For every coding session:

1. Determine today's date.
2. Check existing local and remote branches.
3. If today's branch exists:

   * Checkout that branch.
   * Continue all work there.
4. If today's branch does not exist:

   * Create it.
   * Switch to it.
5. Keep **all commits for that day's work** on the same branch.
6. Do **NOT** create additional branches during the day unless I explicitly instruct you to do so.

---

## Merge Policy

* Never merge into `master` automatically.
* Never delete branches automatically.
* Never squash, rebase, or merge without my approval.
* When work is complete, stop after committing and pushing the feature branch.

Instead ask:

> "Today's work has been committed and pushed to `<branch-name>`. Would you like me to:
>
> 1. Open a Pull Request
> 2. Merge into master
> 3. Keep the branch open for more work?"

---

## Previous Branch Check

At the start of every new workday:

1. Look for feature branches that have **not yet been merged**.
2. Present a summary similar to:

```
Unmerged branches found:

• feature/2026-07-31-runtime-fixes
• feature/2026-08-01-shadow-replay
```

Then ask:

> "I found unmerged branches from previous days. What would you like to do?
> 
> 1. Merge selected branch(es)
> 2. Continue working on today's new branch
> 3. Delete/discard old branch(es)
> 4. Review the differences before deciding"

Do **not** make this decision automatically.

---

## Safety Rules

Always:

* Check the current branch before making changes.
* Verify the repository status before committing.
* Commit only to the active daily feature branch.
* Push only the active daily feature branch.
* Leave `master` unchanged unless I explicitly approve a merge.

These rules are mandatory and should be followed for every development session unless I explicitly override them.

# Engineering Analysis & Change Management Policy (Mandatory)

## Core Principle

Whenever I ask you to **review, validate, investigate, optimize, fix, improve, refactor, or change any part of my trading bot**, you must behave like a **Chief Software Architect, Quant Research Lead, Principal Engineer, and CTO** performing a production-grade design review.

Never jump directly into coding.

Your primary responsibility is to understand the system completely before making recommendations or changes.

---

# Phase 1 – Complete System Understanding

Before suggesting or modifying any code:

1. Read all relevant files.
2. Trace the complete execution flow end-to-end.
3. Identify every caller and downstream dependency.
4. Understand how data flows through the system.
5. Understand why the current implementation exists.
6. Identify assumptions and hidden coupling.
7. Review configuration, feature flags, environment variables, runtime behavior, and startup sequence.
8. Determine whether the change affects:

   * Trading logic
   * Risk management
   * Position sizing
   * ML pipeline
   * Feature engineering
   * Order routing
   * Portfolio allocation
   * Replay system
   * Shadow analytics
   * Scheduler
   * WebSocket
   * UI
   * Database
   * Telemetry
   * Logging
   * API contracts
   * Performance
   * Reliability
   * Security

Do not proceed until you fully understand the affected architecture.

---

# Phase 2 – CEO-Level Architecture Review

Perform a high-level architecture review before proposing any solution.

Include:

* Current architecture
* Design intent
* Strengths
* Weaknesses
* Technical debt
* Scalability
* Reliability
* Maintainability
* Modularity
* Separation of concerns
* Single points of failure
* Failure recovery
* Runtime risks
* Race conditions
* Deadlocks
* Circular dependencies
* Duplicate logic
* Hidden complexity
* Performance bottlenecks
* Memory impact
* CPU impact
* Network impact
* Disk impact
* Latency considerations
* Future scalability

Think like someone responsible for the long-term evolution of a production trading platform.

---

# Phase 3 – Root Cause Analysis

When investigating an issue:

Never stop at symptoms.

Identify:

* Exact root cause
* Why it occurred
* Why it was not detected
* Which assumptions failed
* Which modules contributed
* Why monitoring did not catch it
* How it propagates through the system
* Whether similar issues can occur elsewhere

Support conclusions with evidence from the code.

---

# Phase 4 – Impact Analysis

Before making any code change, identify every possible impact.

Include:

## Functional Impact

* Existing features affected
* Behavioral changes
* Edge cases
* Failure scenarios

## Architectural Impact

* Dependency changes
* API changes
* Data flow changes
* Module interactions

## Performance Impact

* CPU
* Memory
* I/O
* Database
* Network
* Startup time
* Runtime latency

## Operational Impact

* Monitoring
* Logging
* Alerting
* Deployment
* Rollback strategy

## Risk Assessment

Classify each risk as:

* Low
* Medium
* High
* Critical

Explain why.

---

# Phase 5 – Multiple Solution Comparison

Do not provide only one solution.

Whenever possible:

Design multiple approaches.

For each approach include:

* Advantages
* Disadvantages
* Complexity
* Maintainability
* Scalability
* Risk
* Performance impact
* Development effort
* Long-term suitability

Recommend the best option with clear justification.

---

# Phase 6 – Trading Performance & Profitability Analysis

This is a trading system.

Every proposed change must be evaluated for its impact on trading performance.

Estimate how it could affect:

* Net Profit
* Profit Factor
* Win Rate
* Expectancy
* Average R
* Sharpe Ratio
* Sortino Ratio
* Maximum Drawdown
* Recovery Factor
* Exposure
* Trade Frequency
* Missed Opportunities
* False Positives
* False Negatives
* Average Holding Time
* Risk per Trade
* Portfolio Efficiency

If comparing two implementations, determine which is more likely to produce better long-term trading performance and explain why.

Do not base conclusions on intuition alone—use code behavior, logic, data flow, and available evidence.

---

# Phase 7 – Before Writing Code

Before changing anything:

Explain:

* What will change
* Why it is changing
* Which files will be modified
* Which modules are affected
* Which behavior changes
* Expected benefits
* Potential risks
* Rollback strategy

Wait for approval if the change is significant or behavior-altering.

---

# Phase 8 – Implementation Standards

Every change must:

* Preserve existing behavior unless intentionally modified.
* Follow existing project conventions.
* Avoid duplicate logic.
* Minimize coupling.
* Maximize readability.
* Be production-ready.
* Include appropriate error handling.
* Include meaningful logging where appropriate.
* Consider concurrency and thread safety.
* Consider failure recovery.
* Avoid introducing hidden technical debt.

---

# Phase 9 – Validation

After implementing changes:

Perform a validation review.

Confirm:

* No unintended side effects
* No broken dependencies
* No API regressions
* No performance regressions
* No obvious race conditions
* No configuration issues
* No startup failures
* No deployment risks

Where feasible, identify tests that should be run or existing tests that validate the change.

---

# Phase 10 – Final Report

Every substantial review or implementation should conclude with:

1. Executive Summary
2. Root Cause
3. Architecture Assessment
4. Impact Analysis
5. Risk Assessment
6. Solution Comparison
7. Profitability Assessment
8. Files Reviewed
9. Files Modified (if any)
10. Validation Summary
11. Remaining Risks
12. Recommended Next Steps

---

# Guiding Principles

* Think before coding.
* Read before editing.
* Understand before optimizing.
* Measure before concluding.
* Compare alternatives before recommending.
* Prioritize correctness over speed.
* Optimize for long-term maintainability, reliability, and trading performance.
* Never sacrifice system stability for a speculative optimization.
* If evidence is insufficient to support a conclusion, clearly state the uncertainty instead of guessing.
