---
name: pj-task-plan
description: >
  Turn a pj project's agreed architecture into a dependency-ordered set of reviewable tasks
  from its board session. Define each task's outcome, scope, target repos, acceptance criteria,
  dependencies, and integration order, then use pj-task-regi for filing when requested.
  Use for "archi 기준 task 나눠줘", "태스크 계획", "설계서 작업 분해", "작업 순서 잡자",
  or /pj-task-plan. Each started task still needs pj-plan to produce its code architecture
  and implementation plan before worker handoff; this skill does not replace that stage.
---

# pj-task-plan

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


Apply the planner agent's requirements analysis, dependency reasoning, incremental delivery,
and risk analysis to the project's task graph. Work in the existing board session.

Use the user's language for prose; preserve the task schema keys. Claude Code/Grok use
`/pj-*`; Codex uses `$pj-*`. Read `../pj/references/planning-decisions.md` for the shared
the shared decision policy / pj-night decision policy.

## 1. Load architecture and the current task list

All helper names below refer to `$SKILL_DIR/../_shared/scripts/`. Execute them from the board
worktree/session root so repo and branch resolution use the board's actual location.

Resolve the registered project from `../_shared/scripts/pj-repos.py --cwd .` and
`pj-tasks.py proj-list`, matching repo/branch pairs as pj-board does. Read `project.md`,
then `pj-ctx.py --project {project} --root .` and `pj-tasks.py list --project {project}`.
Use the user's architecture path or the canonical document in that context.

If the project needs architecture and its responsibilities/contracts are still undecided, use
pj-archi first. A small task covered by the existing architecture does not need a new architecture
document. Read enough code to locate affected modules, reusable patterns, and integration seams;
leave the detailed code blueprint to each task's pj-plan against its eventual worktree.

Reconcile existing tasks before proposing new ones. Identify already completed coverage and
in-flight work. Preserve existing slugs and progress; do not recreate an equivalent task under
a new title, reset its state, or rewrite an active task's plan while another session uses it.

## 2. Design the task graph

Read `references/task-decomposition.md`. Apply its requirement coverage, task definition,
dependency, integration, and incremental-delivery checks to the actual project.

## 3. Resolve the split and save it

Discuss unresolved scope, contract changes, and decomposition choices with the shared decision policy. Read the
code for factual answers first. In explicit pj-night/no-question mode, decide and record the
reasoning without waiting for a user or board response. A project boundary change follows the
same policy; there is no separate board escalation gate.

Save the agreed task graph in an existing designated task-planning document, or by default
`raw/tasks/{project}/design/task-plan.md` in the vault. Include the architecture source, the task
definitions above, dependency order, possible parallel groups, and decision rationale. Keep draft
identifiers distinct from the canonical slugs that pj-task-regi issues at filing.

## 4. File through pj-task-regi when filing is in scope

For a request to draft/review a plan only, report the document and finish. If the user asked to
write/file the tasks as well, pass the settled definitions to `/pj-task-regi` in prerequisite
order. A split already settled in this conversation does not need another approval round.

pj-task-regi owns slug allocation, list writes, and plan skeleton creation. Give it the original
user request, architecture path/section, scope, repos, acceptance criteria, and dependencies for
each task. It preserves the user's words under `## 최초 요청` and puts the derived definition in
`## 태스크 정의`, so the later planner receives the real task contract rather than just a title.
Use canonical returned slugs to resolve subsequent dependency references and update the graph.

Do not write `tasks.md` or `index.md` directly. If filing stops partway, report which slugs exist
and which definitions remain; resume from those facts rather than duplicating earlier tasks.
Do not start tasks merely because they have been filed. If starting was also requested, continue
through pj-task-start for the requested tasks after the definitions are recorded.

## Report

Give the plan path, task boundaries, dependency order, and material overlap or unresolved facts.
Distinguish drafted tasks from filed slugs and started tasks. The per-task code architecture and
implementation plan will be produced by pj-plan after each task starts.
