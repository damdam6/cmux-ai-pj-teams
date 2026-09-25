---
name: pj-plan
description: >
  Produce a filed task's code architecture and implementation plan as separate linked
  Markdown documents, complete both, then hand off to the worker. Investigate concrete files,
  interfaces, data flow, dependencies and verification; select optional simplification and
  reviewers. Resolve design choices with the shared decision policy in normal execution, autonomously with
  recorded rationale under explicit pj-night/no-question instructions. Runs in the task planner
  session, which remains its planning reviewer after handoff (solo continues as worker).
  Use for /pj-plan, "태스크 계획", "코드 설계", or planner decision/review wake-ups.
  Project architecture is pj-archi; project task decomposition is pj-task-plan.
---

# pj-plan

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


The task's plan document starts as a title and the user's original request. This skill turns
it into something pj-work can build from without guessing. This session combines the code
architect and task planner responsibilities: it hands directly to the worker, so both the code
design and the implementation sequence must be complete here.

Planning is the whole point of the front half of this pipeline: **every question the work
raises gets asked here.** An ambiguity resolved at plan time costs a sentence; the same one
resolved at implementation time costs code that already exists.

Use the user's language for prose; preserve the task schema keys.

This worker is runtime-neutral. Claude Code and Grok call skills as `/pj-*`; Codex calls them as
`$pj-*`. Translate every downstream skill call to the current runtime and never refuse work merely
because the executor is Codex or Grok.

Read `../pj/references/planning-decisions.md` for the shared decision policy. Normal design
choices use the shared decision policy with the user; explicit pj-night execution decides and records them. A
change to project architecture follows that policy directly, without escalation to the board.

## The two documents

**Implementation plan:** `$PJ_VAULT/raw/tasks/{project}/{slug}.md`
(`project=` comes from the dispatch verdict) — pj-task-regi created it at
적재 with frontmatter and a `## 최초 요청` section. You fill in the rest and never touch what
is already there: the original request is the record of what the user actually asked for, and
rewriting it in your own cleaner words is how the point of the task quietly shifts.

**Code architecture:** use the task's existing designated code-design document, or by default
`raw/tasks/{project}/design/{slug}/code-archi.md` in the same vault. Its schema lives in
`references/code-archi-schema.md`. This is distinct from the project architecture and from the
implementation plan: it owns concrete file/component responsibilities, interfaces, and data flow.

Read both schemas before writing. The implementation plan links the canonical code-archi document
under `## 설계 참조`; do not duplicate its blueprint in the plan. Its final `## 작업 인계` is yours
and is always present: the worker starts cold in another session, and that section carries the
constraints that are easy to miss and the documents it must read — it is what a failed worker
launch retries from, so it must stand without this session's chat history.

`branch` is the one frontmatter field you add, using the task's resolved branch from
pj-dispatch/pj-repos (the session root can be a non-git parent of several worktrees). Status is
**not** a field — it is the section the item sits under in `{project}/tasks.md`, and pj-board
manages that. Preserve an existing `## 태스크 정의`: it carries the filed scope and acceptance
criteria from pj-task-plan, which the code design must address.

## The plan's shape is the repo's call, not yours

Load the repo's context first, then read what it says about how work is done here:

1. **The project's context** — `ctx` in `raw/tasks/{project}/project.md`, the documents every
   task in this project inherits. Resolve it, never read the field by hand:

   ```bash
   "$SKILL_DIR/../_shared/scripts/pj-ctx.py" --project "$PROJECT" --root .
   ```

   Each path comes back under a checkout OF ITS OWN REPO: the worktree in this session when the
   repo is part of it, and otherwise that repo's project worktree — a backend-only task still
   reads the frontend's documents from the board's frontend checkout, never from the backend
   one. That is what the script exists to get right. Read what it prints, unless this session
   already has it
   (pj-open-ws's boot prompt often loaded it; don't re-read the same docs). Nothing printed →
   skip, and say so in your report so the user can fill it. Do not go looking for context
   anywhere else — not in the repo alias, not in another skill: it is the project's, and a repo
   with several projects in it has a different answer per project.
