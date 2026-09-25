---
name: pj-night
description: >
  Run a pj project's queued tasks unattended, several at a time, through to the merge — the
  overnight driver. From the project's **board session** it takes the tasks sitting in 할 일,
  starts up to `--parallel` of them as solo sessions carrying the night contract, and as each
  reports done has pj-board merge it and refills the freed slot; a task waits while a dependency
  is still running, is skipped when that dependency can no longer land, and the morning report
  says what landed and what did not. Use when the
  user says "밤에 돌려놓을게", "자는 동안 돌려줘", "밤새 자동으로 진행해", "/pj-night", or names a
  few tasks to run unattended. Starting one task is pj-task-start; watching the list is pj-board.
---

# pj-night

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


The pj pipeline already runs a task from 착수 to 리뷰 without anyone in the room. This skill
supplies the two things it does not: **something that crosses the pj-done gate**, and
**something that starts the next task**.

It adds no transport and no merge logic of its own. The queue and the watch are
`scripts/night-queue.py`; the merge is pj-board's step 3 exactly as it already runs.

Use the user's language for prose; preserve the task schema keys.

This worker is runtime-neutral. Claude Code and Grok call skills as `/pj-*`; Codex calls them as
`$pj-*`. Translate every downstream skill call to the current runtime.

## What the user decided by running this

Merge included. The night run integrates each finished task using pj-wrap's selected mode
(shared default initially `merge`, or explicitly requested `merge|squash`) into the project branch, which is
why the pj-done gate moves rather than disappears — see `references/boot-instructions.md` for why
the task session is allowed to call `/pj-done` itself.

The branch it merges into is a project branch: not `main`, and never pushed. the user's PR workflow is
still ahead of everything this night produces.

## Why several at once, and what it costs

Nothing in the transport serializes tasks — its locks are per-request and per-(project, slug) —
and the day-time flow already runs several task worktrees in one project. Merges serialize on
their own, because one board session consumes the reports.

What concurrency costs is that tasks are cut from the base as it stands when they start, so two
that touch the same files can conflict at the *second* merge. That is the existing failure path,
not a new one: pj-wrap leaves the task 검토 대기 and the morning retries it. Queue tasks that
overlap heavily with `--parallel 1`, which restores the strictly sequential run where every task
branches off the previous merge.

## Where you must be standing

The project's board session — the worktree on the project branch, registered by `/pj-board`.
Anywhere else there is no address for the done reports and no branch to merge into, so stop and
say so. If this session is not yet a board, run `/pj-board {project}` first; do not register one
from here.

## Procedure

### 1. Build the queue

```bash
NQ="$SKILL_DIR/scripts/night-queue.py"
"$NQ" plan --project "$PROJECT" [--slug a --slug b …] [--max N] [--parallel 3] [--attempts 3]
```

No `--slug` → every task in 할 일, in list order. Named slugs → only those, in the order given;
each must be in 할 일. `--parallel` is how many run at once (default 3; `1` is sequential).
`--attempts` is how many self-repair rounds a task session gets before it gives up (default 3)
and it goes into the boot block below.

`plan` refuses to overwrite a `running` queue. Resume it with `next`; to replace it, explicitly
`stop` it first. All queue mutations take the project's `night.lock` across the full state
read/update/write, so overlapping commands cannot hand out the same slot or lose a result.
The lock file stays on disk after release; its existence does not mean the queue is locked.
Use a trusted, user-controlled task storage directory. Project directories must not be symlinks;
state and lock files must be regular, single-link files. Do not delete `night.lock` to unlock a
running process. State writes use a private, exclusively created temporary file and atomic replace.

Show the user the planned queue, the parallelism, and every `WARN` line before starting. A `WARN`
names a task whose dependency is not in tonight's queue and is not 완료, so it will be skipped —
refiling or adding that dependency is a thing only they can decide, now, while they are awake. A
dependency that IS in the queue needs no warning: the task simply waits for it.

