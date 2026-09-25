---
name: pj-task-start
description: >
  Start work on a task that is already in a pj project's list: dispatch on whether it has a
  worktree yet, hand off to pj-open-ws to cut the branch + worktree + planner/worker workspace
  (or one solo session when the user asked for 한 세션),
  and move the item to 진행 중. Use when the user names a filed task and wants work to begin — "그거
  시작하자", "item-search-hide 착수", "/pj-task-start uom-schema", "할 일에서 이거 잡자". Does one
  thing: it does NOT file new tasks (pj-task-regi), does not view the list (pj-board), and does not
  implement anything (pj-plan → pj-work run in the spawned session). For a brand-new topic that
  should be filed and started in one go, pj-kickoff chains regi and this.
---

# pj-task-start

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


착수 only. One filed task in, one live worktree workspace out.

Use the user's language for prose; preserve the task schema keys.

This worker is runtime-neutral. Claude Code and Grok call skills as `/pj-*`; Codex calls them as
`$pj-*`. Translate every downstream skill call to the current runtime and never refuse work merely
because the executor is Codex or Grok.

## Procedure

### 1. Resolve the slug

First token, or the task the user just pointed at on the board. Not in any list → send them to
`/pj-task-regi` to file it first (or `/pj-kickoff` to do both). Never invent a slug here —
pj-task-regi issues them, and a slug minted here would have no plan document behind it.

### 2. Dispatch

```bash
"$SKILL_DIR/../_shared/scripts/pj-dispatch.py" --slug "$SLUG"
```

| Verdict | Do |
|---|---|
| `open-ws` | proceed. `base=` is the task's **project branch** — pass the verdict through, don't recompute it |
| `work` | you're already standing in this task's worktree — nothing to bootstrap. Hand off to `/pj-work {slug}` |
| `goto` | it already has a worktree. **Ask**: use that one, or cut another? Both are legitimate, so it is the user's call |
| `ask` | not in a list, or the project's registration is broken → follow the printed reason |

### 3. Check what it waits on

```bash
"$SKILL_DIR/../_shared/scripts/pj-tasks.py" get --slug "$SLUG"
```

`deps` lists the tasks this one was filed as following. If any of them is not 완료, **say so and
ask** before bootstrapping — whoever split this work recorded that order at a moment when it was
obvious, and it is usually invisible from the task's own title afterwards. A dep may live in another
project; `list` with no `--project` is how you check its status.

Their answer decides it. This is not a gate: starting ahead of a dependency is often correct (the
interface is agreed, the work is parallel), which is why the skill asks instead of refusing.

### 4. Bootstrap

Invoke the **`pj-open-ws`** skill with the slug, the normalized role-launcher tokens
(`plan=<code> work=<code> review=<code>`, or `plan=<code> review=<code> mode=solo` for a
one-session task), and any reference docs and switches from the request. Arriving from
pj-kickoff the tokens are already normalized — pass them verbatim. Invoked directly with raw
hints ("codex로", "claude로"), normalize them the same way kickoff does — one `pj-cmux.py
launcher parse --role <r> --hint … --repo <repo>` call per role, bare un-roled hints scoping to
the planner, 한 세션 / 단일 세션 / solo becoming `mode=solo` — this file holds no mapping table.
pj-open-ws owns branch/workspace naming, the worktree, the planner/worker layout (two panes, or
one under solo), the project's cmux group, and the prompt that boots the planner. Do not inline
any of that here.

### 5. Record it

```bash
"$SKILL_DIR/../_shared/scripts/pj-tasks.py" start --slug "$SLUG" --branch "$BRANCH"
```

No surface is recorded here — it belongs to the project, registered once by **pj-board** when the
project was created. The task could not have been filed without that, so a board exists; what can
still be stale is its `surface` (its tab was closed). That only breaks pj-done's report later, so
warn rather than block: `get --slug` prints the project's `surface`, and if it is missing tell the
user to re-run `/pj-board {project}` in the project's worktree.

### 6. Report

Summary: slug, project, branch, worktree path, workspace name, the group it joined (if any), whether
any dependency is still open, the resolved launchers, and that the planner is up with the worker
waiting for its handoff — or, under solo, that one session is planning and will implement in
place. Your part ends here.

## Pause / refuse

- Not in a git repo → stop.
- No cmux (`cmux` not on PATH, or not inside a workspace) → pj-open-ws can't build anything.
- Dispatch verdicts `work` / `goto` / `ask` → follow the table; never bootstrap through them.
- `pj-open-ws` failed → do **not** run the `start` transition. A task marked 진행 중 with no
  worktree is worse than one still in 할 일, because dispatch then disagrees with the list about
  what exists.
