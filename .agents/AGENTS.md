# Workspace Rules

# Git Branching & Merge Policy (Mandatory)

## Core Branching Rules

1. **Active Branch Naming**: All work uses branches starting from `Alphadesk_001`, incrementing sequentially (e.g. `Alphadesk_001`, `Alphadesk_002`, `Alphadesk_003`) ONLY when explicitly instructed.
2. **Persistent Branching Across Days**: Continue on the same active branch (e.g. `Alphadesk_001`) across consecutive days.
3. **NEVER create or switch to a new branch** unless I explicitly instruct you to do so (at which point you will increment to the next sequential number, e.g. `Alphadesk_002`).
4. **NEVER commit directly to `master` or `main`.**
5. **NEVER merge anything into `master`** without my explicit instruction.

---

## Daily & Continuous Workflow

For every coding session and change:
1. Work exclusively on the active branch (e.g. `Alphadesk_001`).
2. Add proper code documentation/comments for any modifications made.
3. Commit the changes to the active branch with clear, descriptive commit messages.
4. Push the commit to the git remote branch.
5. Do NOT merge to master.
6. Seamlessly continue to the next task or change on the same branch.

---

## Post-Merge Workflow

Once an active branch is successfully merged into `master`:
1. Delete the old merged branch both locally and remotely.
2. Create and switch to a new active branch by incrementing the sequential number (e.g., from `Alphadesk_001` to `Alphadesk_002`).
3. Push the new branch to the remote repository.
4. Continue all new work on this newly created branch.

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