### 2. Start the next task

```bash
"$NQ" next --project "$PROJECT"
```

| Verdict | Do |
|---|---|
| `PJ_NIGHT=next slug=…` | step 3, then come **straight back here** to fill the next slot |
| `PJ_NIGHT=skipped …` | informational: inspect the rest of this output for `next`, `busy`, `waiting` or `done` and follow that final verdict |
| `PJ_NIGHT=busy …` | every slot is full — step 4 |
| `PJ_NIGHT=waiting …` | nothing startable yet (`held=` waits on a dependency in flight) — step 4 |
| `PJ_NIGHT=done` | the queue is empty and nothing is in flight — step 6 |
| `PJ_NIGHT=stopped` | the run was ended — report and stop |

Keep calling `next` until it stops handing out slugs. Starting one task and going straight to the
watch would leave the other slots idle all night.

`next` rechecks each task's status before assigning it. A task that left 할 일 after planning is
recorded as skipped with its current status, and the next eligible task can fill the slot.
The later dispatch check still applies in case another session claims it after this check.

### 3. Kick it off

Invoke **`/pj-task-start {slug}`** with `한 세션` and the night block from
`references/boot-instructions.md`, `{ATTEMPTS}` filled in. Pass it as the invocation's own
words — pj-task-start forwards them to pj-open-ws, which puts them in the planner prompt. Do not
compose a workspace, a branch or a prompt here; pj-open-ws owns all three.

Forward any explicit reviewer selection and model/effort instructions from the night request
with that block as well. A request such as “리뷰 grok” selects the configured Grok review
option across the existing reviewer set; pass it through pj-open-ws launcher normalization.
For example, “Grok 추가, xhigh” must reach pj-plan as an additional
`grok` reviewer plus the requested model/effort and matching configured launcher profile ID;
if that profile does not exist, resolve the configuration before starting the unattended run.
pj-plan records the selection and profile in
its handoff so pj-review can apply them. The night block does not replace those user choices.

pj-task-start failing (branch collision, dispatch verdict `goto`/`ask`) is this task's outcome:

```bash
"$NQ" record --project "$PROJECT" --slug "$SLUG" --outcome failed --reason "착수 실패: …"
```

then back to step 2. Never answer a dispatch question by guessing to keep the night moving — a
`goto` means a worktree already exists, and picking one at 3am is how two branches of the same
task get merged.

### 4. Wait for it

```bash
"$NQ" watch --project "$PROJECT"
```

No `--slug`: it watches **every** in-flight task and returns as soon as **one** of them resolves,
naming it. Run it **in the background** — it blocks for as long as the slowest of them takes.

`--since <ISO timestamp>` overrides the event cutoff for the watched tasks; otherwise each
task's start time is used. Invalid timestamps are refused. New event IDs reset the quiet timer
regardless of UUID ordering; rereading the same events does not count as new activity.
The watch reloads queue state while polling: `stop` or replacing the run ends the old watch,
and a result recorded by another consumer removes that task from its targets.

| Verdict | Meaning | Do |
|---|---|---|
| `PJ_NIGHT=report slug=…` | that task reported done and its reviews pass the night check | step 5, for that slug |
| `PJ_NIGHT=blocked slug=…` | a report exists but required reviews do not pass | record failed with the reason, then step 2 |
| `PJ_NIGHT=error slug=…` | the task's events could not be read | record failed with the error, then step 2 |
| `PJ_NIGHT=stopped` | the run stopped or was replaced | stop this driver; do not merge a late report |
| `PJ_NIGHT=quiet slug=…` | nothing in that task's streams for 30 minutes | `record --outcome failed --reason "무응답: …"` for that slug, then step 2 |
| `PJ_NIGHT=timeout` | 2 hours with nothing resolving | treat the in-flight tasks as stuck: record them failed and step 2 |
| `PJ_NIGHT=idle` | nothing is in flight | step 2 |