2. **Find the repo's stated implementation procedure** in those docs. Repos differ: some
   require a spec or design document before any code, some impose test-first discipline, some
   reserve git operations or service startup for the user. Whatever the repo requires *before*
   code becomes part of this plan. Produce any design needed to choose the implementation here,
   before handoff; include later required implementation artifacts and checks in the work steps.
3. **Keep the repo's procedure authoritative.** The design responsibilities below do not replace
   its workflow or copy its rules. Those live in the repo and change there.

No procedure found → plan the work plainly: what's true now, what should be true, how to get
there, how you'll know it worked.

## Design and plan

Read `references/code-architecture.md` for the investigation and design responsibilities.
Write the code architecture using `references/code-archi-schema.md`, then write the separate
implementation plan using `references/plan-schema.md`. Complete both before worker handoff.

## Grill until nothing load-bearing is unclear

An unclear spot is one that changes **what you would build** — scope, a contract, a policy
the request never states, a tradeoff with no obvious default. Do not pick the reading you
like. Apply `../pj/references/planning-decisions.md` and settle it with the user.

An unclear spot that a doc or the codebase can answer is not unclear: read it instead.
the shared decision policy is for what only the user knows.

Ask one thing at a time. A compressed list of five decisions returned for bulk approval
reads as thoroughness and produces rubber-stamping; the answers you get back are worth less
than the ones you get by unfolding them one at a time.

The opt-out is the user's word, not your judgment: if the invocation (or the boot prompt
pj-open-ws composed) says 묻지 말고 · 쭉 진행 · 알아서 · 확인 없이, skip grilling entirely,
decide each open point yourself, and record every judgment call in the plan so the user can
reverse it. Silence is what they asked for, not invisibility.

**Fold what you settle into the plan itself.** A decision recorded only as a line in a
decision log has not been applied — if the user narrowed the scope, the 범위 section changes;
if they picked an approach, 접근 and 작업 단계 change. The log says *why*, the document says
*what*.

This includes changing an existing project module boundary or interface. Use the shared decision policy with the
user in normal execution, or decide autonomously under pj-night; apply the shared policy's
baseline/deviation recording. Do not send an architecture approval request to the project board.

## Solo tasks: this session is also the worker

