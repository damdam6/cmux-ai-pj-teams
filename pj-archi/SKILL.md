---
name: pj-archi
description: >
  Define or revise a pj project's architecture from its board session: responsibilities,
  module boundaries, interfaces, data flow, constraints, and consequential trade-offs.
  Use for "프로젝트 archi 작성", "아키텍처부터 정하자", "모듈 책임 정리", "인터페이스 설계",
  or /pj-archi. Resolve design choices with the user through the shared decision policy; an explicit pj-night
  invocation decides and records them autonomously. Produces the project architecture that
  pj-task-plan decomposes into tasks. Concrete task code architecture belongs to pj-plan.
---

# pj-archi

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


Give the project a coherent structure before deciding its task boundaries. Apply the architect's
current-state analysis, responsibility design, and trade-off reasoning at project scope. Work in
the existing board session; this skill needs no new agent role or cmux pane.

Use the user's language for prose; preserve the task schema keys. Claude Code/Grok use
`/pj-*`; Codex uses `$pj-*`. Adapt downstream calls to the executing runtime.

Read `../pj/references/planning-decisions.md` before making design decisions. It defines normal
the shared decision policy and unattended decision handling for the whole planning flow.

## 1. Establish the project and its sources

Resolve helper paths below from `$SKILL_DIR/../_shared/scripts/`. Keep the command cwd at the
board worktree/session root; changing cwd into the skill directory would lose project identity.

Use `../_shared/scripts/pj-repos.py --cwd .` and `pj-tasks.py proj-list` to match the
current repo/branch pairs to the registered project. An explicit project argument selects among
those matches. Read `raw/tasks/{project}/project.md` and the existing task list. If this is not the
project's board location, report that and use pj-board's location rules; do not register or move
a board merely to write architecture.

Resolve context with `../_shared/scripts/pj-ctx.py --project {project} --root .`. Read the repo's
own architecture and development rules, the user's requirements, and the relevant existing code.
Keep repo identities explicit for a project spanning several repos.

Find the canonical architecture document from the user's path or the project's context. Extend
that document rather than creating a competing source. If there is none and the repo specifies
no location, use `raw/tasks/{project}/design/archi.md` in the vault. The `design/` subfolder keeps
project artifacts separate from `{slug}.md` task plans. Resolve the actual vault root through the
shared helpers (normally `$PJ_VAULT`; scratch runs can use `PJ_VAULT`).

## 2–3. Investigate and design

Read `references/architecture-guidance.md` and apply its current-state analysis, responsibility
and interface design, and trade-off checks. Keep this stage at project architecture scope.

## 4. Resolve choices and write the architecture

Use the shared decision policy for unresolved design choices in normal execution, one question with a recommended
answer at a time. In explicit pj-night/no-question mode, make the choices and record assumptions
and consequences. Apply the shared decision policy even when revising an existing boundary.

Follow the repo's document convention. In its absence, cover these subjects concisely:

```markdown
# {프로젝트} 아키텍처
## 목표와 범위
## 현황과 제약
## 모듈과 책임
## 인터페이스와 데이터 흐름
## 설계 결정과 대안
## 통합·검증 기준
```

Include migration/rollback and operational constraints only when the change needs them. State
which questions are resolved and which facts are still unavailable. Do not declare the design
ready for decomposition while a missing fact prevents defining its contracts.

## 5. Make it discoverable and report

Link the canonical document from the project's active `ctx` (or legacy `docs`) list without
discarding existing entries. For several repos, retain repo-qualified keys; a vault document
belongs under `ctx.shared`. For a flat one-repo list, an absolute vault path is supported.
Preserve the rest of `project.md`, including identity and color, then run
`pj-ctx.py --project {project} --root . --check` to verify reachability.

A repo document must be available from the project branch before task worktrees can inherit it.
Report an uncommitted architecture document as pending that repo's normal commit procedure;
do not silently commit or assume a new task can read another worktree's uncommitted file.

Report the document path, settled responsibilities/contracts, and any assumptions or unresolved
facts. Continue to `/pj-task-plan` when the user's request includes task decomposition; otherwise
finish with the architecture. This skill does not file tasks, start worktrees, or implement code.
