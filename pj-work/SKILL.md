---
name: pj-work
description: Implement a filed PJ task from its plan and code architecture in the task worktree. Use for a planner handoff, solo continuation, or implementation retry. Resolve design decisions through the planner or shared decision policy, run relevant repository checks, optionally simplify, and dispatch pj-review. Lint targets only explicit changed files.
---

# pj-work

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


Build what the plan and its separate code-archi document specify. This worker role owns product
code edits, including the optional pj-simplify phase in this same session; planners and reviewers
do not edit implementation files.

Use the user's language for prose; preserve the task schema keys.

This worker is runtime-neutral. Claude Code and Grok call skills as `/pj-*`; Codex calls them as
`$pj-*`. Translate every downstream skill call to the current runtime and never refuse work merely
because the executor is Codex or Grok.

## Entry via the handoff wake-up

The worker session is pre-opened by pj-open-ws and told to wait. Its first real turn is:

```
[pj-event-ready] project=… slug=… source=planner event=evt_…
```

Read the event before doing anything:

```bash
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" event read --slug "$SLUG" --event-id "$EVENT_ID"
```

Require `type=worker_handoff` for this slug. Anything else, or a handoff you already processed →
say so and stop; repeated wake-ups are no-ops. Then acknowledge it, so the streams record that
this session actually took the work (`delivered` only ever meant the line reached the pane):

```bash
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" ack --slug "$SLUG" \
  --request-id "$REQUEST_ID" --status processed
```

Then proceed to dispatch below. The handoff event carries cautions and references as durable
data, but the plan document on disk is the authoritative copy — you read both.

**Solo entry.** In a solo task there is no waiting pane: pj-plan ran in this very session and
invoked you right after recording the handoff (`self=true`). Read and ack that handoff event the
same way — the streams should show the work was taken regardless of topology — and read the plan
from disk as below even though you wrote it: the document is the spec, not your memory of writing
it. `"$SKILL_DIR/../_shared/scripts/pj-cmux.py" topology --slug "$SLUG"` answers `solo` when unsure,
and prints the `side_pane` — the plan viewer's pane — where a document meant for the user opens as a
tab (`cmux open <path> --pane <side_pane> --no-focus`) rather than as a new split of this pane.

## Dispatch first

Do not decide from your location — the question is whose worktree this is:

```bash
"$SKILL_DIR/../_shared/scripts/pj-dispatch.py" --slug "$SLUG"   # --slug optional in a worktree
```

`project=` in the verdict is what locates the plan document and the project's context — don't look
it up again.

| Verdict | Do |
|---|---|
| `work` | you're standing in this task's worktree | proceed |
| `goto` | it has a worktree, but not this one. Show the printed path and **ask** whether to continue there or cut a fresh one (`/pj-task-start {slug}`) |
| `open-ws` | no worktree anywhere → hand off to `/pj-task-start {slug}` |
| `ask` | no slug, or it isn't in the task list → ask which task |

## The plan is the spec, and you always read it cold

Read `$PJ_VAULT/raw/tasks/{project}/{slug}.md` **from disk, every time**.
Planning happened in a different session; you never hold the plan in context, and what you
think you remember about it is someone else's reasoning. Then honor `## 작업 인계`: its
유의사항 are binding constraints alongside the repo's own boundaries, and every document in
참조 문서 gets actually read before you implement — the planner put it there because missing
it produces wrong code.

Read the linked code-archi document as well as the plan. The architecture owns file/component
responsibilities, interfaces, and flows; the plan owns execution order and verification. A new
handoff must link a readable code-archi before implementation begins. Legacy plans that already
contain their full design remain usable; do not invent a new design-document gate for old work.
Honor settled deviations from project architecture recorded in the task's code-archi and plan.
The handoff's canonical `raw/tasks/{project}/design/{slug}/code-archi.md` reference is vault-relative
when its reason says so: read it against the same vault root that supplied the plan (`PJ_VAULT`
in scratch runs, otherwise `$PJ_VAULT`). Other local code/design references
are repo-relative as before. Do not look for the vault code-archi under the task worktree or
silently skip it because that incorrect path is absent.

