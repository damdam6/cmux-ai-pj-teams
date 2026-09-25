---
name: pj-wrap
description: Integrate a completed PJ task into its registered project branches using merge or squash, record per-repository completion, and safely clean task worktrees. Use on a processed pj-done report, ./pj-wrap, or an explicit merge retry. Default mode is merge and can be changed with pj-wrap-default.
---

# pj-wrap

Read `../pj/references/portable-setup.md`. This distribution uses standard Git; no external merge
skill or repository-specific wrapper is required. Use the repository's saved commit policy.
A user-invoked `pj-done` or explicitly authorized `pj-night` includes local project-branch
integration. Honor that authorization; do not invent another approval round. Do not push or
create a PR as part of this skill.

## Select the integration mode

Accept `pj-wrap <slug> [merge|squash]` or `pj-wrap <slug> --mode merge|squash`.
Treat `./pj-wrap` as the same skill invocation. An explicit mode applies only to this invocation;
unknown or conflicting modes must be corrected before any Git operation.

Otherwise read the shared default:

```bash
python3 "$SKILL_DIR/../_shared/scripts/pj-wrap-mode.py" get
```

Use the returned `mode`. If reading settings fails, report the error and stop; do not silently
choose another mode. The initial default is **merge**. Change it with
[`pj-wrap-default`](../pj-wrap-default/SKILL.md): `./pj-wrap-default merge|squash`.
Resolve the mode once before integrating any repository and report it. A later default change
does not change an in-progress operation. On retry, recover already landed repos first, even
if their mode differs from the current selection; apply the selected mode only to remaining repos.

| Mode | Result |
|---|---|
| `merge` | Preserve task commits and create a merge commit with `--no-ff` |
| `squash` | Combine the task changes into one new commit |

## Resolve and preflight

Use `pj-tasks.py get --slug "$SLUG"`, `pj-repos.py --slug "$SLUG"` and
`pj-repos.py --project "$PROJECT"`. Resolve exactly one source and target checkout per task repo.
The board must belong to the task's registered project; run each Git operation inside that
repo's project-branch worktree. Never merge into the task checkout or an inferred main branch.

- `완료`: report already complete. `검토 대기`: proceed. `진행 중`: explain the missing completion
  report and obtain the user's decision before bypassing that stage, unless already authorized.
- Skip repositories already in the task's `merged` record.
- Verify source and target branches, source commit hash, clean tracked and untracked working
  files, and no merge/rebase in progress in both checkouts. Preserve uncommitted files.
- Use the review and commit results from `pj-done`; if code changed since then, review the
  changed scope and repeat only checks required by the user or repository. PJ does not run
  or require a type check before integration.
- Inspect the complete diff and intended commit message before mutating the target. A task's
  status alone is not evidence its code is ready or committed.
- Read `python3 "$SKILL_DIR/../_shared/scripts/pj-commit-policy.py" get --repo "$TARGET_WORKTREE"`.
  Apply its saved subject/body/language and integration-message rule.
  Follow [commit-policy.md](../pj/references/commit-policy.md) for a missing/stale/pending policy;
  do not rediscover style from history on each merge. Resolve incompatible message/trailer
  requirements before changing Git state.

## Integrate one repository at a time

Pin `SOURCE_HEAD` to the verified source commit hash. From the verified target checkout, run
the command for the selected mode with quoted arguments:

```bash
# MODE=merge: preserve history; pause before committing even if fast-forward is possible.
git -C "$TARGET_WORKTREE" merge --no-ff --no-commit "$SOURCE_HEAD"
```

```bash
# MODE=squash: stage the combined changes without creating a merge commit.
git -C "$TARGET_WORKTREE" merge --squash "$SOURCE_HEAD"
```

Run only the selected command. After a successful operation, inspect the staged diff and
pending merge state, then commit using a prepared message file:

```bash
git -C "$TARGET_WORKTREE" commit -F "$MESSAGE_FILE"
```

Include trailers `PJ-Task: {slug}`, `PJ-Source: {source-head-hash}`, and
`PJ-Wrap-Mode: merge|squash` in that message. Before a retry, inspect target history for the task
and source trailers and verify that commit's patch and reachability; do not apply a second
integration if the commit landed but task recording was interrupted. Older receipts without a
mode remain recoverable: inspect the actual commit and its parents instead of assuming today's
default was used.

For `merge`, verify the completed merge has the pinned source as a parent. If Git reports already
up to date, verify the source is an ancestor of the target and explain the existing integration;
do not make an empty replacement commit. A real pending merge can have no net file changes and
still need its merge commit to record ancestry. For `squash`, an empty staged diff needs an
explanation and verification; do not create an empty commit to simulate integration.

If either mode conflicts, leave the state inspectable, report affected files and stop that repo.
Do not reset, discard, or force-resolve changes. Finish/conflict-recover within the user's scope
before retrying. Multi-repo integration is not atomic: record every landed repo immediately.

After a verified commit, the board records:

```bash
"$SKILL_DIR/../_shared/scripts/pj-tasks.py" done --slug "$SLUG" --merged "$REPO"
```

The script keeps `검토 대기` until all target repositories have landed. Never mark an outstanding
repo merged. Record the commit hash and full message in the board report.

## Cleanup after recording

A clean task worktree whose committed source hash matches the integrated receipt may be removed
with `git -C "$MAIN_REPO" worktree remove "$TASK_WORKTREE"` (no force). Then remove its
`wt-registry.py` entry. Preserve task branches by default; a squash does not establish ancestry
for `git branch -d`, and this skill must not substitute forced branch deletion.

Keep the task workspace open while any repository is unmerged or still has a worktree. The bundled transport has no workspace-close action: leave the workspace open and report
that the user can close it after all repositories are integrated. Never close the board. For a multi-repo parent, use `wire-session-root.py
--target "$PARENT" --unwire` to remove only manifest-owned links, then remove the empty parent
with `rmdir`. Preserve unexpected files and report their paths. Cleanup failure does not undo
a successful recorded merge; report it separately and resume cleanup from actual state.

Report per repo: selected mode, source → target branch, commit/message, merged/remaining status, and cleanup
results. For an unregistered branch request, use the repository's ordinary Git workflow instead.
