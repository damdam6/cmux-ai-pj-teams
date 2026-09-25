# PJ transport contract

All cross-session operations go through `_shared/scripts/pj-cmux.py`. Skills provide semantic
actions (`workspace.board`, `workspace.open`, `worker.handoff`, `decision.request`,
`decision.reply`, `review.request`, `review.reply`, `done.report`, `markdown.open`), never raw
cmux send/paste commands. Use `request --help` and action-specific `--help` for exact flags.

Events are append-only JSONL under `$PJ_VAULT/raw/tasks/{project}/exchanges/`:
`board.jsonl` and `{slug}/{planner,worker,reviewer}.jsonl`, with additional reviewer streams as
specified by reviewer definitions. The transport locks appends, rejects symlinks, deduplicates
event IDs and truncates only incomplete final records after a crash. Do not edit event files.

A request creates an event. Delivery only means cmux accepted a wake-up. A receiving role reads
`event read` and explicitly acknowledges `processed`, `rejected` or `already-processed`.
Never treat delivery as successful implementation, review, or merge. Before sending to a session,
verify its workspace/surface, registered live CLI process and cmux tree. Failed validation stops;
never paste into a bare shell or substitute another surface. `pj-launch.py` supplies the registry
for standard CLI launches. Optional Grok can also use native cmux process detection.

Only a fixed single-line wake-up enters an existing agent. Findings, handoffs, document contents
and decisions stay in the event data. Templates under `_shared/templates/pj/` provide prompts
and fragments; do not add YAML frontmatter to prompt payload files.

Review scope comes from the worker's task worktree (`payload.requester.cwd`). Run
`_shared/scripts/pj-review-diff.py --base <project branch> --head <task branch>` in each target
repository. It compares the merge base to the current working tree and includes untracked files
without staging or committing. A bare ref applies to all named repositories; named base/head
refs must match by repository. An error or an empty scope across all targets requires scope
correction, not a clean review. Keep the worktree unchanged while reviewers read it.
Reused reviewers reload `event prompt` for the new event so current instructions apply.

## Relay and permissions

Claude/shell requests relay inline. Processes marked `CODEX_SANDBOX` or `CODEX_THREAD_ID` write a
`PJ_CMUX_REQUEST=req_...` marker and leave the request pending for relay outside that process.

The included Codex hook adapter supports the source runtime's `PostToolUse`/`Bash` hook contract;
this is a compatibility requirement, not a promise that every installed Codex supports it.
Use `pj-cmux.py hook install --codex --dry-run` to inspect the exact configuration first. If the
user requests hook installation, verify the installed runtime's hook support/trust behavior,
then install and test a real request. `hook doctor` checks file presence/configuration only.
Global hooks are optional and are never installed by the package installer. A hook extracts up
to four request markers from its input; it does not prove their relationship to one tool call.
Restrict this adapter to a trusted local setup if choosing to enable it.

Without a supported/trusted hook, execute this from an authorized shell that can access cmux:

```bash
python3 "$PJ_PACKAGE_ROOT/_shared/scripts/pj-cmux.py" relay --request-id req_ID
```

Manual relay is required for every pending request; unattended Codex execution requires a
working automatic relay. A permission failure must be reported, not bypassed. Failed requests
remain inspectable with `status` and retryable with `retry --request-id ...` after resolving
the failure. Do not implement in the planner merely because worker delivery failed.

cmux capabilities used: workspace/group creation and enumeration, terminal layouts and surfaces,
tree/process inspection, buffer/send operations, workspace color and Markdown viewing. The local
transport tests simulate cmux; actual compatibility must be verified against the user's install.

## Workspace boot and night checks

**File-backed workspace boot.** `workspace.open` saves planner and worker prompts under
`exchanges/{slug}/boot/{role}-{sha256}.txt`; `workspace.board` uses `exchanges/boot/board-{sha256}.txt`.
Files are UTF-8, mode 0600, content-addressed and retained for retries. Layout commands contain
only a one-line file read plus the allowlisted launcher. Quoted command substitution passes the
body as one argument without shell evaluation; a sentinel preserves trailing newlines and a
read failure prevents launch. Prompt text must never be pasted as a multiline shell command.
Reviewer boot keeps its existing fixed instruction to read `event prompt`.

**Night merge gate.** `night-check --slug` requires a running in-flight task and processed replies
to every required reviewer's latest request in that night run. New review groups carry `night_run_id`
to distinguish even same-second restarts. Active-night `review.complete` and `done.report` enforce
this gate too; daytime terminal-failure semantics remain unchanged. The board must call
`night-queue.py check --project … --slug …` again before an automatic merge, including on direct
wake-ups. A failed/skipped lane is terminal for waiting but never a successful unattended review.

## Reviewer startup watcher

`review.request` records monitoring state before delivery or tab creation. Relay binds the
exact tab and ensures one Python monitor per project. At board entry, `request watcher.start`
also opens/reuses the watcher console in the board pane. It uses the board profile/account,
never a requesting worker's profile. See [pj-watcher](../../pj-watcher/SKILL.md) for controls,
known startup dialogs and bounded recovery. The helper is `_shared/scripts/pj-startup-watch.py`.

Pending startup is `review_launch_queued` / status `starting`. Delivery alone is not startup.
`review.started --reply-to <request-event>` is a local idempotent claim by the registered tab;
`started` ends monitoring only. A duplicate claim must not start a second review. A recorded
review reply also proves startup for older reviewers. `review_start_failed` is terminal startup
failure, not a review result or a successful night review. The console and original requester
receive the same failure pointer. Recovery requires correcting and recording a concrete cause.

The monitor polls every 10 seconds with a 180-second request limit, at most 4 known dialog
actions and 2 same-event resends. It stops reading/typing as soon as the request starts. Unknown
screens and login/command approval are not accepted. Exact registered worktree trust dialogs
are the only folder-trust case. No direct daemon/start/tick is allowed from a sandbox; use the
normal request/authorized relay boundary. Test fixtures never start real cmux sessions.

State is append-only exchange data; the daemon and request locks serialize startup work.
`exchanges/watcher.log` retains daemon errors. Board registry context from `pj-launch.py`
records profile, cwd and effective Codex account path without copying credentials.