**No grilling here in split mode.** pj-plan spent the questions so this stage wouldn't have to; a question
now means either the plan is incomplete or you are re-litigating a decision the user already
made. If something in the plan genuinely cannot be built as written — it contradicts the code,
an assumption turns out false, or a real decision is missing — do NOT guess and do NOT
open-endedly chat across panes. Send the planner a structured decision request and end the turn:

```bash
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" request decision.request --slug "$SLUG" --stdin <<'PJEOF'
{"plan_reference": "<which plan item>", "question": "<the blocking question>",
 "options": ["<bounded option A>", "<option B>"], "evidence": "<what you found>"}
PJEOF
```

The reply comes back as a `[pj-event-ready] … source=planner` wake-up carrying a
`decision_reply` event; reload the updated design and plan, apply it, and continue. The planner
uses the shared decision policy with the user for unresolved design choices, including project boundary changes.
No architecture approval goes to the board. See `../pj/references/planning-decisions.md`.

**Solo:** the transport refuses `decision.request` (there is no other session to ask). Perform
the planner's decision work here: facts come from code, already settled choices remain settled,
and new design choices use the shared decision policy with the user. In explicit pj-night/no-question mode decide
autonomously, update the separate code-archi and plan as affected, record the rationale, and
continue. Solo alone does not waive questions. Missing external prerequisites in pj-night are
reported as blockers without waiting for a user reply or claiming completion.

**Gate.** The document must contain `## 작업 단계` AND `## 작업 인계`. Entering via the
handoff wake-up, both are hard requirements — the planner just wrote them, so absence means
you are reading the wrong document. Entering cold on an older task, a document with 작업 단계
but no 작업 인계 predates the handoff schema: warn the user and ask whether to proceed treating
cautions as "없음", rather than refusing work that was planned in good faith. No document or no
작업 단계 at all → this task never went through pj-plan; say so and send them to
`/pj-plan {slug}`.

## Follow the repo's procedure — don't invent one

