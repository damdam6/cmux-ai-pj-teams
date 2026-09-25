---
name: pj-sync-color
description: >
  Synchronize a pj project's board color with its project.md. Run from any Claude, Codex, or Grok pane in
  the project's board worktree; project identity comes from its registered repo + branch, never a
  session/surface UUID. If the project has no color yet, choose a random readable #RRGGBB that is
  perceptually distinct from every live pj project's color (finished projects release theirs) and
  sitting in the hue region those colors leave emptiest, apply it to the current board
  workspace and every live task workspace, and save it. Re-running reapplies the saved color to
  the whole project; the explicit `newc` mode chooses another distinct color. Trigger on
  "pj 색상 동기화", "프로젝트 색 정해줘",
  "board 색 맞춰줘", "pj-sync-color", or /pj-sync-color.
---

# pj-sync-color

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


Give one pj project one stable color. The project's `project.md` is the source of truth after the
first assignment; `workspace.open` (the pj-cmux.py transport) reads it and applies it directly
when a task workspace is created — no helper pane, no manual workspace color changes outside this workflow — and this skill is the
explicit later resync that updates every already open workspace belonging to the project.

Use the user's language for prose; preserve the task schema keys.

This worker is runtime-neutral. Claude Code and Grok call skills as `/pj-*`; Codex calls them as
`$pj-*`. Translate every downstream skill call to the current runtime and never refuse work merely
because the executor is Codex or Grok.

## Where it runs

Run from the project's **board location**, in any pane/session inside its cmux workspace: the board
worktree for a one-repo project, or the **parent folder of the board worktrees** for a project
spanning several repos — that parent is not a git repo, and that is fine. The helper asks
`pj-repos.py --cwd` what the location is (one repo↔branch pair for a worktree, one per child for
a parent) and matches every pair against `pj-tasks.py proj-list`'s registered `repo` + `branch`.
Claude, Codex, and Grok panes have different surface UUIDs, but they are still the same board
when they stand in the same location.

A task worktree (or a task's parent) carries task branches, so it does not match and is refused.
No registered project for the location → tell the user to run `pj-board {project}` from the
project branch's worktree (or their parent) first. Surface UUID is still useful as pj-done's
report address, but it does not own or identify project color.

## Modes

- No argument: use the saved `color` from `raw/tasks/{project}/project.md`. If it is missing or has
  become too close to another project's value, generate and save a replacement — but only after
  the board-location check above succeeds.
- `newc`: generate another color even when one is already saved.

Do not accept a user-supplied hex here. manual workspace color changes outside this workflow owns arbitrary manual color changes;
pj-sync-color owns the pj invariant that project colors are random, unique, and visibly separated.

## Execute

```bash
"$SKILL_DIR/scripts/sync-color.py" [newc]
```

The helper performs the whole transaction:

1. Resolve the current board project from the location's repo↔branch pair(s).
2. Read every `raw/tasks/*/project.md` color.
3. Reuse this project's saved color, or sample a new readable color: candidates that clear the
   bundled OKLab-distance floor win, and among them the emptiest hue band. When the live color
   space is crowded and nothing clears the floor, the farthest candidate is taken anyway and the
   achieved distance is printed (`distance=… floor=… crowded=yes`) — a board with the best
   available color beats a board with none. Without `newc`, a saved color that is too close is
   replaced only by a strictly better one, so a crowded board's color does not flap run to run.
4. Resolve the project's live task workspaces from task branches through `wt-registry.py`.
5. Apply the hex to the current board and every resolved task workspace.
6. Save it as quoted YAML — `color: "#RRGGBB"` — in the project's frontmatter.

The workspaces change before the file. If any workspace update or the file update fails, the helper
restores every workspace it already changed to that workspace's own previous color (or clears it
when there was none), so a partial project recolor does not silently remain.

## Report

Relay the helper's project, hex, and updated live task-workspace count in the user's language. Say whether it
reused the stored value or generated a new one. Keep it to one line unless something failed.

## Pause / refuse

- Not inside cmux → stop when cmux cannot read/apply the current workspace color.
- The location's repo↔branch pair(s) are not exactly one registered pj board → stop. A task
  worktree must not recolor its project by accident.
- No git worktree at the location at all → stop and say where to run: the board worktree, or for
  a combined project the parent folder holding its worktrees.
- `project.md` missing or malformed → report the path and stop; do not recreate a human document.
- No candidate clears the perceptual-distance floor → NOT a stop: the best available color is
  applied and the shortfall is reported in the `PJ_COLOR` line. Say it to the user in one clause
  (색 공간이 붐벼 기준 거리 미달, 달성 0.104) — never hide it, and never invent a hex by hand.
- cmux rejects the color → leave `project.md` untouched.
