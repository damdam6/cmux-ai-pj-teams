---
name: pj-simplify
description: >
  Simplify a pj task's implemented code before review while preserving behavior, public
  interfaces, and the agreed design. Called by pj-work when pj-plan selected Simplifier: run,
  or explicitly for "이 태스크 코드 정리", "simplifier 돌려줘", "구현 후 단순화",
  or /pj-simplify. Works in the existing worker session on the task's changed code, returns
  its result to the caller, and leaves independent review to pj-review. Large diffs alone
  are not a reason to refactor or expand scope.
---

# pj-simplify

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


Apply the code-simplifier agent's clarity, consistency, and behavior-preservation principles to
the completed task diff. This is an editing phase of the existing worker session. The worker
remains the single implementation writer; no separate reviewer, agent launcher, or transport
action is created for this step.

Use the user's language for prose; preserve the task schema keys. Adapt `/pj-*` to `$pj-*` on Codex.
Read `../pj/references/planning-decisions.md` for design choices; use pj-work's planner decision
route in split mode and the shared the shared decision policy/night policy directly in solo mode.

## 1. Load the task and its simplification scope

Resolve helper names from `$SKILL_DIR/../_shared/scripts/` and keep cwd at the task's worktree
or combined session root.

Run `../_shared/scripts/pj-dispatch.py --slug {slug}` and require `work`, using pj-work's dispatch
table otherwise. Read the canonical `raw/tasks/{project}/{slug}.md` and its separate linked
code-archi document, implementation steps, acceptance criteria, and `## 작업 인계` → `### Simplifier`.

The plan records `실행: run` or `실행: skip`, a reason, and the target files/areas. A missing
selection in a legacy plan means skip automatic simplification. An explicit user request to run
or skip overrides the earlier selection; record that change. Size alone does not authorize a
broader pass. Complete the implementation before starting this phase.

Resolve the task's repos and diff bases with pj-dispatch/pj-repos. Inspect each target repo's
branch diff, staged and unstaged changes, and relevant task-created untracked files. Build an
explicit changed-file list per repo. Preserve unrelated edits and distinguish task changes from
the base; do not sweep a package or repo looking for cleanup work.

## 2. Inspect opportunities

Read `references/simplification-guidance.md` for concrete transformations and their equivalence
checks. Choose only changes that improve clarity in the task’s actual implementation.

## 3. Apply bounded, behavior-preserving changes

Stay inside the task's selected changed files/areas. Preserve observable outputs, side effects,
error behavior, ordering, public interfaces, security boundaries, and the agreed code architecture.
Do not add dependencies, redesign modules, or collect unrelated technical debt into this pass.

If an opportunity requires a design change, pause that edit and follow the shared decision policy:
split worker sends `decision.request` to the existing planner, which uses the shared decision policy with the user;
solo uses the shared decision policy directly. Explicit pj-night mode decides and records the design update without
asking. Apply the decision to the plan before resuming. Never escalate it to the project board.
A design-changing edit is implementation work under the revised plan, not a claim of equivalent
simplification; complete it and then reassess this pass before review.

Work in small changes and inspect their diff against the pre-pass implementation. Check call-site
expectations and acceptance criteria for each transformation. If equivalence cannot be supported,
leave that transformation out and report the uncertainty.

Follow pj-work's repository-specific verification rules. Run relevant permitted checks for
behavior-preserving changes; lint/formatter checks use explicit task-changed files only. Report
what remains unverified. Do not claim runtime equivalence from static reading alone, and do not
commit, merge, or report completion through pj-done in this phase.

## 4. Record the outcome and return

Under the plan's `### Simplifier`, keep the selection and append an outcome:
`결과: completed`, `no-change`, or `skipped`, with a short reason, per-repo file list, and a
description of the implementation state inspected (branch commit and any uncommitted changes).
If blocked, record `결과: pending` and the unresolved issue; a pending pass cannot count as done.
Record meaningful changes, preserved constraints, and verification limits so a resumed worker
does not repeat a completed pass just because its chat history is gone.

Return to the calling pj-work. A direct invocation reports the same result and the
next review step. Do not invoke pj-work recursively or create review requests yourself. The
caller submits the final implementation INCLUDING this pass to pj-review, unless the user
explicitly waived review. Cleanup after an earlier review requires a new review of the changed
implementation; old findings are not evidence about newly edited code.