pj-open-ws opens a task either split (planner pane + waiting worker pane) or solo (one pane, one
session — the user asked for 한 세션 / solo at 착수). The boot prompt says which ("Solo session:
plan and implement here"), and the transport is the authority when in doubt:

```bash
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" topology --slug "$SLUG"   # PJ_TOPOLOGY=solo|split|unknown
```

Solo changes exactly two things here. The handoff in step 8 is still requested — cautions and
references are validated and recorded the same way — but the transport delivers it to nobody
(`PJ_CMUX=delivered … self=true`): there is no waiting pane, and it never types into the session
that is asking. Then, instead of step 9, you invoke **`/pj-work {slug}`** yourself, in this session,
and carry on. Everything before that is unchanged — the plan is not lighter because the same
session will build it, and the grilling rules apply as written. A solo task has no planning review
(pj-review records it skipped), so the second act below does not apply.

Do not decide solo from the size of the task or your own preference. `PJ_TOPOLOGY=split` with a
solo-sounding prompt means a worker is waiting: hand off to it.

In solo the plan viewer is already up: pj-open-ws opened the document beside this session at boot,
and it fills in live as you write. Step 7's `markdown.open` is still required — it confirms the
viewer is alive and reopens it only if the user closed it — so the procedure does not change. That
viewer's pane is the task's **side pane**: `topology --slug` prints `side_pane=… side_surface=…`,
and a document worth putting in front of the user goes there as a tab — `cmux open <path> --pane
<side_pane> --no-focus` — never by splitting this session's own pane. The code reviewer opens as a
tab in that pane for the same reason.

## Pick this task's reviewers

Every task gets the **code** reviewer and, in a split task, the **plan** review this session
performs itself. Those are not yours to choose. What you choose is which of the optional
reviewers this task also needs — and you choose it because you are the one who just read the
code. The worker receives a plan, not your investigation; by review time nobody else knows
whether this work touches an auth boundary or a schema.

List the reviewers by id, discovered from their own folders — never from memory:

```bash
ls "$SKILL_DIR/../_shared/templates/pj/reviewers"
python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["title"],"|",d["when"])' \
  "$SKILL_DIR/../_shared/templates/pj/reviewers/<id>/reviewer.json"
```

Each definition's `when` field states the condition to judge, and the judgment is **mechanical,
not aesthetic**: apply the condition to the files this plan will actually change. Do not pick one
because the task feels important, and do not pick every reviewer to look thorough — each one is a
session, a pane, and a finding set the worker must disposition.

A reviewer you leave out is a lane nobody reviews. That is a real cost, and it is the cost you
are weighing; when the condition is met, pick it even if the change looks small.

Record the picks in `## 작업 인계` as their ids with one line each on why, and pass the same ids
on the handoff in step 8. Both, for the same reason cautions are in both: the document is what a
human reads and the event is what the transport reads.

The recorded review profile selects an option across the selected reviewer lenses; it does
not itself add a reviewer. Preserve any later profile/option request in handoff `cautions`
for pj-review to resolve with `launcher parse --role review --reviewer <id>`.
An explicit request to **add** a Grok cross-check selects the optional `grok`
reviewer alongside `code`; asking to run the existing code reviewer on Grok is a launcher
override, not an additional reviewer. Preserve any requested Grok model/effort and matching configured profile ID in the handoff
`cautions` so pj-review can apply it on that reviewer's first request. Do not add transport fields.

## Select simplification before review

Decide whether the completed implementation should run `/pj-simplify {slug}` before review.
Consider likely duplication across implementation steps, intricate control flow, scattered
responsibilities, or abstractions that could obscure a straightforward change. A large diff of
mechanical edits alone is not a reason to select it. Honor an explicit user run/skip preference.

Record `실행: run` or `실행: skip`, the reason, and the bounded target files/areas under
`## 작업 인계` → `### Simplifier`. For run, place the phase after implementation and before
pj-review in the work steps. Mirror the selection as one 작업 유의사항 line, which travels in
the existing handoff `cautions` list; do not add an unsupported transport field or reviewer id.
The skill executes in the worker session and returns its result; independent reviewers then
inspect the final diff. A task with no useful cleanup opportunity should explicitly select skip.

## Procedure

1. **Parse the slug** — first token. None → let pj-dispatch derive it using pj-repos, which
   supports both a worktree and the parent of several. Still ambiguous → ask.
2. **Confirm you're in the right worktree**:
   `"$SKILL_DIR/../_shared/scripts/pj-dispatch.py" --slug "$SLUG"` and require `work`.
   `goto` / `open-ws` / `ask` → follow pj-open-ws's dispatch table; there is nothing to plan
   against here.
3. **Read the plan document** (the 최초 요청) and load context per the section above.
4. **Investigate and design the task's code architecture**, per above. Resolve file/symbol
   responsibilities, interfaces, data flow, and dependencies before writing the implementation
   sequence. Do not edit product code during planning.
5. **Grill** what's still unclear, per above.
6. **Write the two documents.** Put the code blueprint in code-archi using
   `references/code-archi-schema.md`. Put the implementation sequence in the separate plan using
   `references/plan-schema.md`, ending with `## 작업 인계`: link code-archi, record the optional
   reviewers and explicit simplifier selection. Do not paste the blueprint into the plan.
7. **Verify both documents are saved and linked, then open the finished plan in cmux Markdown
   viewer** — through the transport, after the final
   write:

   ```bash
   "$SKILL_DIR/../_shared/scripts/pj-cmux.py" request markdown.open --slug "$SLUG"
   ```

   The transport opens the canonical vault document (the panel watches the file live, so it must
   be that file, not a copy). A viewer failure **blocks the handoff**: the worker must not start
   from a plan the user was never shown. Normally the viewer is already up — pj-open-ws opened it
   at boot (split: a tab in the worker pane; solo: a pane beside this session) — and this call
   only confirms it is alive, reopening it in the same place if the user closed it.
8. **Hand off to the waiting worker** — only after the viewer opened successfully:

   ```bash
   "$SKILL_DIR/../_shared/scripts/pj-cmux.py" request worker.handoff --slug "$SLUG" --stdin <<'PJEOF'
   {"cautions": ["<each 작업 유의사항 bullet, verbatim>"],
    "references": [{"path": "<each 참조 문서 path>", "reason": "<its one-line reason>"}],
    "reviewers": ["<optional reviewer id>", "..."]}
   PJEOF
   ```

   The payload mirrors 작업 인계 (the document stays authoritative; the event is the durable
   record). Include code-archi in `references`. For the default vault document, use
   `raw/tasks/{project}/design/{slug}/code-archi.md` and state "vault-relative" in its reason;
   the worker resolves this canonical task path against the vault root, not its repo cwd.
   For a repo design document, use its repo-relative path (repo-folder-prefixed in a combined
   session). Keep clickable absolute links in the plan when helpful, but do not put an absolute
   path or `..` in the event: the existing transport rejects them. Check that the referenced
   file is readable before sending. `reviewers` is the selection from
   the section above: the OPTIONAL ids only, omitted or `[]` when the task needs none. The
   default reviewers are not listed and passing one is refused — they always run, and naming them
   would imply you could also leave one out. An id that is not a reviewer folder is refused here,
   while you can still fix it, rather than at review time.

   The transport validates the fields, verifies the worker pane still holds a live agent, and
   injects only the fixed `[pj-event-ready]` line; the worker cold-loads everything else from
   disk. Never invoke pj-work in this session (solo tasks excepted — see above), and never paste
   plan content into any prompt.
9. **Split: stay alive.** Report in the user's language that the handoff was delivered ("워커에 인계했습니다 —
   구현이 끝나면 이 세션이 계획 리뷰를 맡습니다") and end the turn. Do not exit, do not keep
   working the task — your next turn is a wake-up from the worker.
   **Solo: continue.** The handoff came back `self=true`. Say the plan is recorded ("계획 기록
   완료 — 같은 세션에서 구현으로 이어갑니다") and invoke `/pj-work {slug}` now.

## Planning review — this session's second act

Split tasks only: a solo task has no separate planner to ask, and pj-review records its plan review
as skipped rather than letting the implementer grade its own plan.

A `[pj-event-ready] project=… slug=… source=worker event=…` line arriving here is the worker
asking this session for something. Read the event first:

```bash
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" event read --slug "$SLUG" --event-id "$EVENT_ID"
```

For a `review_requested` event, first claim startup once:

```bash
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" request review.started --slug "$SLUG" --role plan \
  --reply-to "$EVENT_ID" --round "$ROUND"
```

Continue only for `PJ_REVIEW_STARTED=started`. `duplicate` means this exact request was already
started: do not perform a second review. This ends only startup monitoring, not the review.
If `event prompt` already instructed you to make this claim, do not claim it a second time.

**`review_requested` (role=plan)** — the implementation is done; review the RESULT against the
INTENT you planned. Use the worker's task worktree from `payload.requester.cwd`, not this
planner's checkout. For each target repo, pair its `diff_base` and `diff_head` values by repo
name (a bare ref applies to all target repos) and run this read-only helper in that task worktree:

```bash
python3 "$SKILL_DIR/../_shared/scripts/pj-review-diff.py" --base "$DIFF_BASE" --head "$DIFF_HEAD"
```

Review the manifest and cumulative patch: current-branch commits, staged and unstaged changes,
and untracked new files. Review happens before the final commit; do not require a commit or use
only `base...head`. An error, or an empty scope across all target repos, is not a zero-findings
review. Send a `blocking` finding with `plan_reference: "review-scope"`, the command/output as
evidence, and a request to correct the scope. A repo with no changes is fine when another target
repo has reviewable changes. PJ does not require type checks; follow an explicit user request
or consuming repository policy.
Your lens is intent, scope, and acceptance: did it solve the 최초 요청, follow the agreed 접근,
respect 목표's non-goals and the 인계 cautions, meet 확인 방법? Mention a technical defect ONLY
when it breaks an agreed behavior — code quality is the independent reviewer's lens, not yours.
You still hold the reasoning behind the plan; that context is why this review is yours. Then:

```bash
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" request review.reply --slug "$SLUG" --role plan \
  --round "$ROUND" --stdin   # {"findings": [{severity, plan_reference, finding, evidence,
                             #   recommended_change}]} — zero findings → {"findings": []}
```

Always reply, even with nothing to report — the worker cannot close the round on silence. For a
round > 1 request, recheck ONLY the findings the event names. **Never edit implementation
files** — a fix you'd love to make is a finding, not an edit.

**`review_start_failed`** — a reviewer requested from this session did not start. Read the
recorded task/tab/reason and use `pj-watcher` to inspect it once. Startup waiting, dialog handling,
and bounded resends belong exclusively to the watcher. Do not poll, press Enter, run a retry
loop, or create another `review.request` merely because startup timed out. `already-delivered`
means the transport recorded delivery, not that the reviewer started; it is not permission to
bypass the watcher with a new request. Report the failed request and stop automatic recovery for
that reviewer. Other reviewers can continue. Do not treat failure as zero findings or claim to
be watching a terminal failed request in the background. A new request in the same round is
allowed only after a concrete startup cause has actually been corrected; record the cause and
correction first. A live idle tab or an assumption that the wake-up was lost is not a correction.

**`decision_request`** — the worker or its simplification phase hit a point the plan doesn't
decide. Answer factual questions from code and honor already settled choices. For a new design
choice, use the shared decision policy with the user in normal execution, including project boundary changes;
pj-night/no-question mode decides autonomously and records the result. Update the code-archi
document, the plan's work steps, and handoff before replying; do not escalate to the board. Reply with
`request decision.reply --slug … --reply-to <event-id> --stdin` (`{"decision": …, "reason": …}`).

A repeated wake-up for an event you already replied to is a no-op — say so and stop.

## Pause / refuse

- Dispatch verdict isn't `work` → follow the table; never plan against another task's branch.
- No plan document at `raw/tasks/{project}/{slug}.md` → the task was never filed through pj-task-regi.
  Say so and send them to `/pj-task-regi`; do not create the file yourself, or the list and the
  documents drift apart with no way to tell which is real.
- 최초 요청 is empty and nothing in context says what the work is → ask the user. Never infer
  a scope from the title alone.
- The repo's docs contradict each other on a design choice → resolve it through the shared
  decision policy: the shared decision policy in normal execution; decide and record under explicit pj-night.
- `markdown.open` fails (relay feedback says failed/pending) → keep the completed plan document,
  report its absolute path and the transport error, and stop before the handoff. Opening the
  canonical plan is part of this stage's completion; do not downgrade it to a printed command or
  skip ahead.
- `worker.handoff` delivery fails (the worker pane was closed or its agent died) → the failure is
  the answer, not an obstacle: report it, tell the user to reopen the worker pane with the
  launcher recorded in the task's worker stream, then re-run
  `pj-cmux.py retry --request-id <id>`. Never fall back to implementing in this session — solo
  is chosen at 착수 and shows up as `self=true`, never as a failed delivery.
- The user waived grilling but a question is genuinely unanswerable without them (a
  credential, an unavailable repo, or an inaccessible external fact) → in pj-night record the
  blocker and stop the task without a question or done report. Outside night, report what input
  is missing. Do not invent facts; design judgments themselves remain autonomous when waived.