Every verdict resolves exactly one task, so after handling it go back to step 2 — that both
refills the freed slot and re-arms the watch over whatever is still running.

A `[pj-event-ready] … source=worker …` line may wake this session first. Read the event; only
`type=done_report` goes to step 5. It is the same report the watch may return, not a second one.
Every path must run the check in step 5, including wake-ups received after the watch stopped.

`quiet` and `timeout` leave the task's workspace and worktree alone. They are the state the user
reads in the morning, and tearing them down would delete the evidence of what went wrong.

### 5. Merge it

Immediately before consuming the report and merging, run:

```bash
"$NQ" check --project "$PROJECT" --slug "$SLUG"
```

Only `PJ_NIGHT=ready` authorizes this automatic merge. It requires the same task to be in flight
in the running queue and a processed reply to every required reviewer's latest request in this
run. Failed delivery, skipped/unrequested reviewers and unresolved blocking findings do not
pass. A targeted recheck preserves successful reviews from unchanged reviewers. A stopped run
or already-recorded task must not merge; a review failure in a still-active task is recorded as
failed. Never bypass this check because a prior watch already returned `report`.

Then follow **pj-board's step 3** — read the event, 검토 대기, ack, `/pj-wrap`, record 완료.

Then record what happened:

```bash
"$NQ" record --project "$PROJECT" --slug "$SLUG" --outcome merged
# merge did not land (conflict, dirty tree) — pj-board leaves it 검토 대기 and so do you:
"$NQ" record --project "$PROJECT" --slug "$SLUG" --outcome failed --reason "머지 실패: …"
```

Back to step 2. A failed merge is not retried tonight: pj-wrap's own rule is that the task stays
검토 대기 and `/pj-wrap {slug}` retries once the cause is gone, and nothing at 3am can clear a
conflict.

### 6. The morning report

```bash
"$NQ" report --project "$PROJECT"
```

Report what merged, what failed and why, what was skipped and for which dependency, and which
task workspaces are still standing. Distinguish the completed AI reviews from the user's live
diff review, which unattended execution skips. The user can inspect the integrated changes
with `git log -p` on the project branch.

## Pause / refuse

- Not in a cmux workspace, or this session is not the project's board → stop; there is no address
  for the reports.
- A night run already `running` for this project → resume with `next` only if this session is
  its sole driver; otherwise say it is already running. The queue lock protects assignment,
  but two board drivers could still try to consume and merge the same report.
  `night-queue.py stop --project …` ends the old run before a replacement is planned.
- `plan` finds nothing in 할 일 → say there is nothing to run. Do not pull 진행 중 tasks in;
  those already have worktrees and an owner.
- A task's dispatch verdict is `goto` or `ask` → record it failed and move on. Never decide it.
- The user asks to run two projects in one night → one board owns one branch, so run one project
  per board session; a second project needs its own board and its own `/pj-night`.

## Anti-patterns

- Don't reimplement the merge here. pj-board consumes the report and pj-wrap merges it; a second
  path to the project branch is a second answer to "what is on it".
- Don't raise `--parallel` to cover a queue of tasks that all edit the same area. They will each
  finish and then collide one by one at the merge, and you will have spent the night producing
  branches that need a human to rebase.
- Don't merge two reports at once to keep up. pj-board's step 3 is one report at a time; the
  serialization is what makes each task's base the previous merge rather than a guess.
- Don't retry a task the session gave up on. It already spent its `--attempts` rounds; a fresh
  session would start from a half-finished worktree without the reasoning that produced it.
- Don't skip `record`. A slug stays in `inflight` until it is recorded, which is what keeps a
  crashed-and-resumed loop from starting the same task twice and what frees the slot.
- Don't let the night keep a stalled task alive to "give it more time". The quiet window is
  already 30 minutes of silence; holding the queue on it spends the night on one task.
