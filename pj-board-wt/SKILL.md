---
name: pj-board-wt
description: >
  Build the WORKTREE and cmux workspace a pj board will stand in — one repo, or two when the
  project spans frontend and backend. Creates a worktree per repo under
  $PJ_WORKTREE_ROOT/{project}/{repo folder}, links the skill roots the pj workers
  need, opens a single-pane workspace whose session boots into pj-board, and stops there: it
  does NOT register the project, because a board is something the user registers. Use when the
  user says "보드 워크트리 만들어줘", "프로젝트 워크트리 열어줘", "pj 보드 만들자",
  "FE+BE 프로젝트 시작", or invokes /pj-board-wt. For a TASK's worktree use pj-task-start (which
  calls pj-open-ws); for registering the project use pj-board.
---

# pj-board-wt

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


A project needs a place to stand before it can exist. That place is a worktree on the project's
long-lived branch — one per repo the project owns — plus a workspace with a board session in it.

This skill builds that place and nothing else. **It does not run `proj-reg`.** A board is a
session the user registers, on a branch they chose, and registering on their behalf would make
the project's merge target a side effect of a directory being created. So the boot prompt
invites `pj-board` and the user decides.

Use the user's language for prose; preserve the task schema keys.

Runtime-neutral: Claude Code and Grok call skills as `/pj-*`, Codex as `$pj-*`. Translate every
downstream call to the current runtime.

## One repo or two — the project decides, once

The repos a project owns are the repos its board has worktrees in, and that set is fixed here.
A task can later narrow to a subset of them, but it can never widen past them — so a project
that may need both frontend and backend must be created with both now. Say that when the user
asks for one repo and the work obviously spans two; don't decide it for them.

## Parse the invocation

```
/pj-board-wt {project} [repo hints] [branch hints] [--attach] [free text]
```

1. **`{project}`** — first token, a kebab-case slug. It names the worktree parent
   (`worktrees/{project}/`), so it shares a namespace with task slugs; `pj-tasks.py` refuses a
   task slug that collides with a project name, and the same collision here is a directory
   collision. Missing → ask.
2. **Repos** — which repos the project owns. Accept aliases (`fe`, `be`, `FE+BE`, `프론트`,
   `백엔드`) and folder names. Resolve each against `$PJ_ALIASES`; an
   unknown one → ask rather than guess. Absent → ask, and offer the repo you are standing in.
3. **Branches** — one per repo, as `repo:branch` pairs or in repo order. Absent → ask. There is
   no default: the project branch is the merge target for every task the project will ever hold.
4. **`--attach` / 기존 브랜치** — check out branches that already exist instead of cutting new
   ones. Per repo, not global: a project may attach to an existing backend branch while cutting
   a fresh frontend one.
5. **Board launcher** — a hint like `codex`, `grok`, `claude` for the board session. Normalize it
   through the single launcher-policy source; this file holds no mapping table:

   ```bash
   "$SKILL_DIR/../_shared/scripts/pj-cmux.py" launcher parse --role plan --hint "<words>" --repo "$REPO"
   ```

   The board is not a planner, but it is a long-lived reasoning session, so it takes the `plan`
   role's default when the user gives no hint.

Confirm the parsed plan with the user before creating anything — repos, branches, whether each
is new or attached, and the paths. A worktree on the wrong branch is cheap to delete and
expensive to notice.

## Bootstrap

### 1. Resolve each repo's environment

