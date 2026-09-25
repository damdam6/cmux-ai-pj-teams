---
name: pj-watcher
description: >
  Open the board's watcher AI tab and inspect, start, or stop its reviewer startup monitor. Use for /pj-watcher,
  "리뷰어 시작 감시", "리뷰어 탭이 안 시작해", "업데이트 창에 멈췄어", or watcher status requests.
  It watches startup only, handles allowlisted startup dialogs, resends the same review request,
  and stops monitoring each tab when that exact request acknowledges review.started.
---

# pj-watcher

Read `../pj/references/portable-setup.md` and resolve `SKILL_DIR` to this skill's real directory.

One AI tab named `watcher` beside the board tab in the same pane, plus one background Python
monitor per project board. Python holds one monitoring record per review request and performs
the periodic checks. The AI tab handles status and failure diagnosis when woken; it does not
run a second polling loop. Use the user's language for replies; preserve event and status keys.

## Open the watcher tab

From the board session, run:

```bash
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" request watcher.start --project "$PROJECT"
```

This opens or reuses a tab named exactly `watcher` in the board's pane, without taking focus.
The watcher reuses the board's exact configured launcher profile, including its account env.
There is no fixed model, effort, or personal launcher alias. Claude, Codex, Grok and custom
profile IDs use their declared runtime. The recorded board workspace profile takes precedence;
a manually opened board can use the `pj-launch.py` registry profile. If neither identifies it,
supply `--board <profile-id>` from the board session. Never infer the board from a worker.

The bundled launcher records cwd/profile and effective Codex account path. An older board can
supply its own context by requesting startup from that board tab; otherwise restart through the
updated wrapper or configure `CODEX_HOME` in the explicit profile's env. Never guess an account.
Repeated starts
reuse the recorded tab while it exists, including while it is still booting. If it contains
a stopped CLI or startup dialog, inspect that tab instead of creating duplicates.

## Console procedure

When booted as the watcher console, do not run `request watcher.start` again. Run `watcher status`
once and yield. On a `review_start_failed` event pointer, read that event with `event read --slug
<slug> --event-id <id>`, inspect watcher status once, and report the recorded task/tab/cause.
Inspect only the affected tab if needed for diagnosis. Do not poll or compete with Python by
pressing Enter or resending messages. Do not infer a lost wake-up from an idle tab alone.
The original requester also receives the failure and stops automatic retries. Recovery follows
the policy below; unresolved failures remain visible. The console does not review code or mark
a review/task complete. Python continues monitoring pending starts while the console is idle.

## Automatic registration

`pj-cmux.py request review.request` registers the request before delivery or tab creation.
The relay starts the Python monitor if needed and binds the created/reused tab to that request.
Worker and planner sessions never maintain a second registration list. The watcher handles both
new reviewer sessions and reused sessions, including the original planner's review request.
The requester submits once per reviewer/pass and yields; only this watcher waits for startup,
handles dialogs, and resends the original event. Requesters must not add their own polling or
resend loop. `already-delivered` from transport retry is a no-op, not a startup acknowledgement
or an instruction to generate a replacement request.

Run `request watcher.start` once when a board starts, so the console is present and even a
request whose relay never ran can time out visibly.

This uses the normal relay/hook boundary. Do not start a detached cmux controller from a sandbox.
Repeated starts are harmless: one process holds the project lock. State persists in exchange
events and resumes after a process restart. An idle watcher stays available until stopped.

## Inspect or stop

```bash
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" watcher status --project "$PROJECT"
"$SKILL_DIR/../_shared/scripts/pj-cmux.py" watcher stop --project "$PROJECT"
```

Status lists the console tab, exact task, request event, reviewer tab, startup state, retry counts,
and failure reason.
`stop` ends monitoring without closing agent tabs. Stop the watcher when closing the board.
A later reviewer launch starts it again automatically. Starting it resumes pending records;
a timed-out/failed request is not silently retried.

## Startup policy

- Poll every 10 seconds. Each request has a 180-second startup limit, at most 4 dialog actions,
  and at most 2 fixed-message deliveries after launch. A locked/launching request is skipped
  for that poll so other tabs continue to be checked.
- Match complete known startup dialogs and verify a live matching CLI on the exact tab before
  typing. Never press Enter periodically or type into a shell showing stale agent output.
- Recognize Codex's `› Ask Codex to do anything` composer as ready as well as known Claude
  input hints. Busy and approval indicators take priority over input placeholders. Do not
  assume an unknown screen is ready without a verified input-state signature. Grok's empty
  boxed `❯` composer with a `Grok <version> (<effort>)` status border is ready; its
  `always-approve` status label is not an approval request. Actual approval dialogs or busy
  indicators still take priority, and a composer containing draft text is not empty.
  This recognition does not change the CLI's permission mode or approve a command.
- Updates: select an explicitly displayed Skip/Later option; never install an update.
- Folder trust: accept only an exact displayed path resolved as this task's registered
  worktree (or its combined parent). Unknown roots remain blocked.
- Enter: only known startup welcome or completed-update acknowledgements. Login, arbitrary
  permissions, command execution prompts and unknown choices are not auto-approved.
- After resolving a dialog, wait for an idle input state and resend the original event id and
  round. Never create a new review round, replace the reviewer session, or resend while busy.
- `review.started` claims the exact request once. A duplicate wake-up cannot start another
  review. A recorded reply also proves startup for older reviewers.
- On started, complete only that request's monitoring and stop reading/typing into its tab.
  `review.reply` remains the review result; started never means reviewed or approved.
- On timeout, missing process/tab, or unhandled dialog, record `review_start_failed` and wake
  the watcher console and original requester with the same fixed event pointer. If either
  recipient is gone, retain its notification failure in status; never type into its leftover shell.
  The failure event includes the observed screen and action/resend counts for diagnosis.

On failure, inspect status once, show the tab and recorded cause, and stop automatic recovery for
that reviewer. A failed record is terminal; do not say it is still being monitored. Never create
a replacement request or round just to reset timeout/resend limits. A live idle tab or a suspected
lost wake-up does not justify resubmission. If a concrete cause is actually corrected, record
the cause and correction before issuing one recovery request for that reviewer in the existing
round. Otherwise leave it failed and report the unresolved cause. Do not report a failed startup
as a zero-findings review. The watcher never resumes a review after its startup was acknowledged.
