---
name: pj-task-regi
description: >
  File work into a pj project's task list without starting it. Two shapes: (a) one topic → one
  task ("이거 나중에 하자", "할 일에 넣어둬", "백로그에 적어놔", "/pj-task-regi Item 검색창
  문제"); (b) settled task definitions from pj-task-plan → file them with dependencies and scope.
  A raw design/spec request ("이 문서 보고 task로 쪼개줘", "설계서 기준으로 작업 나눠줘") routes to
  pj-task-plan for decomposition first. Which project a task joins is read
  off the current branch, so this normally runs in the project's board session. Each task gets a
  slug, a list entry, and a plan document skeleton holding the user's own words. It never starts
  work — that is pj-task-start, or pj-kickoff to file and start in one go. To see what is already
  filed, pj-board.
---

# pj-task-regi

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


Turn intent into filed tasks. Nothing here begins work, so it is cheap to be wrong: a badly split
task is edited or dropped, not un-implemented.

Use the user's language for prose; preserve the task schema keys.

This worker is runtime-neutral. Claude Code and Grok call skills as `/pj-*`; Codex calls them as
`$pj-*`. Translate every downstream skill call to the current runtime and never refuse work merely
because the executor is Codex or Grok.

## Which project

A task belongs to exactly one project. Resolve the current repo/branch pairs (including a
multi-worktree parent) and match the registered project:

```bash
"$SKILL_DIR/../_shared/scripts/pj-repos.py" --cwd .
"$SKILL_DIR/../_shared/scripts/pj-tasks.py" proj-list          # match the resolved repo/branch pairs
```

| 현재 브랜치 | 프로젝트 |
|---|---|
| 등록된 프로젝트 브랜치 | 그 프로젝트 — 보드 세션에서 적재하는 기본 경로 |
| `{prefix}/{slug}` (태스크 워크트리) | `get --slug {slug}` 의 `project` — 작업 중 떠오른 관련 작업을 그 자리에서 적재 |
| 그 외 (메인 체크아웃 등) | **판정 불가 → 묻는다.** `proj-list` 를 보여주고 고르게 한다 |

`--project` in the invocation overrides all of it. Never infer a project from the topic's wording;
a task filed under the wrong project gets its plan document in the wrong folder and reports to the
wrong board.

Determining this by branch is the same rule `pj-dispatch.py` uses for worktrees, deliberately — one
judgment basis, so the project a task is filed under and the worktree it later gets cannot disagree.

## The slug is issued here

Once, at filing, and it never changes. It is the key the whole family runs on — list entry, plan
document filename, branch, workspace, dispatch verdict.

- English kebab-case from the topic, meaningful, **max 5 words** (`item-search-hide`).
- Named for the problem, not the fix you have in mind.
- Already taken, **or equal to a project's name**, → `pj-tasks.py add` refuses. Ask for another;
  never auto-suffix (`-2`, `-v2`). A near-identical slug is worse than a collision, because every
  later dispatch then has two plausible answers. Projects share the namespace because
  the worktree root is one namespace — `worktrees/{name}/` is the parent for a board AND for a
  task, so the two would collide there.

## Filing one task

Two writes, in this order.

**1. The list entry.**

```bash
"$SKILL_DIR/../_shared/scripts/pj-tasks.py" add --project "$PROJECT" --slug "$SLUG" \
  --title "$TITLE" [--deps other-slug,another-slug] [--repos example-frontend]
```

`--title` is one concise line, symptom/goal-first, derived from the topic. No `--repo` — the repo is
the project's, and one fact gets one home.

`--repos` is for a project that owns **several** repos and a task that touches only some of them.
Omit it and the task targets every repo the project owns, which is the right default and the only
possibility for a single-repo project. Pass it when the user's topic is plainly one-sided ("FE 쪽
라벨만", "쿼리 쪽만") — a one-repo task in a two-repo project then opens one worktree and merges
one branch, exactly like today's single-repo flow. The script refuses a repo the project does not
own, and drops a subset that equals the whole (it would only repeat the project's own badge).

Don't guess the subset from the title. A task filed FE-only that turns out to need the backend
cannot widen later — its worktree and branch are already cut — so when it is not obvious, ask, or
leave it off and let the task cover the project.

**2. The plan document skeleton** at
`$PJ_VAULT/raw/tasks/{project}/{slug}.md`.

This is not bookkeeping — it is the only context the later planning session inherits. That session
boots with nothing but this file, so **the user's request goes in verbatim**. If they typed a real
sentence, keep it word for word; if the topic was terse, one line of restatement is enough.
Ambiguities you smooth over here become that session's blind spots.

```markdown
---
project: ds-migration
slug: item-search-hide
created: 2026-08-03
---

# Item 검색창 노출 제거

## 최초 요청

(사용자가 말한 그대로)
```

Frontmatter carries **identity only**. Status is not a field here — it is the section the item sits
under in `{project}/tasks.md`. `branch` gets added when work starts, `deps` and `repos` live in the
list, and `repo`/`ctx` live on the project.

## A design document or settled task definitions

For a raw architecture/spec document that still needs decomposition, invoke `/pj-task-plan`
from the project's board session. It owns task boundaries, acceptance criteria, dependencies,
and integration order. Pass through whether the user asked for a draft only, filing, or starting;
this delegation does not broaden the request. Do not maintain a second decomposition procedure
here.

When pj-task-plan returns settled task definitions for filing, use **Filing one task** above
for each, in prerequisite order. Do not invoke pj-task-plan again or ask the user to approve the
same split twice. Reuse known slugs for already filed tasks; the script remains the authority on
collisions and dependencies.

Preserve the actual user request in `## 최초 요청`. Add the derived material separately as
`## 태스크 정의`: source architecture path/section, outcome, scope/exclusions, target repos,
acceptance criteria, and a link to the project task-plan. Keep `deps` and `repos` authoritative
in the list. This gives the later pj-plan a concrete task contract without pretending generated
definitions were the user's verbatim words.

Filing does not write the code architecture or implementation sequence. Each task's pj-plan
creates those as separate documents once its worktree starts.

## Recording dependencies

`--deps` is a comma-separated list of slugs, no spaces. Record one whenever the order is real: task
B genuinely cannot be built, or cannot be built sensibly, until A lands. A dep may name a task in
another project; the check spans all of them.

This matters most exactly here, because a split is the moment the ordering is obvious and the only
moment it is written down. A week later nobody reconstructs it from the titles, and pj-board can
only show what was recorded.

Record the ordering, not a wish. If two tasks merely touch the same file, that is not a dependency
— it is a merge conflict, and pretending otherwise makes the board show work as blocked when it
isn't.

Nothing enforces deps: pj-task-start warns and asks, and the user decides. So an over-recorded
dependency costs a question every time, which is a real cost and a reason to be honest about which
ones are actual.

## Report

Summary: which project it went to, what was filed, each slug, and the dependency order if there is
one. Then the follow-up — `/pj-task-start {slug}` to begin one, `/pj-board` to see everything.

## Pause / refuse

- No repo/branch context can be resolved and no project was named → ask; a multi-worktree
  parent is valid even though it is not itself a git repo.
- Project not determinable and the user hasn't named one → ask. Never file into a guess.
- No project registered at all → nothing can be filed. Send them to make a worktree on the
  project's branch and run `/pj-board {project}` there.
- Topic genuinely unclear → ask. Never invent a task.
- Slug collision, or a slug equal to a project's name → ask for another; never auto-suffix.
- A dep that isn't filed yet → file it first. If the user wants a dependency on something outside
  the lists entirely (another team, a release), that is not a `deps` value — put it in the plan
  document's 최초 요청 where a human will read it.
