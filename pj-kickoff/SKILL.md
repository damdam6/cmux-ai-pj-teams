---
name: pj-kickoff
description: >
  One-shot: a new topic becomes a filed pj task in the current project and then a live worktree
  workspace. Thin orchestrator that chains pj-task-regi and pj-task-start, so it normally runs in a
  project's board session. Use whenever the user hands over a
  NEW topic and wants to start on it now — "이거 하자", "이거 작업 시작하자", "새 작업 킥오프",
  a topic paired with per-role launcher hints ("plan claude, work codex, review codex") or a bare model hint
  ("claude로 ㄱ", "grok으로 ㄱ"), a one-session request ("한 세션으로", "solo로"), or
  "/pj-kickoff Item 검색창 문제" / "$pj-kickoff Item 검색창 문제"
  — even without the words "worktree" or "task". To file without starting, pj-task-regi; to
  start something already filed, pj-task-start; to see the list, pj-board.
---

# pj-kickoff

Read `../pj/references/portable-setup.md` before running commands. Resolve `SKILL_DIR`
to this file's real parent and load the configured paths there. Launcher hints are profile
IDs from `launchers.local.json` (defaults: `claude`, `codex`, `grok`); never invent a model
alias or execute a shell function. Use the user's language for prose; retain the Korean
status/section keys in the on-disk task schema because the scripts parse them.


Thin orchestrator: `pj-task-regi` → slug → `pj-task-start`. It owns only the sequencing.
Slug rules, the plan document skeleton, and dependency recording live in pj-task-regi;
dispatch, worktree bootstrap, and the 진행 중 transition live in pj-task-start. Do not
duplicate or inline their procedures — invoke them through the current runtime's skill mechanism
(`/pj-*` in Claude Code/Grok, `$pj-*` in Codex) so each runs under its own instructions. Never
refuse a worker because the current runtime is Codex or Grok; the pj umbrella exposes the same
worker sources to all three runtimes.

The only bash this skill runs itself is `pj-cmux.py launcher parse` — launcher policy has
exactly one source, and it is not prose in any skill file.

Use the user's language for prose; preserve the task schema keys.

This worker is runtime-neutral. Claude Code and Grok call skills as `/pj-*`; Codex calls them as
`$pj-*`. Translate every downstream skill call to the current runtime and never refuse work merely
because the executor is Codex or Grok.

## Scope: this session only

The chain stops where the session does. pj-task-start hands to pj-open-ws, which boots a
**fresh session** in the new workspace, and that session runs pj-plan → pj-work → pj-review on
its own. You cannot reach into it, so don't describe those stages as things you will do — say
they are underway and stop.

## Procedure

1. **Parse the invocation.** The argument is the **topic** (empty → ask; never invent a task).
   Route the optional extras, leaving them embedded otherwise:
   - a **design/spec document** the user points at → goes to pj-task-regi, which may split it
     into several tasks rather than one (all inside one project),
   - **role launcher hints** → see step 1a below,
   - **topology** — 한 세션 / 단일 세션 / solo (planner and worker in ONE session) → the token
     `mode=solo`, carried to pj-task-start verbatim. The default is split (two panes); never
     infer solo from a task sounding small,
   - **reference docs** → pj-task-start,
   - **downstream switches** — 묻지 말고 / 쭉 진행 (waives pj-plan's grilling) and 리뷰 없이 /
     리뷰 생략 (waives the review dispatch) → pj-task-start, which carries them into the
     boot prompt. Any, all, or none may be present; route each on its own.

1a. **Normalize the three role launchers.** The task runs as three independently launched
   roles — planner, worker, reviewer — and each can use a different runtime/model/effort.
   Segment the user's words into per-role profile IDs: `plan`/`계획`, `work`/`구현`,
   `review`/`리뷰` (for example, "plan claude, work codex, review codex"). A bare profile
   applies to the planner only. Do not parse model/effort nicknames; configure a profile's
   standard CLI argv if the user needs model-specific settings. A review profile selects its
   configured `reviewOption` across all selected reviewers (default: its runtime); specialized
   reviewer defaults can differ within the option. Preserve a separate account's configured
   option and environment. See pj/references/portable-setup.md. Resolve every
   role, including the ones with no hint, in the one place launcher policy lives:

   ```bash
   "$SKILL_DIR/../_shared/scripts/pj-cmux.py" launcher parse --role plan   --hint "<words or empty>" --repo "$REPO"
   "$SKILL_DIR/../_shared/scripts/pj-cmux.py" launcher parse --role work   --hint "…" --repo "$REPO"
   "$SKILL_DIR/../_shared/scripts/pj-cmux.py" launcher parse --role review --hint "…" --repo "$REPO"
   ```

   Each prints `PJ_LAUNCHER=<code> role=<r> source=explicit|alias|default` — precedence is
   explicit hint > the repo alias's `roleLaunchers` > the global defaults, and this skill
   holds no mapping table of its own. Keep the three codes and their sources for the report.
   Under `mode=solo` resolve only `plan` and `review`: the worker role runs in the planner's
   session, so a `work` hint the user gave is told back as ignored (워커 역할은 planner 세션이
   겸함) rather than silently dropped or resolved into a token.
2. **Invoke `pj-task-regi`** with the topic. It issues the slug, files the entry, and writes
   the plan document skeleton.
3. **Gate.** It filed **more than one task** (a document split) → stop and ask which to start.
   Starting several at once is a choice with real cost — several worktrees, several sessions —
   and not one to make on the user's behalf. It filed nothing (unclear topic, collision) →
   stop there.
4. **Invoke `pj-task-start`** with the slug as the first token, followed by the normalized
   tokens verbatim — `plan=<code> work=<code> review=<code>`, or `plan=<code> review=<code>
   mode=solo` for a one-session task — then reference docs and switches. The tokens survive
   the hops unre-interpreted; downstream skills validate, they don't re-parse. The branch is cut from the task's **project branch** regardless of where you
   are standing, so kicking off from inside another task's worktree is fine — don't send the
   user anywhere first.
5. **Report and stop.** One Summary: slug, project, branch, worktree path, workspace
   name, the group it joined (if any), any dependency still open, that the planner is up and
   the worker is waiting (solo: 한 세션이 계획부터 구현까지 맡음) — and **every launcher used,
   with where each came from** (명시/별칭/기본값). Nothing about the model choice may resolve
   silently.
