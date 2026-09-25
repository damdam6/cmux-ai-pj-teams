# Planner, worker and reviewer sessions

- Board: project metadata, task filing, completion event processing and integration.
- Planner: task code architecture plus a separate implementation plan, decisions and planning review.
- Worker: implements the handoff and requests reviews; does not silently redesign the contract.
- Reviewer: task-scoped persistent code review; optional specialized reviewers use bundled definitions.

Split mode opens planner and worker panes. `pj-plan` reads project context, inspects actual code,
resolves important questions using [planning-decisions.md](planning-decisions.md), writes both
canonical documents and opens them via `markdown.open`. Its implementation plan ends with the
handoff section required by the schema. Only after successful preparation does it issue
`worker.handoff`. The planner stays available for `decision.request` and planning review.

Solo is selected explicitly (`mode=solo`): planner and worker are one session, handoff is recorded
as self-delivery, and planning review is skipped; code review still runs. A lost worker is an
error, not permission to switch topology. Solo does not authorize unattended design choices.

A one-repo session stands in that worktree. A multi-repo session stands in the parent containing
the repo worktrees, with runtime skill/config links combined by `wire-session-root.py`. The
parent is not itself a Git checkout. Resolve repositories through `pj-repos.py`; run Git
operations in each appropriate checkout. Tasks may target a subset of the project's repositories.

Use [portable-setup.md](portable-setup.md) for launcher profile selection. Runtime defaults are
Claude or Codex; each explicit role profile may use another supported runtime. A profile's
`runtime` supplies the skill sigil. The recorded review profile's `reviewOption` (defaulting
to its runtime) applies across the task's spawned reviewers. Code uses the recorded profile;
other reviewers use their configured default for that option. A custom account option names
its own profile, so it does not fall back to a different account. Reviewer definitions and
the option profile's `reviewers` overrides own those defaults.
Do not duplicate model tables in skills or assume profile names encode CLI flags.

`pj-review` uses persistent reviewers and records each finding/disposition by event ID. Fixes
are reviewed again against the changed scope. Completion requires the applicable review policy,
checks, a committed task branch and an explicit done request or authorized unattended run.
`pj-done` emits the report; only the board integrates with `pj-wrap`. Multi-repo integration is
sequential and recorded after each successful merge so partial failure can resume honestly.

Keep project color synchronized with `pj-sync-color`; workspace creation applies it directly.
Read [cmux-orchestration.md](cmux-orchestration.md) before changing any event or delivery behavior.