Per repo, from `$PJ_ALIASES`: `short` (the badge), `wtFolder` (its folder
name under the parent; falls back to the repo's own folder name), `linkPaths`, `envCopy`.

`linkPaths` must carry both skill roots — `.claude/skills` (or `.claude`) and `.codex/skills`
(or the whole `.codex`). Claude Code and Grok read the first, Codex the second. A board worktree
missing one loses `pj-board`, `pj-wrap` and `pj-sync-color` for that runtime. Missing → say so;
it is a one-line alias fix, and fixing it afterwards means recreating the worktree.

A repo with no alias → propose one and save it (`save-repo-alias.py`) before continuing.
For each selected repository, also follow
[`commit-policy.md`](../pj/references/commit-policy.md) to analyze and save its commit format
when missing or stale. Reuse a ready saved policy; show any unresolved format question in the
registration result. This belongs to repository setup and does not register the project or commit code.

### 2. Create a worktree per repo

```bash
"$SKILL_DIR/../_shared/scripts/create-worktree.sh" \
  --name "$PROJECT" --repo "$REPO_PATH" \
  --root "$PJ_WORKTREE_ROOT" --wt-folder "$WT_FOLDER" \
  { --attach "$BRANCH" | --prefix "$PREFIX" --base "$BASE" } \
  ${ENV_COPY:+--env-copy "$ENV_COPY"} ${LINK_PATHS:+--link-paths "$LINK_PATHS"}
```

`--root` is what puts the worktree outside the repo; without it the script keeps its legacy
`{repo}/.worktrees/{name}` behaviour, which four other skill families still rely on. `--repo`
lets this run from anywhere — the vault, another project's parent folder — instead of requiring
you to stand in the repo first.

Read `WORKTREE_ABS` and `BRANCH` out of each run's `---WORKTREE_RESULT---` block. Exit codes
that mean stop and ask: `4` (the directory or branch already exists), `5` (`--attach` branch
missing, or already checked out somewhere — the message names the holder).

**Create every repo's worktree before opening the workspace.** A half-built parent folder
opened as a workspace looks like a working board and is not one.

### 3. Wire the parent folder — only when there are several worktrees

`linkPaths` gives a session its runtime config — `.claude/skills`, `.codex`,
`settings.local.json`, the repo's runtime directories. `create-worktree.sh` already applied it
to each worktree by delegating to the same script named below; what is left is the PARENT,
where the session actually stands when there are several worktrees. Runtime config is
discovered from the cwd **upward**, so the children's `.claude` is invisible from there.
Nothing errors; the skills are simply absent, which is the worst shape this failure can take.

```bash
"$SKILL_DIR/../_shared/scripts/wire-session-root.py" --target "$PARENT"
"$SKILL_DIR/../_shared/scripts/pj-link-family.sh" --target "$PARENT"
```

No `--repo` and no path list: with none given it asks `pj-repos.py` which repos the target is a
root for and takes each one's alias `linkPaths`. It then merges by what is on disk — the same
target from both repos collapses to one link, genuinely different directories merge into one
(this is what puts both repos' skill sets in reach: `pj-wrap` **and**
`pj-wrap`), and `settings.local.json` goes through `merge-settings.py` so both repos'
`permissions.allow` and `additionalDirectories` survive.

The merge only carries skills already in a child checkout. The second command links this package’s
complete PJ set (including `pj-ser-up`) so the FE+BE session can start local servers even when one
repo's `/pj` was stale. Already-correct links are reported `already linked`; a destination that
is not the package link is `SKIP` and is never overwritten.

Exit 3 from `wire-session-root.py` means it reported a conflict — two repos with different
things under one name. Say which, and do not paper over it: the parent is missing that entry
and the session will not have it. Still run `pj-link-family.sh` afterwards — a conflicted
skill name does not excuse leaving the rest of the family off the parent.

**Skip this for a single worktree.** The session stands in the worktree, which
`create-worktree.sh` already wired; a `.claude` above it would be config nothing reads.

### 4. Open the workspace

One semantic request. Never construct cmux commands here:

```bash
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" request workspace.board --project "$PROJECT" \
  --repo-wt "example-backend:$BE_WORKTREE:$BE_BRANCH" \
  [--repo-wt "example-frontend:$FE_WORKTREE:$FE_BRANCH"] \
  --board "$BOARD_LAUNCHER" --name "$WS_TITLE" --description "$DESC" \
  --board-prompt - <<'PJEOF'
{composed board prompt}
PJEOF
```

- `WS_TITLE` — `{short}/{project}` for one repo, `{short}+{short}/{project}` for two
  (`BE/ctx-compact`, `BE+FE/ctx-compact`). Repo identity lives in the title, not in a pill.
- `DESC` — a concise description of what the project is for.
- What the transport resolves: a single-pane layout booting the board launcher with your prompt
  as its auto-submitted first message; the session's cwd (**the worktree when there is one
  repo, their shared parent when there are several**); the project's colour if it already has
  one; the group of the workspace you invoked from; and a registry entry per repo, all carrying
  the same workspace name.

### 5. Compose the board's boot prompt

Write it in English, a few lines, and keep it to what the session cannot read off disk. It is
agent-to-agent text.

```
Project: {PROJECT}. You are its board session.
Run {PJ_BOARD_CALL} {PROJECT} first — that is what registers this session as the board.
[if two repos] This project spans {repos}; its worktrees are the folders beside you.
[if the user added something now] Also: "{this-invocation words}".

Reply in the user's language; preserve the task schema keys.
```

Choose the sigil from the selected profile's runtime: Claude/Grok use `/pj-board`; Codex uses `$pj-board`.

Do not pass `--permission-mode` unless the user asked. A CLI flag overrides the session's own
`settings.json` `defaultMode`, silently downgrading their configured behaviour.

### 6. Report back

Summary: project, each repo with its branch and whether it was cut or attached, each
worktree path, the workspace name and the group it joined (or that it didn't), the board
launcher, whether the parent folder was wired (and any conflict it reported), that the
pj family including `pj-ser-up` was linked there, and the one
thing that has **not** happened yet — the project is not registered until
the board session runs `pj-board`. Say that plainly; a user who reads "보드 워크트리 완료" and
walks away has a directory, not a project.

If the project has no colour, mention `/pj-sync-color` — but as the board's next step, not
yours.

## Pause / refuse

- No project name → ask. Never derive one from the repo or the branch.
- No branch for some repo → ask. Never default to the repo's main branch: that would make the
  project's merge target a branch nobody chose.
- A repo the user named has no alias → propose and save one first.
- `create-worktree.sh` exit 4 or 5 → stop and ask, quoting the message. Never auto-suffix a
  name (`-2`, `-v2`): a collision on a project name usually means that project already exists.
- Any repo's worktree fails → stop before opening the workspace, and say which succeeded, so
  the user can decide whether to remove them or continue by hand.
- `wire-session-root.py` reports a conflict (exit 3) → say which entry, and that the session will
  not have it. Do not resolve it by picking a repo: two different things under one name is a
  question for the user, and the wrong skill running silently is what the refusal prevents.
- `cmux` not on PATH, or `$CMUX_SURFACE_ID` unset → stop. Without a cmux session there is no
  sidebar to place the workspace in.
- The user asks for a TASK's worktree → that is `pj-task-start`. This skill builds boards only,
  and a task worktree cut from here would have no project branch to be based on.