The plan already reflects the repo's stated procedure (pj-plan read it out of the domain
docs and folded it into 작업 단계). Load the **project's** context if it isn't loaded yet —
`ctx` in `raw/tasks/{project}/project.md`, optional and resolved by `../_shared/scripts/pj-ctx.py --project {project} --root .` (never by hand: a repo in this session resolves to its worktree here, a repo the task does not target resolves to that repo's project worktree — a backend-only task reads the frontend's documents from the board's frontend checkout) — then work the steps in
their order. The repo alias is not where it lives: a repo with several projects in it has a
different answer per project.

- Any artifact the repo requires **before** code — a spec, a design doc, agreed test cases —
  is a real gate even though this skill defines no gate of its own. If it's a step, it comes
  in its place.
- Stay inside the repo's stated **boundaries** for what an agent may do. Some repos reserve
  git operations, test runs, or service startup for the user. Where a boundary exists, stop
  at it and hand back rather than "helpfully" crossing it.
- **No special rule found** → implement and run relevant scoped verification. Commit only when
  requested or included in the user's completion/unattended authorization. Use standard Git and
  exact task paths; no separate commit skill is required. Integration belongs to pj-wrap.

## Static-check scope is mandatory

A repo rule such as "agents may run static checks" answers **whether** a class of tool is allowed;
it never answers **how much of the repo** the tool may scan. Permission does not widen scope.

The default and maximum pj-work scope is the exact set of files this worker created or
edited for this task. Establish that list before running a checker and pass those literal paths to
the tool. A plan step or repo rule may narrow the list further, but cannot widen it. If the task
touches several languages, invoke each checker only with the matching changed files.

A static check may run only when all of these are true:

- the repo procedure explicitly permits that checker category;
- it is read-only — no `--fix`, write, generation, cache-rewrite, or formatter-write mode;
- the command accepts explicit file paths and remains semantically valid with those paths; and
- every supplied target is a file this task changed. No directory, `.`, recursive glob, package,
  workspace, or repository root is an acceptable substitute.

Allowed examples, with placeholders standing for the actual changed-file list:

```text
eslint src/changed-a.ts src/changed-b.ts
ruff check --no-cache app/changed.py tests/changed_test.py
prettier --check src/changed-a.ts
git diff --check
```

`git diff --check` is the one targetless exception because its input is the current patch rather
than a recursive repository scan. Ordinary investigation such as `rg`, reading files, and
`git diff` is not a static verification run and remains available as needed.

Never run a broader fallback merely because the checker cannot accept file paths. These are
forbidden unless the user explicitly requests that broader command in the current invocation:

```text
pnpm lint                  # root script
npm run lint               # root script
eslint .                   # recursive repo scan
ruff check .               # recursive repo scan
pnpm -r lint               # all workspaces/packages
pnpm --filter <pkg> lint   # whole package, still broader than changed files
```

Tests/builds/typechecks follow the repository's own rules and the user's authorization. Run
relevant checks proportionate to the task; do not copy a personal ban on tests into another
repository. PJ does not add a mandatory typecheck at implementation, review or completion;
run one only when requested by the user or required by the repository. Never run a test that requires
unavailable credentials or mutates an external service without the applicable authorization.

If a permitted checker has no faithful changed-file invocation, **do not run it**. Report
`정적 검사 미실행 — 변경 파일 단위 실행을 지원하지 않음` and leave the appropriate command
for the user. A clean global lint result is not evidence about this task strong enough to justify
the unrelated cost and noise.

## Optional simplification before review

After the implementation steps, read `## 작업 인계` → `### Simplifier`. `실행: run` calls
`/pj-simplify {slug}` in this worker session before opening any review round. `실행: skip` skips
it; a legacy plan with no selection also skips automatic execution. A current explicit user
run/skip instruction takes precedence and is recorded. `skip-review` does not imply skip-simplify.

Inspect the simplifier's changes against the agreed design and behavior, and ensure its outcome
is recorded. A pending pass or unresolved decision is not completion. On resume, use the saved
outcome and inspected code state to avoid repeating an unchanged pass. Meaningful implementation
changes after a recorded pass require reassessing that selection before review.

Then proceed with the final diff, including simplification. The caller owns review dispatch;
pj-simplify returns here and does not recursively call pj-work or create review requests.

## Verification and report

Run the plan's relevant verification when permitted. Prefer targeted test files or cases and
stop after appropriate checks pass unless a new failure or change justifies more work. If a
check cannot run, name the command, reason and unverified behavior. Do not claim runtime
correctness from reading code alone.

Report the changes, files, verification results and any skipped checks with reasons. Include
whether simplification ran and what it changed. State whether task changes are committed.
The final completion stage can make a task-scoped commit when authorized; no external commit
skill is needed.

## Hand off to review

After any selected simplification, invoke **`/pj-review {slug}`** — it dispatches the round's review group: the task's
independent code reviewer (the first request creates it, every later one reuses it), a wake-up to
the original planner for intent/scope, and whichever specialist reviewers the planner selected at
the end of planning. You do not pick them and you do not add one; the set was recorded on the
handoff and pj-review requests exactly it.

Each finding set comes back to this session separately as a `[pj-event-ready]` wake-up, where
pj-review's triage phase verifies and dispositions them. You do not review your own diff here;
the whole value is that the code reviewer never saw your implementation reasoning and the planner
never saw your implementation shortcuts. In a solo task
pj-review requests code and selected optional reviewers and records plan review skipped — the report
must say so.

Skip it only on the user's word — 리뷰 없이 · 리뷰 생략 · 리뷰 빼고 or the boot prompt's
skip-review line. Then say in the report that no review was run, so a skipped review never
looks like a passed one, and stop there. If the planner session turned out to be gone and the
plan review was skipped, the final summary must say that too — and name every other reviewer
that was skipped or failed. Never report a clean review round that wasn't one. **Do not invoke pj-done** in either case: reporting the task finished
is the user's call, and the task stays 진행 중 until they make it. The explicit pj-night boot
contract authorizes pj-done after its successful review condition; follow that contract then.

## Pause / refuse

- Dispatch verdict isn't `work` → follow the table; never implement on another task's branch.
- No plan document, or no 작업 단계 in it → send them to `/pj-plan {slug}`; don't improvise a
  plan and start coding. (작업 인계 rules are in the gate above.)
- The plan can't be built as written or leaves a real decision open → `decision.request` to the
  planner; don't guess, don't chat across panes, don't hand it to the user before the planner.
- A repo boundary blocks a step the user asked for → say which rule blocks it and let them
  decide; don't quietly override the repo, and don't quietly skip what they asked for either.
- A wake-up names an event you already processed → no-op; say so and stop.
