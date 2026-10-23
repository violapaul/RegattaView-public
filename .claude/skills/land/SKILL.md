---
name: land
description: >-
  Land an agent worktree's changes onto main and clean up. Commits any pending
  changes in the agent/<topic> worktree, fast-forwards main to that branch, then
  removes the worktree and deletes the branch. Use when the user says to land,
  merge, finish, or ship a worktree; "get the changes into main"; "merge and
  delete the branch/worktree"; or clean up after worktree work. Follows this
  repo's worktree convention (agent/<topic> branch ↔ ../RegattaView-<topic>).
---

# Land a worktree onto `main`

Turns the repo's "worktree by default" workflow (see `CLAUDE.md` → *Git Workflow*)
into one deterministic landing step: **commit → `--ff-only` merge → delete branch
+ worktree.** Never rebases, force-merges, or pushes unless explicitly told to.

## Inputs

- **Optional argument:** the topic, branch (`agent/<topic>`), or worktree path to
  land. If omitted:
  - If the current directory is inside an `agent/<topic>` worktree, use that.
  - Otherwise run `git worktree list` and, if exactly one `agent/*` worktree
    exists, use it; if several, ask the user which one.

## Procedure

Run these from a shell. `MAIN` = the primary checkout (first line of
`git worktree list`); `WT` = the target worktree path; `BR` = `agent/<topic>`.

1. **Resolve the target.** Establish `WT` and `BR`:
   ```bash
   git worktree list                 # MAIN is the first entry
   git -C "$WT" rev-parse --abbrev-ref HEAD   # confirm this is BR (agent/<topic>)
   ```
   Confirm `BR` really starts with `agent/`. If not, stop and ask — this skill is
   only for the agent-worktree convention.

2. **Commit pending changes in the worktree.** The repo's default leaves
   worktree changes untracked, so usually there's something to commit:
   ```bash
   git -C "$WT" status --short
   ```
   - If there are uncommitted/untracked changes: review the diff
     (`git -C "$WT" diff` and `git -C "$WT" diff --cached`), propose a
     Conventional-Commits message, and **confirm the message with the user**. If
     the diff spans clearly separate concerns, ask whether to split into multiple
     commits. Then:
     ```bash
     git -C "$WT" add -A
     git -C "$WT" commit -m "<type(scope): summary>" -m "<body>" \
       -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
     ```
   - If the worktree is already clean (work was committed during the session),
     skip to step 3.

3. **Fast-forward `main`.** The merge must run on `main` in the main checkout:
   ```bash
   git -C "$MAIN" merge --ff-only "$BR"
   ```
   - **If `--ff-only` fails** (main moved since the branch point): **stop. Do not
     rebase, force, or `git pull`.** Surface the failure to the user and let them
     decide (they may want to review the divergence, rebase deliberately, or
     resolve by hand). Leave the branch and worktree intact.

4. **Verify it landed:**
   ```bash
   git -C "$MAIN" merge-base --is-ancestor "$BR" HEAD && echo "landed" || echo "NOT landed"
   ```

5. **Clean up** (only after step 4 confirms it landed):
   ```bash
   git worktree remove "$WT"     # no --force; if it refuses, surface why
   git branch -d "$BR"           # lowercase -d (safe); refuses if unmerged
   ```
   - `git worktree remove` without `--force` refuses if the worktree has stray
     changes — that's a safety net; report it rather than forcing.
   - `git branch -d` (not `-D`) refuses to delete an unmerged branch — another
     safety net. If it refuses after a confirmed ff-only merge, stop and
     investigate; don't escalate to `-D`.

6. **Report:** the landed commit(s), that `main` fast-forwarded, and that the
   branch + worktree were removed.

## Guardrails

- **Never** push (`git push`) unless the user explicitly asks.
- **Never** `--force`, `-D`, rebase, or force-update `main`. Every command here is
  the safe, refusing variant on purpose.
- Run the merge and the worktree removal from the **main checkout**, not from
  inside the worktree (you can't remove a worktree you're standing in, and
  `--ff-only` advances the branch checked out in the current worktree).
- If anything is ambiguous (which worktree, what commit message, single vs.
  multiple commits), ask before acting — landing is intentional, not reflexive.
