---
name: pj
description: Install or inspect the complete PJ project/task workflow in a consumer project, including shared scripts and templates. Use for pj 설치, link pj, or setting up PJ skills for a repository.
---

# pj

PJ tracks a project across one or more Git repositories, with board, planner, worker and reviewer
sessions. Install this entire sibling set; individual skill folders are not standalone.

Read [portable-setup.md](references/portable-setup.md) and the package [README.md](../README.md).
Resolve this file's real path: its parent is `SKILL_DIR`, its parent's parent is `PJ_PACKAGE_ROOT`.
The distribution lives wherever the user placed it; never assume a personal vault or checkout.

1. Inspect `config.local.json` and the configured alias file. Explain missing values using the
   README's setup table; preserve choices already made. The package contains examples, not the
   user's repositories, credentials, tasks, or environment files.
2. Install links in the requested consumer directory using the bundled installer:

   ```bash
   python3 "$PJ_PACKAGE_ROOT/_shared/scripts/install.py" --project "$PROJECT_PATH"
   ```

   This links `pj`, all 20 `pj-*` folders, and `_shared` into both runtime skill roots.
   It refuses conflicting destinations instead of replacing them. Use `--dry-run` to inspect.
   Do not link from any other personal source or copy only the skill Markdown files.
3. Run `pj_config.py` and `pj-cmux.py launcher parse --role plan --runtime <claude|codex>` to
   verify paths and launcher selection. The configured CLI must be installed and authenticated.
   Restart a runtime session if it has already cached its skill list.
4. Each repository's `linkPaths` must expose these skill roots to its worktrees. For multiple
   repositories, `wire-session-root.py` combines them at the shared session parent. Then
   `pj-link-family.sh --target <parent>` refreshes the whole package family there, preserving
   conflicts while linking missing skills. Inspect every `SKIP` before declaring it complete.
5. Start the initial board agent inside cmux through `pj-launch.py --profile <id>` so its live
   process can be verified. Later board/task/reviewer sessions use this wrapper automatically.
   Read the transport reference before configuring an optional Codex hook or manual relay.

Report installed/already-linked paths, conflicts, missing settings and verification results.
Installing skills does not start work, register a project, install global hooks, or merge code.

For routing, use the README's complete flow graph and skill list. `pj-board-wt` creates the board
worktrees; `pj-board` registers the project; `pj-archi` and `pj-task-plan` define work;
`pj-task-regi` files it; `pj-task-start` starts it. `pj-kickoff` combines filing and starting.
The task runs `pj-plan` → `pj-work` → optional `pj-simplify` → `pj-review` → `pj-done`.
The board then uses `pj-wrap` to integrate it with merge or squash. `pj-wrap-default` views or
changes the shared mode; an unset default is merge. `pj-night` is explicitly selected unattended
execution. `pj-watcher` provides the board console and bounded reviewer startup monitoring.
`pj-sync-color` maintains project workspace colors. `pj-rebase` rebases FE and BE
onto the same user-supplied branch and reports conflicts.
`pj-ser-up` starts paired local servers from user-configured commands and DB preparation;
read its configuration reference before first use.

Maintenance references:
- [commit-policy.md](references/commit-policy.md): analyze once at repository registration,
  persist the format, and reuse it for task/integration commits.
- [planning-decisions.md](references/planning-decisions.md): user decisions and unattended scope.
- [planner-worker-separation.md](references/planner-worker-separation.md): session roles and handoff.
- [cmux-orchestration.md](references/cmux-orchestration.md): events, delivery and relay.
- [agent-content-mapping.md](references/agent-content-mapping.md): bundled reasoning templates.
