---
name: pj-done
description: >
  Report a reviewed, committed PJ task from its worktree to the project board for local
  integration. Use for pj-done, 완료 보고해줘, or 이거 완료 처리해. Checks each target repository's
  committed state; type checking is not required. The user invokes completion explicitly, or
  authorizes it through pj-night. The board records the task and merges it.
---

# pj-done

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


The worktree's way of saying "this one is ready — merge it". It checks the committed state, then sends a completion event home.

**Nothing invokes this automatically.** pj-review is the last link of the chain and stops there on
purpose: a session that just finished writing code is the worst possible judge of whether the work
is done. The user runs this, after looking at the diff — that look is the gate, and it now sits
here because the report is what starts the merge.

Use the user's language for prose; preserve the task schema keys.

This worker is runtime-neutral. Claude Code and Grok call skills as `/pj-*`; Codex calls them as
`$pj-*`. Translate every downstream skill call to the current runtime and never refuse work merely
because the executor is Codex or Grok.

## Completion requirements

The board records 검토 대기 and invokes pj-wrap after processing this report. Verify that every
repository's reviewed task changes are committed and that the working trees contain no remaining
tracked or untracked changes. Preserve unrelated work and report ownership conflicts.

PJ completion does not run or require a type check. Tests or checks explicitly required by the
user or repository remain part of that task's acceptance criteria; do not add a type-checking
requirement merely because pj-done, pj-review, pj-wrap, or pj-night is running.

## Procedure

### 1. Dispatch

```bash
"$SKILL_DIR/../_shared/scripts/pj-dispatch.py" --slug "$SLUG"   # --slug optional in a worktree
```

Require `work`. Any other verdict → you are not in this task's worktree and have nothing to report;
follow pj-work's dispatch table.

### 2. Prepare an authorized task commit, then check

A user-invoked completion request or explicit pj-night run authorizes committing this task's
reviewed changes before local integration, unless repository rules reserve commits for the user.
If uncommitted changes are exclusively this task's, inspect the diff, stage explicit file paths
and commit with standard Git using the repository's saved commit policy. Load it with
`python3 "$SKILL_DIR/../_shared/scripts/pj-commit-policy.py" get --repo "$REPO_WORKTREE"`
for each repository. Follow [commit-policy.md](../pj/references/commit-policy.md) if missing,
stale, or awaiting a format decision. Reuse a ready policy; do not infer style from history again.
Current explicit user/repository instructions still take precedence. Do not stage all
files blindly, include unrelated edits, or bypass failed hooks. If ownership is unclear or the
repository requires user commits, report the blocker and preserve all files. No external commit
skill is required. Then verify the committed state in each target repository.

Check: committed

```bash
git status --porcelain
```

Empty → committed. Anything at all in the output → **not** committed, and that includes untracked
files: a new file the work depends on is exactly the kind of thing that gets left behind, and it is
invisible in a diff of tracked changes.

Also confirm the branch actually has commits of its own against its base — a worktree where
everything was written and then reverted is clean by this check but has nothing in it.

### 3. Report

During an active pj-night run, `done.report` also checks the required reviewers' latest requests
and processed replies. Failed/skipped reviews and unresolved blocking findings refuse the report.
Return to review within the night attempt budget or stop with the failure; do not bypass the gate.

**Every target repository is committed** → request the report through the transport. You never construct a cmux
command or look up a surface yourself:

```bash
# one target repository
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" request done.report --slug "$SLUG" \
  --commit ok

# several — one report, the verdicts inside it
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" request done.report --slug "$SLUG" \
  --result example-backend:ok --result example-frontend:ok
```

**One report per task, however many repos it touched.** The commit checks are per repo; the report is
not, because the board records one task and merges one task. Two reports would give the board two
addresses for one completion and no way to tell a retry from a second half.

The transport resolves the project's board (registered per project — the board is also the branch
this work merges into), **validates that the target surface still holds a live agent**, and injects
only a fixed `[pj-event-ready]` wake-up line; the fields travel as an event the board reads back
through `pj-cmux.py event read`. A dead or shell-only target is a visible failed delivery, never a
fallback send.

An omitted typecheck is recorded as `none` (not run). The optional `--typecheck` or third
`--result` field can retain a check result already obtained: `ok`, `fail`, `none`, or `unparsed`.
Never report an unperformed check as passed. These fields are informational; they do not require
running or repeating a checker to complete the task.

**A commit check failed** → do not report success. Explain the affected repository and preserve
its files. If the user explicitly requests sending the failure, use `--commit fail` (or that
repository's `--result ...:fail`); the board rejects integration and keeps the task 진행 중.

### 4. Report locally too

One summary in this session: per-repository commit status, any checks actually performed, and
the delivery status **accurately** —
the request marker's relay feedback (or `pj-cmux.py status --request-id <id>`) says whether the
wake-up was **delivered**. Delivered means 전달됨, nothing more: not 검토 대기, not 머지됨 — the
board does the moving when it processes the event, and until its ack lands you never saw it move.
Then one line the user needs:

> 보드가 이벤트를 처리하면 pj-wrap의 선택 방식으로 통합됩니다(초기 기본값: merge). 커밋 메시지와 결과는 보드 워크스페이스에 표시됩니다.

Say it because the merge and its commit message land in a pane the user is not in, and nothing in
this session will tell them when it has happened. **Do not move their focus** — pj-review doesn't
either.

## Pause / refuse

- Dispatch verdict isn't `work` → nothing here to report.
- Some target repo's commit check failed → do not report success. Say which repo and why; the passing ones are not news and reporting them separately is what the single-report
  rule exists to prevent.
- The relay reports the delivery FAILED (board not registered, tab closed, agent gone) → say so in
  the user's language and tell them to run `/pj-board {project}` in that project's worktree, then re-run
  `pj-cmux.py retry --request-id <id>` (the id is in the `PJ_CMUX_REQUEST=` line). Do not write the
  task list yourself, do not send to another project's board, and never fall back to raw cmux.
- The request stays pending (no relay feedback at all) → in a Claude session this shouldn't
  happen (requests relay inline); in a Codex session it means the PostToolUse hook didn't fire
  (`pj-cmux.py hook doctor`). Either way show the retry command and say the report has NOT been
  delivered yet.
- Not inside a cmux workspace → there is nothing to send from. Report the checks locally and say the
  completion wasn't delivered.
