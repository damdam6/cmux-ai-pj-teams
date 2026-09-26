#!/usr/bin/env python3
"""pj-cmux.py — PJ's cmux transport: semantic requests, exchange event streams, reviewed relay.

WHY THIS EXISTS
---------------
PJ runs a task across four independent agent sessions (board / planner / worker / reviewer).
Before this script, cmux policy was spread across skill prose and helpers: models composed raw
`cmux send`/`set-buffer` argv, review findings derived from a diff were auto-submitted into a
live session as a user turn, and a surface that had quietly become a bare shell could execute a
pasted payload line by line. This script centralizes all of it:

  * Skills and spawned agents express MEANING (`request worker.handoff --slug s`), never argv,
    surfaces, buffers, layouts, or launcher commands.
  * Cross-session data (handoffs, findings, dispositions, reports) lives as English JSONL
    events in append-only per-role streams under raw/tasks/{project}/exchanges/. The only text
    ever injected into a live session is the one fixed wake-up line; receivers read the real
    payload back through `event read`.
  * Execution happens in a reviewed relay (`relay`), normally invoked by a PostToolUse hook so
    a sandboxed tool process (Codex) never needs the cmux socket itself. The relay re-resolves
    policy from the slug, compiles argv from an allowlist, and runs subprocess(list, shell=False).

Design contract: pj/references/cmux-orchestration.md and
planner-worker-separation.md. Read those before changing behavior here.

THE REQUEST LIFECYCLE — two paths by runtime (hooks are CODEX-ONLY, user decision 2026-08-11)
---------------------
Claude Code's Bash tool, Grok's shell tool, and plain shells reach the cmux socket directly, so
for them `request`
appends the event and RELAYS INLINE in the same process — the caller sees delivered/failed in
the same tool output, and no hook is installed on the Claude side. Codex's sandboxed tool
process cannot reach the socket: there `request` appends the event and prints the
PJ_CMUX_REQUEST marker, and the codex PostToolUse hook (which runs OUTSIDE the sandbox)
relays it. Detection is by codex's own env marks (CODEX_SANDBOX / CODEX_THREAD_ID).

    agent            pj-cmux.py request <action>     appends one request event, then:
      |            Claude/Grok/shell → relay inline   codex → print PJ_CMUX_REQUEST=req_…
      v                                                 |
      |              codex PostToolUse hook           finds the marker in tool_response,
      |                (pj-cmux-codex-hook.sh)        relays in-process
      v
    relay             re-resolve → validate target → cmux → append delivered/ action_completed
      |                                              (or delivery_failed; never a fallback send)
      v
    receiver          pj-cmux.py event read / ack    processes the event, acks processed|rejected

`delivered` means cmux accepted the fixed wake-up into a VERIFIED agent surface. `processed`
means the receiver acted and acknowledged. They are different events and different claims.

Local record actions (review.group.open, review.disposition, review.skip) complete at request
time — they touch no live session, so they print no marker and need no relay.

STREAMS
-------
    raw/tasks/{project}/exchanges/board.jsonl            project-wide (board role)
    raw/tasks/{project}/exchanges/{slug}/planner.jsonl   per task
    raw/tasks/{project}/exchanges/{slug}/worker.jsonl
    raw/tasks/{project}/exchanges/{slug}/reviewer.jsonl

Appends: O_APPEND|O_NOFOLLOW under fcntl.flock(LOCK_EX), one line per event, fsync before
release. A crash can only leave an incomplete FINAL line; the next locked append truncates it
back to the last newline — the single permitted rewrite. Readers take LOCK_SH and drop a
partial tail. Every path component below raw/tasks is lstat-checked: symlinks are rejected.

An event is deduplicated by event_id + content digest: a replayed append of the same event is
a no-op, the same event_id with different content is a conflict (exit 4). Valid lines are
never edited. Same-request relay races are serialized by a per-request flock in
/tmp/pj-cmux-locks/ rather than by holding a stream lock across the cmux subprocess — role
streams cross-reference each other (a relay may append role_attached to two other streams),
and nested stream flocks would need a global ordering to stay deadlock-free. The remaining
crash window (cmux accepted the send, relay died before appending `delivered`) is inherently
uncertain — cmux has no idempotency key — and receiver monotonicity makes the retry harmless.
Default reviewer creation also takes one task-scoped lock so two simultaneous first requests
cannot create two persistent reviewers.

LOCK DISCIPLINE (learned in sync-color.py): this script never opens raw/tasks/index.md.
All project/task lookups go through pj-tasks.py as a subprocess BEFORE any exchange lock is
taken, and no subprocess runs while a stream lock is held.

TARGET VALIDATION
-----------------
Only actions that type into an EXISTING agent surface validate the target (worker.handoff,
reused review.request --role code, review.request --role plan, review.reply, decision.*,
done.report):

  1. expected workspace+surface from the latest role_attached event (or the project record for
     the board, or the requester recorded on a prior event as the pre-separation fallback);
  2. Claude/Codex: the /tmp/cmux-agents/<ws>/ entry exists and its pid is a matching live
     process; Grok: cmux's native `top --processes` reports a Grok process under that surface;
  3. one targeted `cmux tree` confirms the surface still belongs to that workspace.

Ordinary delivery does not poll read-screen; reviewer startup uses the board watcher.
NEVER a fallback to another surface: a failed check appends
delivery_failed and stops. A Grok board with no custom registry entry needs one read-only
`tree --all` to recover its workspace; role-attached task sessions always use the recorded one.
A live shell whose agent exited has no matching registry/native process and is rejected — the
case that used to execute pasted payloads.

SECURITY
--------
The wake-up line is the only text this script ever submits into a live session, and it is a
fixed template filled with validated identifiers. Review findings, handoff cautions, document
bodies, and anything else file-loaded or diff-derived stay event data. A spawned reviewer is
booted the same way: one fixed line naming `event prompt`, which renders its instructions from
a fixed template plus the validated review_requested event and a short worker-authored summary —
never the 최초 요청, never file contents. The full prompt used to be pasted into the TUI and
arrived with whole sections missing (the reply command among them) once it grew past a few KB.

TUI mechanics (ready markers, buffer paste, surface UUID discovery) are implemented here.
They require compatibility checks when the installed terminal or agent CLI changes.

USAGE
-----
    pj-cmux.py request <action> [flags] [--stdin]     # see ACTIONS below
    pj-cmux.py relay --request-id req_… [--slug s]
    pj-cmux.py retry --request-id req_… [--slug s]
    pj-cmux.py ack   --slug s --request-id req_… --status processed|rejected|already-processed
    pj-cmux.py event read --slug s --event-id evt_…   |  event list --slug s [--role r] [--type t]
    pj-cmux.py event prompt --slug s --event-id evt_… # a spawned reviewer's instructions for its review_requested event
    pj-cmux.py status --request-id req_… | --slug s [--round N]
    pj-cmux.py topology --slug s                      # PJ_TOPOLOGY=solo|split|unknown + side_pane=… (the plan viewer, opened at
                                                      #   workspace.open: solo → own pane beside the session, split → tab in the worker pane)
    pj-cmux.py launcher parse --role plan|work|review --hint "…" [--repo r] [--reviewer id]
    pj-cmux.py hook [run] --runtime claude|codex      # PostToolUse stdin → relay → hook JSON
    pj-cmux.py hook install --claude|--codex [--dry-run]  |  hook doctor

EXIT CODES
    0 ok (including no-op idempotent retries)     4 event conflict (same id, different content)
    1 usage / validation                           5 unknown request/event id
    2 environment (cmux/template/stream missing)   6 cmux runtime error
    3 target validation failed / refused
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import glob
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import stat
import shutil
import subprocess
import sys
import time
import types
import uuid

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()


# ---------- paths & policy ----------

# Storage root is configurable; see README.md.
VAULT = pathlib.Path(os.environ.get("PJ_VAULT")
                     or pathlib.Path.home() / ".local" / "share" / "pj")
TASKS_DIR = VAULT / "raw" / "tasks"
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
TEMPLATE_DIR = SCRIPT_DIR.parent / "templates" / "pj"
DATA_ALIASES = _aliases_path()
PJ_TASKS = SCRIPT_DIR / "pj-tasks.py"
SYNC_COLOR = SCRIPT_DIR.parent.parent / "pj-sync-color" / "scripts" / "sync-color.py"
WT_REGISTRY = SCRIPT_DIR / "wt-registry.py"
RESOLVE_WS_GROUP = SCRIPT_DIR / "resolve-ws-group.py"
# Overridable for the same single reason as PJ_VAULT: the end-to-end test runs this script as a
# SUBPROCESS against real repos and a scratch vault, so it cannot monkeypatch the constant the
# way the relay unit tests do — and without a registry it can reach, every delivery INTO a
# session (handoff, review reply, plan wake-up) fails target validation and the review round
# can never be driven to completion. Production never sets it.
REGISTRY_ROOT = os.environ.get("PJ_CMUX_REGISTRY_ROOT") or "/tmp/cmux-agents"
LOCK_ROOT = "/tmp/pj-cmux-locks"

# Runtime columns select default profiles. Explicit role profiles may override them.
# Profile argv and runtime identity come from the package launcher configuration.
RUNTIMES = ("claude", "codex")
DEFAULT_RUNTIME = "claude"
# Built-in review options are profiles. Custom account/model groups can name another
# configured profile via reviewOption; no shell-function naming convention is required.
REVIEW_OPTIONS = ("claude", "codex", "grok")
RUNTIME_REVIEW_OPTION = {"claude": "claude", "codex": "codex"}

# session role → (claude, codex). ONLY the roles a workspace boots.
#
# Reviewer launchers are NOT here. A reviewer is its own definition folder and carries its own
# launcher pair, so putting a copy in this table would make the same fact answerable from two
# places. `review` stays because `workspace.open` records a `launchers.review` for the task
# before any reviewer exists, and it resolves to whatever the `code` reviewer's definition says.
LAUNCHER_TABLE = {
    "plan": ("claude", "codex"),
    "work": ("claude", "codex"),
}
DEFAULT_REVIEWER = "code"
ROLE_KEYS = ("plan", "work", "review")

# Profile IDs resolve to standard CLI argv arrays, never personal shell functions.
from pj_config import launcher_profiles

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
UUID_RE_S = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
UUID_RE = re.compile(rf"^{UUID_RE_S}$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
# No leading \b: the hook scans RAW PostToolUse JSON, where a newline before the marker is
# the two characters `\n` — 'n' is a word character, so a boundary assertion never holds there.
MARKER_RE = re.compile(r"PJ_CMUX_REQUEST=(req_[0-9a-f]{16})\b")

ROLES = ("board", "planner", "worker", "reviewer")
# The one sidebar pill pj sets: on a task workspace when its dual review has finished and the
# task is waiting for the user's /pj-done. A stable key replaces rather than stacks the status.
REVIEW_DONE_PILL = {"key": "pj", "text": "pj work done", "icon": "checkmark.seal.fill",
                    "color": "#2E7D32", "priority": "100"}
REVIEWER_DIR = TEMPLATE_DIR / "reviewers"
REVIEWER_KEYS = ("title", "when", "default", "launchers", "spawn", "to", "stream",
                 "reply_type", "finding_field", "prompt", "solo")
SWITCHES = ("skip-review", "no-grill")
SEVERITIES = ("blocking", "important", "suggestion")
DISPOSITIONS = ("accepted", "rejected", "planner-decision", "user-decision")
ACK_STATUSES = ("processed", "rejected", "already-processed")

EVENT_TYPES = {
    "workspace_open", "workspace_board", "markdown_open", "color_sync", "worker_handoff",
    "review_group_opened", "review_requested", "code_review", "plan_review",
    "review_disposition", "review_skipped", "review_complete", "decision_request",
    "decision_reply", "done_report", "role_attached", "delivered", "delivery_failed",
    "action_completed", "ack",
    "review_started", "review_watch", "review_start_failed", "watcher_control",
    "watcher_start", "watcher_attached", "review_launch_queued",
}

# Terminal tree parsing and readiness markers.
PANE_LINE = re.compile(rf"\bpane\s+pane:(\d+)\s+({UUID_RE_S})")
SURFACE_LINE = re.compile(rf"\bsurface\s+surface:(\d+)\s+({UUID_RE_S})\s+\[(\w+)\]\s+\"(.*?)\"")

# Pane 2 normally waits for the planner to register in the cmux agent registry (60s cap) before
# its own launcher starts, so pane 1 deterministically becomes the workspace lead. Grok uses
# cmux's native agent integration rather than this registry and receives a short startup lead in
# deliver_workspace_open instead.
POLL_PREAMBLE = (f'i=0; until [ -n "$(ls -A {shlex.quote(REGISTRY_ROOT)}/"$CMUX_WORKSPACE_ID" 2>/dev/null)" ]'
                 ' || [ "$i" -ge 60 ]; do sleep 1; i=$((i+1)); done; ')

TEST = os.environ.get("PJ_CMUX_TEST") == "1"
# Run the local helper scripts for real while still stubbing cmux — see run_helper.
LIVE_HELPERS = os.environ.get("PJ_CMUX_LIVE_HELPERS") == "1"
EXECUTED: list[list[str]] = []  # argv log in test mode


def should_relay_inline() -> bool:
    """Hooks are codex-only: Claude's Bash tool and plain shells reach the cmux socket
    directly, so their requests relay in-process. Codex marks its tool processes with
    CODEX_* env; only those defer to the marker + PostToolUse hook path."""
    return not (os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_THREAD_ID"))


class Fail(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


_STARTUP_WATCH = None


def watcher(name: str, *args, **kwargs):
    global _STARTUP_WATCH
    if _STARTUP_WATCH is None:
        spec = importlib.util.spec_from_file_location("pj_startup_watch", SCRIPT_DIR / "pj-startup-watch.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _STARTUP_WATCH = module
    return getattr(_STARTUP_WATCH, name)(types.SimpleNamespace(**globals()), *args, **kwargs)


def review_start_line(project: str, slug: str, event: dict) -> str:
    return reviewer_wakeup_line(project, slug, event["from"], event["event_id"])


def op_watcher(a) -> int:
    return watcher("command", a)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def run_cmux(args: list[str], check: bool = True, timeout: float = 30) -> str:
    """The single cmux chokepoint. In test mode it records argv and executes nothing."""
    if TEST:
        EXECUTED.append(list(args))
        # The simulated CLI is idle; dialog tests provide their own read-screen fixture.
        return "? for shortcuts" if args[1:2] == ["read-screen"] else ""
    env = dict(os.environ, CMUX_QUIET="1")
    try:
        p = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Fail(6, f"cmux 응답 시간 초과 ({args[1]}, {timeout:g}초)")
    except OSError as e:
        raise Fail(6, f"cmux 실행 실패: {e}")
    if check and p.returncode != 0:
        raise Fail(6, f"cmux 오류 ({' '.join(args[:3])}…): "
                      f"{p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def run_helper(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Non-cmux helper subprocesses (pj-tasks.py, wt-registry.py, sync-color.py, …).

    Test mode records only — unit tests pass fixture paths that no repo backs, and actually
    running wt-registry.py against them would write into directories that do not exist.

    `PJ_CMUX_LIVE_HELPERS=1` records AND runs them. That is for the end-to-end test, which
    builds real repos and a real (scratch) vault and needs the helpers to actually compose:
    stubbing them means the only thing verified is that this module formats argv correctly,
    which is the half that was never in doubt.
    """
    if TEST:
        EXECUTED.append(list(args))
        if not LIVE_HELPERS:
            return 0, "", ""
    try:
        p = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    except OSError as e:
        raise Fail(2, f"헬퍼 실행 실패: {args[0]} ({e})")
    return p.returncode, p.stdout, p.stderr


# ---------- project / task resolution (always BEFORE any stream lock) ----------

def pj_tasks_json(*args: str) -> dict | list:
    rc, out, err = run_helper([str(PJ_TASKS), *args])
    if TEST and not LIVE_HELPERS:  # tests patch this function; the raw path never parses
        return {}
    if rc != 0:
        raise Fail(1, f"pj-tasks.py {args[0]} 실패: {err.strip() or out.strip()}")
    try:
        return json.loads(out)
    except ValueError as e:
        raise Fail(2, f"pj-tasks.py 출력이 JSON 이 아닙니다: {e}")


def task_info(slug: str) -> dict:
    if not SLUG_RE.match(slug or ""):
        raise Fail(1, f"slug 형식이 잘못됐습니다: {slug!r}")
    info = pj_tasks_json("get", "--slug", slug)
    if not isinstance(info, dict) or not info.get("project"):
        raise Fail(1, f"작업을 찾지 못했습니다: {slug}")
    return info


def repo_alias(repo: str) -> dict:
    try:
        data = json.loads(DATA_ALIASES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entry = data.get("aliases", {}).get(repo)
    return entry if isinstance(entry, dict) else {}


def project_color(project: str) -> str | None:
    """Registered color from project.md frontmatter — read-only, no lock (a stale read here
    costs one recolor, not a lost update)."""
    path = TASKS_DIR / project / "project.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    cm = re.search(r'^color:\s*"?(#[0-9A-Fa-f]{6})"?\s*$', m.group(1), re.M)
    return cm.group(1).upper() if cm else None



# ---------- launcher policy ----------

def valid_launcher(code: str) -> bool:
    return code in launcher_profiles()


def launcher_runtime(code: str) -> str:
    if not valid_launcher(code):
        raise Fail(1, f"Unknown launcher profile: {code!r}")
    return launcher_profiles()[code]["runtime"]


def launcher_shell_command(code: str) -> str:
    """Use a bundled registry wrapper around a configured standard CLI argv array."""
    launcher_runtime(code)
    return shlex.join([sys.executable, str(SCRIPT_DIR / "pj-launch.py"),
                       "--profile", code, "--"])


def valid_runtime(runtime: str | None) -> str:
    """The runtime to resolve against. None means the default rather than an error: every
    existing caller passes no runtime and must keep getting the column it always got."""
    if runtime is None:
        return DEFAULT_RUNTIME
    if runtime not in RUNTIMES:
        raise Fail(1, f"--runtime 은 {'|'.join(RUNTIMES)} 입니다: {runtime!r}")
    return runtime


def default_launcher(key: str, runtime: str | None = None) -> str:
    """The launcher for one session role. The only reader of LAUNCHER_TABLE.

    `review` is delegated to the default reviewer's own definition rather than duplicated here.
    """
    if key == "review":
        return reviewer_launcher(DEFAULT_REVIEWER,
                                 RUNTIME_REVIEW_OPTION[valid_runtime(runtime)])
    if key not in LAUNCHER_TABLE:
        raise Fail(1, f"런처 테이블에 없는 키입니다: {key!r} "
                      f"(가능: {', '.join(LAUNCHER_TABLE)}, review)")
    return LAUNCHER_TABLE[key][RUNTIMES.index(valid_runtime(runtime))]


def reviewer_def(role: str) -> dict:
    """One reviewer's definition, read from its own folder.

    A reviewer is `templates/pj/reviewers/<id>/reviewer.json` and the folder name IS the id —
    the value that has always been in the event's `role` field. Nothing here enumerates the
    reviewers, because a list in this file would be a second definition of what a reviewer is,
    and the moment it disagrees with the folders there is no way to tell which one is real.

    Every question the transport used to answer with `role == "code"` — where does the request
    go, which stream holds the reply, what is the finding's location field called, does a
    session get spawned — is a property of the definition and is read from here.
    """
    path = REVIEWER_DIR / role / "reviewer.json"
    if role != os.path.basename(role) or not path.is_file():
        raise Fail(1, f"--role 은 {'/'.join(reviewer_roles())} 중 하나입니다: {role!r}")
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Fail(2, f"리뷰어 정의를 읽을 수 없습니다: {path} ({e})")
    missing = [k for k in REVIEWER_KEYS if k not in spec]
    if missing:
        raise Fail(2, f"{path}: 빠진 키 {', '.join(missing)}")
    if spec["reply_type"] not in EVENT_TYPES:
        raise Fail(2, f"{path}: reply_type {spec['reply_type']!r} 이 EVENT_TYPES 에 없습니다")
    if spec["spawn"]:
        if not spec.get("prompt"):
            raise Fail(2, f"{path}: spawn 리뷰어에는 prompt 가 필요합니다")
        lens = REVIEWER_DIR / role / spec["prompt"]
        if lens.name != spec["prompt"] or not lens.is_file():
            raise Fail(2, f"{path}: prompt 가 이 리뷰어 폴더의 파일이 아닙니다: {spec['prompt']!r}")
    if spec["spawn"]:
        options = spec.get("launchers") or {}
        missing = [o for o in RUNTIMES if not valid_launcher(options.get(o))]
        if missing:
            raise Fail(2, f"{path}: spawn 리뷰어에는 옵션별 launchers "
                          f"({'/'.join(REVIEW_OPTIONS)}) 가 필요합니다 — 없거나 무효: "
                          f"{', '.join(missing)}")
    return spec


def reviewer_roles() -> list[str]:
    """Every reviewer id there is — the folders, in name order. Discovery, never a list."""
    if not REVIEWER_DIR.is_dir():
        return []
    return sorted(p.name for p in REVIEWER_DIR.iterdir()
                  if (p / "reviewer.json").is_file())


def review_option(launcher: str) -> str:
    """A configured group preserves account identity independently of the profile name."""
    launcher_runtime(launcher)
    option = launcher_profiles()[launcher].get("reviewOption", launcher_runtime(launcher))
    if not valid_launcher(option):
        raise Fail(1, f"Unknown review option profile: {option!r}")
    return option


def reviewer_launcher(role: str, option: str | None = None) -> str:
    """This reviewer's default launcher for one review option, from its own definition."""
    option = option or DEFAULT_RUNTIME
    if not valid_launcher(option):
        raise Fail(1, f"Unknown review option profile: {option!r}")
    spec = reviewer_def(role)
    if not spec["spawn"]:
        raise Fail(1, f"Reviewer {role} reuses its existing session, without a launcher")
    code = (launcher_profiles()[option].get("reviewers", {}).get(role)
            or spec.get("default_launcher")
            or (spec.get("launchers") or {}).get(option)
            or option)
    if not valid_launcher(code):
        raise Fail(2, f"리뷰어 {role} 에 {option} 런처가 없습니다")
    return code


def parse_launcher(role: str, hint: str, repo: str | None,
                   runtime: str | None = None,
                   reviewer: str | None = None) -> tuple[str, str]:
    """Resolve explicit profile > repo alias > runtime default.

    A review option profile selects each reviewer's own default. A more specific
    profile stays exact; its reviewOption only controls other task reviewers.
    """
    if role not in ROLE_KEYS:
        raise Fail(1, f"--role 은 {'/'.join(ROLE_KEYS)} 중 하나입니다: {role!r}")
    default, source = default_launcher(role, runtime), "default"
    if reviewer and role != "review":
        raise Fail(1, "--reviewer requires --role review")
    if role == "review" and reviewer:
        default = reviewer_launcher(reviewer, RUNTIME_REVIEW_OPTION[valid_runtime(runtime)])
    alias_val = (repo_alias(repo).get("roleLaunchers", {}) or {}).get(role) if repo else None
    if alias_val:
        if not valid_launcher(alias_val):
            raise Fail(1, f"repo-aliases roleLaunchers.{role} 가 유효한 런처가 아닙니다: "
                          f"{alias_val!r}")
        default, source = alias_val, "alias"

    code = (hint or "").strip()
    if not code:
        return default, source
    if not valid_launcher(code):
        raise Fail(1, f"Unknown profile {code!r}; configure launchers.local.json. "
                      f"Available: {', '.join(launcher_profiles())}")
    if role == "review" and code == review_option(code):
        code = reviewer_launcher(reviewer or DEFAULT_REVIEWER, code)
    return code, "explicit"


# ---------- templates ----------

def render_template(name: str, fields: dict[str, str]) -> str:
    path = TEMPLATE_DIR / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise Fail(2, f"템플릿을 읽을 수 없습니다: {path} ({e})")
    for k, v in fields.items():
        text = text.replace("{" + k + "}", v)
    leftover = re.search(r"\{[A-Z_]+\}", text)
    if leftover:
        raise Fail(2, f"{name}: 채워지지 않은 자리표시자 {leftover.group(0)}")
    return text.strip()


REVIEWER_COMMON = "reviewer.md"


def render_reviewer_prompt(role: str, fields: dict[str, str]) -> str:
    """The shared reviewer contract, then this reviewer's lens.

    Two files, not one: the contract — how to reply, the severity vocabulary, what a reviewer may
    not do, the noise floor — is identical for every reviewer, and a copy per reviewer is a copy
    that drifts. The lens is the only part that differs, so it is the only part that lives in the
    reviewer's own folder.

    The lens is appended verbatim and its `{…}` are NOT substituted: it is prose about code, and
    a brace in a code example is not a placeholder. Only the contract carries fields.
    """
    spec = reviewer_def(role)
    lens_path = REVIEWER_DIR / role / spec["prompt"]
    try:
        lens = lens_path.read_text(encoding="utf-8")
    except OSError as e:
        raise Fail(2, f"리뷰어 렌즈를 읽을 수 없습니다: {lens_path} ({e})")
    # The lens is spliced INTO the contract at {LENS}, not appended after it, so the prompt ends
    # on the required final action. Found live, twice: with the lens last, both reviewers read
    # their lens's closing bullets as the end of their instructions, wrote prose findings, and
    # went idle without ever running review.reply. The sentinel keeps the lens's own braces
    # (code examples) out of the placeholder pass.
    sentinel = "\x00LENS\x00"
    common = render_template(f"reviewers/{REVIEWER_COMMON}",
                             {**fields, "REVIEWER_ROLE": role,
                              "REVIEWER_TITLE": spec["title"], "LENS": sentinel})
    if sentinel not in common:
        raise Fail(2, f"reviewers/{REVIEWER_COMMON} 에 {{LENS}} 자리가 없습니다")
    return common.replace(sentinel, lens.strip()).rstrip() + "\n"


def render_fragment(name: str, fields: dict[str, str] | None = None) -> str:
    """One line spliced INTO a prompt, from templates/pj/fragments/.

    Fragments are files for the same reason whole prompts are: prompt text is authored and
    reviewed as text. What stays here is the choice of WHICH fragment — control flow, decided
    from a validated closed set (SWITCHES), never prose.
    """
    return render_template(f"fragments/{name}.md", fields or {})


def wakeup_line(project: str, slug: str, source: str, event_id: str) -> str:
    return render_template("event-ready-wakeup.md", {
        "PROJECT": project, "SLUG": slug, "SOURCE": source, "EVENT_ID": event_id})


def reviewer_wakeup_line(project: str, slug: str, source: str, event_id: str) -> str:
    """Persistent reviewers need an explicit completion reminder on every later request.

    The reminder is fixed control text; review scope and findings remain event data.
    """
    return render_template("reviewer-event-ready-wakeup.md", {
        "PROJECT": project, "SLUG": slug, "SOURCE": source, "EVENT_ID": event_id,
        "PJ_CMUX": shlex.quote(str(SCRIPT_DIR / "pj-cmux.py"))})


# ---------- exchange streams ----------

def exchanges_dir(project: str) -> pathlib.Path:
    return TASKS_DIR / project / "exchanges"


def stream_path(project: str, slug: str | None, role: str) -> pathlib.Path:
    if role == "board":
        return exchanges_dir(project) / "board.jsonl"
    if not slug:
        raise Fail(1, f"{role} 스트림에는 slug 가 필요합니다")
    return exchanges_dir(project) / slug / f"{role}.jsonl"


def assert_no_symlinks(path: pathlib.Path) -> None:
    """Reject a symlink anywhere between raw/tasks and the stream file. Exchange data must
    not be redirectable outside the vault by a planted link."""
    base = TASKS_DIR
    try:
        path.resolve().relative_to(base.resolve())
        # Inspect the original components: resolving first hides links whose target is
        # still inside raw/tasks, including a substituted boot prompt file.
        rel = path.absolute().relative_to(base.absolute())
    except ValueError:
        raise Fail(2, f"스트림 경로가 raw/tasks 밖입니다: {path}")
    cur = base
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise Fail(2, f"심링크는 exchange 경로에 올 수 없습니다: {cur}")


def digest_of(event: dict) -> str:
    core = {k: event.get(k) for k in ("type", "from", "to", "slug", "round",
                                      "reply_to", "payload")}
    blob = json.dumps(core, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def make_event(type_: str, from_: str, to: str, slug: str | None, payload: dict,
               round_: int = 0, reply_to: str | None = None,
               request_id: str | None = None) -> dict:
    assert type_ in EVENT_TYPES, type_
    ev = {"event_id": new_id("evt"), "request_id": request_id, "type": type_,
          "from": from_, "to": to, "slug": slug, "round": round_, "reply_to": reply_to,
          "created_at": now_iso(), "payload": payload}
    ev["digest"] = digest_of(ev)
    return ev


def _parse_lines(raw: bytes) -> list[dict]:
    events = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            ev = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue  # partial tail or hand-damage: readers drop, appender repairs
        if isinstance(ev, dict) and ev.get("event_id"):
            events.append(ev)
    return events


def read_stream(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    assert_no_symlinks(path)
    with open(path, "rb") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        try:
            raw = fh.read()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return _parse_lines(raw)


def append_event(path: pathlib.Path, event: dict) -> str:
    """Append one event. Returns 'appended' | 'duplicate'. Same event_id with different
    content is a conflict (exit 4)."""
    assert_no_symlinks(path.parent if not path.exists() else path)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_no_symlinks(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "ab") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            with open(path, "rb") as reader:
                raw = reader.read()
            # crash recovery: truncate an unterminated final line (the only rewrite allowed)
            if raw and not raw.endswith(b"\n"):
                keep = raw.rfind(b"\n") + 1
                os.truncate(path, keep)
                raw = raw[:keep]
            for ex in _parse_lines(raw):
                if ex.get("event_id") == event["event_id"]:
                    if ex.get("digest") == event["digest"]:
                        return "duplicate"
                    raise Fail(4, f"이벤트 충돌: {event['event_id']} 가 다른 내용으로 이미 "
                                  f"존재합니다 ({path})")
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            fh.write(line.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return "appended"


def task_streams() -> list[str]:
    """Every per-task stream name: the two session roles plus each reviewer's own stream.

    Reviewers get a stream each because with several of them one shared file cannot answer
    "whose reply is this" without parsing payloads — and the stream is a cheaper answer. `plan`
    declares the planner's stream and so collapses into it, which is what it has always done.
    """
    seen = ["planner", "worker"]
    for role in reviewer_roles():
        name = reviewer_def(role)["stream"]
        if name not in seen:
            seen.append(name)
    return seen


def all_stream_paths(project: str, slug: str | None) -> list[tuple[str, pathlib.Path]]:
    out = [("board", stream_path(project, None, "board"))]
    if slug:
        for role in task_streams():
            out.append((role, stream_path(project, slug, role)))
    return out


def load_events(project: str, slug: str | None) -> list[tuple[str, dict]]:
    """[(role, event)] across the board stream and (if slug) the three task streams."""
    out: list[tuple[str, dict]] = []
    for role, path in all_stream_paths(project, slug):
        out.extend((role, ev) for ev in read_stream(path))
    return out


def find_request(request_id: str, slug: str | None) -> tuple[str, str, dict, pathlib.Path]:
    """(project, source_role, event, stream_path) for a request id."""
    if slug:
        candidates = [(task_info(slug)["project"], slug)]
    else:
        candidates = []
        for p in sorted(glob.glob(str(TASKS_DIR / "*" / "exchanges"))):
            project = pathlib.Path(p).parent.name
            slugs = {None} | {q.name for q in pathlib.Path(p).iterdir() if q.is_dir()}
            candidates.extend((project, s) for s in slugs)
    seen = set()
    for project, s in candidates:
        for role, path in all_stream_paths(project, s):
            if path in seen or not path.exists():
                continue
            seen.add(path)
            for ev in read_stream(path):
                if ev.get("request_id") == request_id:
                    return project, role, ev, path
    raise Fail(5, f"요청을 찾지 못했습니다: {request_id}"
                  + ("" if slug else " — --slug 를 주면 빨라집니다"))


@contextlib.contextmanager
def request_lock(request_id: str, blocking: bool = True):
    """Serialize relays of the same request without nesting stream locks."""
    os.makedirs(LOCK_ROOT, exist_ok=True)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
        raise Fail(1, "잘못된 request lock key")
    directory = os.open(LOCK_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(request_id + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "r+") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Fail(3, "request lock은 단일 링크 일반 파일이어야 합니다")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield None
            return
        try:
            yield fh
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def reviewer_session_lock(project: str, slug: str, role: str):
    """Serialize discovery/creation of ONE reviewer's session.

    Keyed per reviewer, not per task: a round runs several reviewers and they spawn at the same
    time, so a task-wide lock would make them queue behind each other for nothing. Two requests
    for the SAME reviewer racing is what must not happen — each would see "no session yet".
    """
    os.makedirs(LOCK_ROOT, exist_ok=True)
    key = hashlib.sha256(f"{project}\0{slug}\0{role}".encode()).hexdigest()[:24]
    with open(os.path.join(LOCK_ROOT, "reviewer-" + key + ".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---------- payload validation ----------

def _s(payload: dict, key: str, limit: int | None, required: bool = True,
       pattern: re.Pattern | None = None) -> str:
    v = payload.get(key)
    if v is None or v == "":
        if required:
            raise Fail(1, f"payload.{key} 가 필요합니다")
        return ""
    if not isinstance(v, str):
        raise Fail(1, f"payload.{key} 는 문자열이어야 합니다")
    if limit is not None and len(v) > limit:
        raise Fail(1, f"payload.{key} 가 너무 깁니다 ({len(v)} > {limit})")
    if pattern and not pattern.match(v):
        raise Fail(1, f"payload.{key} 형식이 잘못됐습니다: {v!r}")
    return v


def check_reference_path(path: str) -> None:
    if path.startswith(("https://", "http://")):
        return
    if path.startswith("/") or ".." in path.split("/"):
        raise Fail(1, f"참조 경로는 저장소 상대 경로나 안정된 URL 이어야 합니다: {path!r}")


def validate_handoff(payload: dict) -> dict:
    cautions = payload.get("cautions", [])
    references = payload.get("references", [])
    if not isinstance(cautions, list) or len(cautions) > 10:
        raise Fail(1, "cautions 는 최대 10개의 문자열 리스트입니다")
    for c in cautions:
        if not isinstance(c, str) or not c.strip() or len(c) > 300:
            raise Fail(1, f"caution 항목이 잘못됐습니다 (비어 있거나 300자 초과): {c!r}")
    if not isinstance(references, list) or len(references) > 10:
        raise Fail(1, "references 는 최대 10개의 리스트입니다")
    for r in references:
        if not isinstance(r, dict):
            raise Fail(1, "references 항목은 {path, reason} 객체입니다")
        check_reference_path(_s(r, "path", 500))
        _s(r, "reason", 200)
    return {"cautions": cautions, "references": references,
            "reviewers": validate_reviewer_selection(payload.get("reviewers"))}


def validate_reviewer_selection(picked) -> list[str]:
    """The optional reviewers the PLANNER chose for this task.

    Chosen at the end of planning, not at review time: whether the work touches auth, raw
    queries, React render paths or a schema is something the planner learned while
    investigating, and the worker only receives the plan. Recording it on the handoff puts the
    decision where the reasoning was.

    Default reviewers are not listed — they always run, so naming them here would let a handoff
    that omits one read as "skip it". Passing one is refused rather than silently dropped: a
    planner that thinks it is selecting `code` is a planner that believes it could also
    deselect it.
    """
    if picked is None:
        return []
    if not isinstance(picked, list) or len(picked) > 8:
        raise Fail(1, "reviewers 는 최대 8개의 리뷰어 id 리스트입니다")
    out = []
    for r in picked:
        if not isinstance(r, str):
            raise Fail(1, f"reviewers 항목은 문자열 id 입니다: {r!r}")
        if reviewer_def(r)["default"]:
            raise Fail(1, f"{r} 는 기본 리뷰어라 고를 대상이 아닙니다 — 항상 실행됩니다")
        if r not in out:
            out.append(r)
    return out


def validate_findings(payload: dict, role: str) -> dict:
    loc_key = reviewer_def(role)["finding_field"]
    findings = payload.get("findings")
    if not isinstance(findings, list) or len(findings) > 30:
        raise Fail(1, "findings 는 최대 30개의 리스트입니다 (없으면 빈 리스트)")
    out = []
    for f in findings:
        if not isinstance(f, dict):
            raise Fail(1, "finding 은 객체여야 합니다")
        sev = _s(f, "severity", 20)
        if sev not in SEVERITIES:
            raise Fail(1, f"severity 는 {'/'.join(SEVERITIES)} 중 하나입니다: {sev!r}")
        # The prose fields are unbounded: findings reach the worker only through `event read`,
        # never a live session, and a length cap only bounced real evidence back for a retry.
        out.append({"severity": sev, loc_key: _s(f, loc_key, 300),
                    "finding": _s(f, "finding", None), "evidence": _s(f, "evidence", None),
                    "recommended_change": _s(f, "recommended_change", None)})
    return {"findings": out}


def validate_dispositions(payload: dict) -> dict:
    ds = payload.get("dispositions")
    if not isinstance(ds, list) or not ds or len(ds) > 30:
        raise Fail(1, "dispositions 는 1–30개의 리스트입니다")
    out = []
    for d in ds:
        if not isinstance(d, dict):
            raise Fail(1, "disposition 은 객체여야 합니다")
        disp = _s(d, "disposition", 30)
        if disp not in DISPOSITIONS:
            raise Fail(1, f"disposition 은 {'/'.join(DISPOSITIONS)} 중 하나입니다: {disp!r}")
        idx = d.get("finding_index")
        if not isinstance(idx, int) or idx < 0:
            raise Fail(1, "finding_index 는 0 이상의 정수입니다")
        out.append({"finding_index": idx, "disposition": disp,
                    "note": _s(d, "note", 500, required=(disp != "accepted"))})
    return {"dispositions": out}


def requester_env() -> dict:
    """The requesting session's cmux identity, recorded on the request event so the relay
    never depends on the hook process's environment."""
    return {"workspace": os.environ.get("CMUX_WORKSPACE_ID", ""),
            "surface": os.environ.get("CMUX_SURFACE_ID", ""),
            "cwd": os.getcwd()}


# ---------- target validation ----------

def read_registry_entry(path: str) -> dict:
    kv = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    kv[k] = v
    except OSError:
        return {}
    return kv


def pid_command(pid: str) -> str:
    if not pid or not pid.isdigit():
        return ""
    try:
        return subprocess.run(["ps", "-p", pid, "-o", "command="],
                              capture_output=True, text=True).stdout
    except OSError:
        return ""


def registry_find_surface(surface: str, workspace: str | None = None) -> tuple[str, dict]:
    """(workspace uuid, entry) for a registered agent on `surface`. Raises Fail(3)."""
    pat = (os.path.join(REGISTRY_ROOT, workspace, "*") if workspace
           else os.path.join(REGISTRY_ROOT, "*", "*"))
    for f in sorted(glob.glob(pat)):
        e = read_registry_entry(f)
        if e.get("surface", "").upper() == surface.upper():
            return os.path.basename(os.path.dirname(f)), e
    raise Fail(3, f"에이전트 레지스트리에 surface {surface} 가 없습니다 — 대상 세션이 "
                  "종료됐거나 에이전트가 아닙니다")


def native_grok_alive(workspace: str, surface: str) -> bool:
    """Use cmux's native Grok integration when no pj-launch.py registry entry exists."""
    tree = run_cmux(["cmux", "tree", "--workspace", workspace, "--id-format", "both"])
    surface_ref = None
    for line in tree.splitlines():
        m = SURFACE_LINE.search(line)
        if m and m.group(2).upper() == surface.upper():
            surface_ref = f"surface:{m.group(1)}"
            break
    if not surface_ref:
        return False
    top = run_cmux(["cmux", "top", "--workspace", workspace, "--processes", "--flat",
                    "--format", "tsv"], check=False)
    for line in top.splitlines():
        cols = line.split("\t")
        if (len(cols) >= 7 and cols[3] == "process" and cols[5] == surface_ref
                and "grok" in cols[6].lower()):
            return True
    return False


def workspace_for_native_surface(surface: str) -> str:
    """Resolve a Grok board's workspace when it has no pj-launch.py registry entry."""
    tree = run_cmux(["cmux", "tree", "--all", "--id-format", "both"])
    workspace = None
    for line in tree.splitlines():
        wm = re.search(r"\bworkspace\s+workspace:\d+\s+(%s)\b" % UUID_RE_S, line)
        if wm:
            workspace = wm.group(1).upper()
            continue
        sm = SURFACE_LINE.search(line)
        if sm and sm.group(2).upper() == surface.upper() and workspace:
            return workspace
    raise Fail(3, f"cmux 트리에서 surface {surface} 를 찾지 못했습니다")


def validate_target(workspace: str, surface: str, launcher: str | None = None) -> None:
    """The fast, non-polling check before typing into an existing surface (spec: registry +
    pid + one targeted tree lookup; unknowable counts as dead)."""
    if launcher and launcher_runtime(launcher) == "grok":
        if not native_grok_alive(workspace, surface):
            raise Fail(3, f"대상 surface {surface} 의 grok 프로세스가 살아있지 않습니다 "
                          "(셸만 남은 surface 에는 보내지 않습니다)")
        return
    try:
        _, entry = registry_find_surface(surface, workspace)
    except Fail:
        # A board registered from Grok predates role_attached and therefore has no launcher
        # field. cmux's native Grok process integration is the only safe fallback.
        if launcher is None and native_grok_alive(workspace, surface):
            return
        raise
    needle = entry.get("kind", "")
    if needle not in ("claude", "codex", "grok"):
        raise Fail(3, "Unknown agent runtime in surface registry")
    if launcher and launcher_runtime(launcher) != needle:
        raise Fail(3, "대상 surface의 runtime이 요청 프로필과 다릅니다")
    if needle not in pid_command(entry.get("pid", "")):
        raise Fail(3, f"대상 surface {surface} 의 {needle} 프로세스가 살아있지 않습니다 "
                      "(셸만 남은 surface 에는 보내지 않습니다)")
    tree = run_cmux(["cmux", "tree", "--workspace", workspace, "--id-format", "both"])
    if not TEST and surface.upper() not in tree.upper():
        raise Fail(3, f"surface {surface} 가 workspace {workspace} 에 없습니다")


def send_wakeup(workspace: str, surface: str, line: str) -> None:
    if "\n" in line:
        raise Fail(1, "wake-up 은 한 줄이어야 합니다")  # 방어: 다중행은 절대 send 금지
    run_cmux(["cmux", "send", "--workspace", workspace, "--surface", surface, "--", line])
    if not TEST:
        time.sleep(1)
    run_cmux(["cmux", "send", "--workspace", workspace, "--surface", surface, "--", r"\r"])


# ---------- role target resolution ----------

def latest_role_attached(project: str, slug: str, role: str) -> dict | None:
    for ev in reversed(read_stream(stream_path(project, slug, role))):
        if ev.get("type") == "role_attached":
            return ev.get("payload", {})
    return None


def reviewer_stream(project: str, slug: str, role: str) -> pathlib.Path:
    """Where THIS reviewer's attachments and findings live — its definition says which."""
    return stream_path(project, slug, reviewer_def(role)["stream"])


def latest_reusable_reviewer_attached(project: str, slug: str, role: str) -> dict | None:
    """This reviewer's session for this task, or None when it has none yet.

    Every review is reuse-based now, so `reviewer_mode` is no longer a policy anyone chooses.
    The recorded value stays and is still filtered on, because it is the only thing that
    distinguishes a reusable session from the isolated ones the removed hard mode created:
    those records are still in the streams, and picking one up as this task's reviewer would
    wake a session that was built to answer exactly one request and then be done.
    """
    for ev in reversed(read_stream(reviewer_stream(project, slug, role))):
        payload = ev.get("payload") or {}
        if ev.get("type") == "role_attached" and payload.get("reviewer_mode") == "reuse":
            return payload
    return None


def reviewer_attached_for_request(project: str, slug: str, role: str,
                                  request_event_id: str) -> dict | None:
    """Attachment created by one exact request, used to make relay retries session-idempotent."""
    for ev in reversed(read_stream(reviewer_stream(project, slug, role))):
        if ev.get("type") == "role_attached" and ev.get("reply_to") == request_event_id:
            return ev.get("payload") or {}
    return None


def latest_requester(project: str, slug: str, role: str) -> dict | None:
    """Pre-separation fallback: the newest event in the role's stream that recorded its
    requesting session (the old single task-lead session doubles as the worker)."""
    for ev in reversed(read_stream(stream_path(project, slug, role))):
        req = (ev.get("payload") or {}).get("requester") or {}
        if req.get("workspace") and req.get("surface"):
            return req
    return None


def task_topology(project: str, slug: str) -> str:
    """solo → planner and worker are one session; split → two sessions; unknown → no worker
    attachment (never opened through workspace.open, or a pre-separation task).
    workspace.open records the flag on both role_attached events; records that predate the
    flag are read positionally — both roles on one surface can only mean one session."""
    worker = latest_role_attached(project, slug, "worker")
    if not worker:
        return "unknown"
    if "solo" in worker:
        return "solo" if worker["solo"] else "split"
    planner = latest_role_attached(project, slug, "planner") or {}
    same = (planner.get("surface") and planner.get("surface") == worker.get("surface")
            and planner.get("workspace") == worker.get("workspace"))
    return "solo" if same else "split"


def task_is_solo(project: str, slug: str | None) -> bool:
    return bool(slug) and task_topology(project, slug) == "solo"


def task_runtime(project: str, slug: str | None) -> str:
    """The runtime this task was opened on — the LAUNCHER_TABLE column every later reviewer
    request for it resolves from.

    Recorded once by workspace.open — as given, never inferred from the launchers: a task opened
    without `--runtime` is a claude task, by decision. The board stream is project-wide, so the
    lookup is by THIS task's slug; the newest workspace_open in the file belongs to whichever
    task was opened last, and reading it here handed one task another task's runtime.
    """
    if not slug:
        return DEFAULT_RUNTIME
    for ev in reversed(read_stream(stream_path(project, slug, "board"))):
        if ev.get("type") == "workspace_open" and ev.get("slug") == slug:
            rt = (ev.get("payload") or {}).get("runtime")
            return rt if rt in RUNTIMES else DEFAULT_RUNTIME
    return DEFAULT_RUNTIME


def side_pane(project: str, slug: str) -> tuple[str, str] | None:
    """The plan viewer's (pane, surface): recorded on the planner's role_attached when a solo
    workspace boots the viewer at open, or on the action_completed of a markdown.open that split
    one off. Documents open there as tabs and a solo task's code reviewer boots below it. None
    when no viewer was ever recorded for the task."""
    for ev in reversed(read_stream(stream_path(project, slug, "planner"))):
        pl = ev.get("payload") or {}
        if (ev.get("type") in ("action_completed", "role_attached")
                and pl.get("side_pane") and pl.get("side_surface")):
            return pl["side_pane"], pl["side_surface"]
    return None


def surface_alive(workspace: str, surface: str) -> bool:
    """Is `surface` still in the workspace tree? Unknowable counts as gone, so callers reopen."""
    if TEST:
        return True
    try:
        _, surfaces = cmux_tree(workspace)
    except Fail:
        return False
    return any(s == surface.upper() for lst in surfaces.values() for s, _ in lst)


def resolve_role_target(project: str, slug: str, role: str) -> tuple[str, str, str | None]:
    if role == "board":
        info = pj_tasks_json("proj-get", "--project", project)
        surface = info.get("surface") if isinstance(info, dict) else None
        if not surface:
            raise Fail(3, f"{project} 의 보드 surface 가 등록돼 있지 않습니다 — "
                          f"/pj-board {project} 를 먼저 실행하세요")
        try:
            ws, _ = registry_find_surface(surface)
        except Fail:
            ws = workspace_for_native_surface(surface)
        return ws, surface, None
    att = latest_role_attached(project, slug, role)
    if att and att.get("workspace") and att.get("surface"):
        return att["workspace"], att["surface"], att.get("launcher")
    req = latest_requester(project, slug, role)
    if req:
        return req["workspace"], req["surface"], None
    raise Fail(3, f"{role} 세션이 기록돼 있지 않습니다 (role_attached 없음) — "
                  "세션이 열린 적 없거나 스트림이 비어 있습니다")


# ---------- actions ----------
# Each action: source role (whose stream holds the request), target role, local?, and how the
# relay delivers it. Agents never supply argv/surfaces/launchers beyond these allowlisted args.

def read_stdin_json() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError as e:
        raise Fail(1, f"--stdin JSON 파싱 실패: {e}")
    if not isinstance(data, dict):
        raise Fail(1, "--stdin 은 JSON 객체여야 합니다")
    return data


def next_round(project: str, slug: str) -> int:
    """The round a new review.group.open should take.

    An OPEN-BUT-UNUSED round is reused rather than superseded: opening a round and then
    opening again before requesting anything is a retry (a model re-running a command it
    wasn't sure landed), not a second review pass. Inflating the round there would make
    "round 3" mean one actual review, and re-review filtering reads these numbers.
    """
    events = read_stream(stream_path(project, slug, "worker"))
    rounds = [ev.get("round", 0) for ev in events if ev.get("type") == "review_group_opened"]
    if not rounds:
        return 1
    latest = max(rounds)
    used = any(ev.get("type") == "review_requested" and ev.get("round") == latest
               for ev in events)
    return latest + 1 if used else latest


def current_round(project: str, slug: str) -> int:
    """The round in flight — what a skip/disposition belongs to."""
    rounds = [ev.get("round", 0) for ev in read_stream(stream_path(project, slug, "worker"))
              if ev.get("type") == "review_group_opened"]
    return max(rounds) if rounds else 0


def op_request(a) -> int:
    action = a.action
    if action == "review.started":
        return op_review_started(a)
    if action not in ACTIONS:
        raise Fail(1, f"알 수 없는 액션: {action} (가능: {', '.join(sorted(ACTIONS))})")
    spec = ACTIONS[action]
    info = None
    project = a.project
    if spec["needs_slug"] or (a.slug and not project):
        info = task_info(a.slug)
        project = info["project"]
    if not project:
        raise Fail(1, f"{action} 는 --slug 또는 --project 가 필요합니다")
    stdin = read_stdin_json() if a.stdin else {}
    ev = spec["build"](a, info or {}, project, stdin)
    ev["payload"]["requester"] = requester_env()
    ev["digest"] = digest_of(ev)  # payload changed after make_event
    source = action_source_role(action, ev)
    path = stream_path(project, a.slug if source != "board" else None, source)
    append_event(path, ev)
    if action == "review.request":
        watcher("register", project, ev)
    print(f"event={ev['event_id']} stream={path.relative_to(TASKS_DIR)}")
    if spec["local"]:
        print(f"PJ_CMUX=ok action={action} event={ev['event_id']}")
        return 0
    if should_relay_inline():
        retry_cmd = (f"pj-cmux.py retry --request-id {ev['request_id']}"
                     + (f" --slug {a.slug}" if a.slug else ""))
        try:
            msg, result = relay_request_detail(ev["request_id"], a.slug)
        except Fail as e:
            log(str(e))
            print(f"PJ_CMUX=failed request={ev['request_id']} — {retry_cmd}")
            return e.code
        log(msg)
        print(f"PJ_CMUX={'starting' if result.get('awaiting_start') else 'delivered'} request={ev['request_id']}"
              + (" self=true" if result.get("self") else ""))
        return 0
    print(f"PJ_CMUX_REQUEST={ev['request_id']}")
    return 0


def build_workspace_open(a, info, project, stdin):
    # solo: planner and worker are one session on the planner's launcher, so there is no
    # worker launcher to pass — a differing --worker is a contradiction, not a preference.
    solo = bool(getattr(a, "solo", False))
    required = ("planner", "reviewer") if solo else ("planner", "worker", "reviewer")
    for role_flag in required:
        code = getattr(a, role_flag)
        if not code or not valid_launcher(code):
            raise Fail(1, f"--{role_flag} 에 유효한 런처가 필요합니다 (launcher parse 로 "
                          f"정규화하세요): {code!r}")
    if solo and a.worker and a.worker != a.planner:
        raise Fail(1, f"--solo 에서는 --worker 를 넘기지 마세요 ({a.worker!r}) — 워커 역할은 "
                      "planner 런처 세션이 겸합니다")
    worker = a.planner if solo else a.worker
    # Two input forms, one payload. `--repo-wt` names a repo per worktree, which a task
    # targeting several needs; the flat `--worktree`/`--branch` pair is the single-repo form
    # every existing caller uses, and it is folded into a one-element list rather than kept as
    # a second code path. Repo identity is unknown in the flat form and is not needed: the
    # registry resolves it from the worktree itself.
    repo_wt = parse_repo_wt(getattr(a, "repo_wt", None))
    if repo_wt and (a.worktree or a.branch):
        raise Fail(1, "--repo-wt 와 --worktree/--branch 는 같이 쓸 수 없습니다")
    if not repo_wt:
        if not a.worktree or not os.path.isabs(a.worktree):
            raise Fail(1, "--worktree 는 절대 경로여야 합니다 (또는 --repo-wt 를 쓰세요)")
        if not TEST and not os.path.isdir(a.worktree):
            raise Fail(1, f"워크트리가 없습니다: {a.worktree} — create-worktree.sh 가 먼저입니다")
    if not a.name:
        raise Fail(1, "--name (워크스페이스 이름) 이 필요합니다")
    prompt = a.planner_prompt or ""
    if a.planner_prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip() or len(prompt) > 4000:
        raise Fail(1, "--planner-prompt 가 비었거나 4000자를 넘습니다")
    switches = a.switch or []
    bad = [s for s in switches if s not in SWITCHES]
    if bad:
        raise Fail(1, f"알 수 없는 스위치: {bad} (가능: {', '.join(SWITCHES)})")
    # the task record has no branch yet (start is recorded AFTER the workspace opens), so
    # the caller passes the branch create-worktree.sh just cut — never fall back to the slug
    if not repo_wt:
        branch = a.branch or info.get("branch", "")
        if not branch or not BRANCH_RE.match(branch):
            raise Fail(1, "--branch (create-worktree 가 만든 브랜치) 가 필요합니다")
        repo_wt = [{"repo": "", "worktree": a.worktree, "branch": branch}]
    return make_event("workspace_open", "board", "board", a.slug, {
        "launchers": {"plan": a.planner, "work": worker, "review": a.reviewer},
        "runtime": valid_runtime(getattr(a, "runtime", None)),
        "solo": solo, "name": a.name, "description": a.description or "",
        "switches": switches, "planner_prompt": prompt,
        "repo_wt": repo_wt,
        # the flat fields stay the single-repo shape the rest of the module reads; with several
        # worktrees `worktree` is their shared parent and `branch` has no one value to hold.
        "worktree": workspace_cwd({"repo_wt": repo_wt}),
        "branch": repo_wt[0]["branch"] if len(repo_wt) == 1 else "",
        "base_branch": info.get("project_branch", ""),
    }, request_id=new_id("req"))


def parse_repo_wt(specs: list[str] | None) -> list[dict]:
    """`--repo-wt <repo>:<worktree>:<branch>` into records.

    The worktree is an absolute path and therefore contains `/` but never `:`, so a plain
    3-way split is unambiguous — and a wrong field count fails here rather than producing a
    worktree named after a branch.
    """
    out: list[dict] = []
    for spec in specs or []:
        parts = spec.split(":")
        if len(parts) != 3 or not all(parts):
            raise Fail(1, f"--repo-wt 는 repo:worktree:branch 형식입니다: {spec!r}")
        repo, worktree, branch = parts
        if not os.path.isabs(worktree):
            raise Fail(1, f"--repo-wt 의 워크트리는 절대 경로여야 합니다: {worktree!r}")
        if not TEST and not os.path.isdir(worktree):
            raise Fail(1, f"워크트리가 없습니다: {worktree} — create-worktree.sh 가 먼저입니다")
        if not BRANCH_RE.match(branch):
            raise Fail(1, f"--repo-wt 의 브랜치 형식이 잘못됐습니다: {branch!r}")
        if any(r["repo"] == repo for r in out):
            raise Fail(1, f"--repo-wt 에 같은 repo 가 두 번 나옵니다: {repo}")
        out.append({"repo": repo, "worktree": worktree, "branch": branch})
    return out


def build_workspace_board(a, info, project, stdin):
    """The board's own workspace, which is NOT a task workspace: no roles, no plan document,
    no handoff — one session standing on the project's branch(es).

    It deliberately does not register the project. A board is something the user registers by
    running pj-board in it, so this action only builds the place that can be registered; the
    boot prompt is what carries the invitation.
    """
    repo_wt = parse_repo_wt(getattr(a, "repo_wt", None))
    if not repo_wt:
        raise Fail(1, "--repo-wt 가 최소 하나 필요합니다 (repo:worktree:branch)")
    if not a.name:
        raise Fail(1, "--name (워크스페이스 이름) 이 필요합니다")
    if not a.board or not valid_launcher(a.board):
        raise Fail(1, f"--board 에 유효한 런처가 필요합니다: {a.board!r}")
    prompt = a.board_prompt or ""
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip() or len(prompt) > 4000:
        raise Fail(1, "--board-prompt 가 비었거나 4000자를 넘습니다")
    return make_event("workspace_board", "board", "board", None, {
        "launchers": {"board": a.board},
        "name": a.name, "description": a.description or "",
        "repo_wt": repo_wt,
        # `worktree`/`branch` keep the flat single-repo shape the rest of the module reads, so
        # a one-repo board takes exactly the path a task workspace does.
        "worktree": repo_wt[0]["worktree"],
        "branch": repo_wt[0]["branch"] if len(repo_wt) == 1 else "",
        "board_prompt": prompt,
    }, request_id=new_id("req"))


def deliver_workspace_board(project: str, slug: str | None, ev: dict) -> tuple[str, dict]:
    p = ev["payload"]
    pane = boot_command(project, None, "board", p["launchers"]["board"], p["board_prompt"])
    layout = json.dumps({"pane": {"surfaces": [{"type": "terminal", "command": pane}]}})
    cwd = workspace_cwd(p)
    ws_ref, ws_uuid, color = create_workspace(project, p, layout, cwd)
    return "board", {"workspace": ws_uuid, "workspace_ref": ws_ref, "cwd": cwd,
                     "color": color or "",
                     "repos": [r["repo"] for r in p["repo_wt"]]}


def build_markdown_open(a, info, project, stdin):
    plan_path = TASKS_DIR / project / f"{a.slug}.md"
    if not TEST and not plan_path.is_file():
        raise Fail(1, f"계획 문서가 없습니다: {plan_path}")
    return make_event("markdown_open", "planner", "planner", a.slug,
                      {"plan_path": str(plan_path)}, request_id=new_id("req"))


def build_color_sync(a, info, project, stdin):
    mode = a.mode or "reuse"
    if mode not in ("reuse", "new"):
        raise Fail(1, "--mode 는 reuse|new 입니다")
    return make_event("color_sync", "board", "board", a.slug,
                      {"project": project, "mode": mode}, request_id=new_id("req"))


def build_worker_handoff(a, info, project, stdin):
    payload = validate_handoff(stdin)
    payload["plan_path"] = str(TASKS_DIR / project / f"{a.slug}.md")
    return make_event("worker_handoff", "planner", "worker", a.slug, payload,
                      round_=0, request_id=new_id("req"))


def selected_reviewers(project: str, slug: str) -> list[str]:
    """Every reviewer this task runs: the defaults, plus the optional ones the planner picked.

    A default that the task's topology excludes (`plan` on a solo task) is left out here rather
    than opened and then skipped — pj-review records that skip itself, and listing it as
    expected would make the round look incomplete until it did.
    """
    picked: list[str] = []
    # the handoff is the PLANNER's event and lands in the planner stream — the same place the
    # decision was made, which is the point of recording it there
    for ev in reversed(read_stream(stream_path(project, slug, "planner"))):
        if ev.get("type") == "worker_handoff":
            picked = (ev.get("payload") or {}).get("reviewers") or []
            break
    solo = task_is_solo(project, slug)
    return [r for r in reviewer_roles()
            if (reviewer_def(r)["default"] or r in picked)
            and (reviewer_def(r)["solo"] or not solo)]


DIFF_REF_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*:)?[A-Za-z0-9][A-Za-z0-9._/-]*"
                         r"(?:,(?:[A-Za-z0-9][A-Za-z0-9._-]*:)?[A-Za-z0-9][A-Za-z0-9._/-]*)*$")


def diff_ref_lines(base: str, head: str) -> str:
    """Review current task worktrees; only an unqualified ref applies to every repo."""
    def parse(v: str) -> list[tuple[str, str]]:
        refs = [tuple(p.split(":", 1)) if ":" in p else ("", p) for p in v.split(",")]
        repos = [repo for repo, _ in refs]
        if len(set(repos)) != len(repos) or (len(refs) > 1 and "" in repos):
            raise Fail(1, "리뷰 ref 는 단일 공통 브랜치 또는 중복 없는 repo:branch 목록이어야 합니다")
        return refs

    def command(b: str, h: str) -> str:
        return shlex.join(["python3", str(SCRIPT_DIR / "pj-review-diff.py"),
                           "--base", b, "--head", h])

    bases, heads = parse(base), parse(head)
    if not bases[0][0] and not heads[0][0]:
        return f"- Working-tree diff: `{command(bases[0][1], heads[0][1])}` in the task worktree."
    if not bases[0][0]:
        bases = [(repo, bases[0][1]) for repo, _ in heads]
    if not heads[0][0]:
        heads = [(repo, heads[0][1]) for repo, _ in bases]
    hmap = {r: b for r, b in heads}
    bmap = dict(bases)
    for repo in bmap.keys() | hmap.keys():
        if repo not in hmap:
            raise Fail(1, f"리뷰 대상 {repo} 의 diff_head 가 없습니다")
        if repo not in bmap:
            raise Fail(1, f"리뷰 대상 {repo} 의 diff_base 가 없습니다")
    lines = ["- Working-tree diff, one per repo — run each INSIDE that repo's task worktree folder:"]
    for r, b in bases:
        lines.append(f"  - `{r}`: `{command(b, hmap[r])}`")
    return "\n".join(lines)


def build_review_group_open(a, info, project, stdin):
    rnd = a.round or next_round(project, a.slug)
    payload = {"summary": _s(stdin, "summary", 1000),
               # a bare branch, or `repo:branch,repo:branch` for a task spanning repos whose
               # project branches differ — see diff_ref_lines
               "diff_base": _s(stdin, "diff_base", 400, pattern=DIFF_REF_RE),
               "diff_head": _s(stdin, "diff_head", 400, pattern=DIFF_REF_RE),
               "roles": selected_reviewers(project, a.slug)}
    state = night_state(project, a.slug)
    if state and state.get("state") == "running" and a.slug in state.get("inflight", {}):
        payload["night_run_id"] = state.get("run_id")
    return make_event("review_group_opened", "worker", "worker", a.slug, payload, round_=rnd)


def _find_group(project, slug, rnd):
    for ev in reversed(read_stream(stream_path(project, slug, "worker"))):
        if ev.get("type") == "review_group_opened" and ev.get("round") == rnd:
            return ev
    raise Fail(1, f"round {rnd} 의 review_group_opened 가 없습니다 — review.group.open 이 "
                  "먼저입니다")


def recorded_workspace_launcher(project: str, slug: str, role: str) -> str | None:
    """Launcher selected when this task's workspace successfully opened.

    workspace.open is project-board traffic, so planner/worker role attachments cannot carry
    the deferred reviewer choice. Only use completed opens: a request whose workspace creation
    failed must not override the alias/default fallback for a pre-separation task.
    """
    events = read_stream(stream_path(project, None, "board"))
    for ev in reversed(events):
        if ev.get("type") != "workspace_open" or ev.get("slug") != slug:
            continue
        if not find_terminal(events, ev):
            continue
        launcher = ((ev.get("payload") or {}).get("launchers") or {}).get(role)
        if launcher:
            if not valid_launcher(launcher):
                raise Fail(1, f"workspace.open 에 기록된 {role} 런처가 유효하지 않습니다: "
                              f"{launcher!r}")
            return launcher
    return None


def build_review_request(a, info, project, stdin):
    spec = reviewer_def(a.role)
    if not spec["solo"] and task_is_solo(project, a.slug):
        raise Fail(1, f"solo 태스크에서는 {a.role} 리뷰어를 쓰지 않습니다 — "
                      f"review.skip --role {a.role} 으로 생략을 기록하세요")
    chosen = selected_reviewers(project, a.slug)
    if a.role not in chosen:
        raise Fail(1, f"{a.role} 는 이 태스크의 리뷰어가 아닙니다 (선택된 리뷰어: "
                      f"{', '.join(chosen)}) — 리뷰어는 계획 종료 시 플래너가 고릅니다")
    rnd = a.round or current_round(project, a.slug)
    group = _find_group(project, a.slug, rnd)
    launcher = None
    if spec["spawn"]:
        # One task-scoped reviewer, always. The first request creates it and every later one
        # wakes that same session, so its launcher is fixed once and a request that asks for a
        # different one is a contradiction rather than a preference.
        reusable = latest_reusable_reviewer_attached(project, a.slug, a.role)
        if reusable:
            existing_launcher = reusable.get("launcher")
            if not valid_launcher(existing_launcher):
                raise Fail(1, "기존 리뷰어의 런처 기록이 유효하지 않습니다: "
                              f"{existing_launcher!r}")
            if a.launcher and a.launcher != existing_launcher:
                raise Fail(1, f"이 태스크의 {a.role} 리뷰어는 {existing_launcher} 입니다 — "
                              "모델을 바꾸려면 그 리뷰어 세션을 닫고 다시 요청하세요")
            launcher = existing_launcher
        else:
            # the code reviewer runs the recorded launcher itself; every other reviewer runs
            # its own default for the same review option
            recorded = recorded_workspace_launcher(project, a.slug, "review")
            option = (review_option(recorded) if recorded
                      else RUNTIME_REVIEW_OPTION[task_runtime(project, a.slug)])
            launcher = (a.launcher
                        or (recorded if a.role == DEFAULT_REVIEWER else None)
                        or reviewer_launcher(a.role, option))
        if not valid_launcher(launcher):
            raise Fail(1, f"유효한 리뷰어 런처가 아닙니다: {launcher!r}")
    findings_filter = []
    if a.findings:
        findings_filter = a.findings.split(",")
        for f in findings_filter:
            if not re.match(r"^evt_[0-9a-f]{16}$", f):
                raise Fail(1, f"--findings 는 evt_ id 목록입니다: {f!r}")
    payload = {"role": a.role, "launcher": launcher, "group_event": group["event_id"],
               "summary": group["payload"].get("summary", ""),
               "diff_base": group["payload"].get("diff_base", ""),
               "diff_head": group["payload"].get("diff_head", ""),
               "plan_path": str(TASKS_DIR / project / f"{a.slug}.md"),
               "recheck": findings_filter}
    return make_event("review_requested", "worker", spec["to"], a.slug, payload,
                      round_=rnd, request_id=new_id("req"))


def build_review_reply(a, info, project, stdin):
    spec = reviewer_def(a.role)
    rnd = a.round or current_round(project, a.slug)
    _find_group(project, a.slug, rnd)
    payload = {**validate_findings(stdin, a.role), "role": a.role}
    requests = [ev for ev in read_stream(stream_path(project, a.slug, "worker"))
                if ev.get("type") == "review_requested" and ev.get("round") == rnd
                and (ev.get("payload") or {}).get("role") == a.role]
    # `from` is the reviewer's STREAM, which is what action_source_role appends by. Using the
    # delivery target ("reviewer") instead put every code-side reviewer's findings in one file,
    # and then nothing distinguished them: `code_review` + round matched any of them, so one
    # reviewer replying made the round look terminal for all of them.
    return make_event(spec["reply_type"], spec["stream"], "worker", a.slug, payload, round_=rnd,
                      reply_to=requests[-1]["event_id"] if requests else None,
                      request_id=new_id("req"))


def op_review_started(a) -> int:
    """Claim exactly one request before reviewing; duplicate wake-ups do no new work."""
    project = task_info(a.slug)["project"]
    spec = reviewer_def(a.role)
    rows = read_stream(stream_path(project, a.slug, "worker"))
    requests = [e for e in rows if e["type"] == "review_requested"
                and e["payload"].get("role") == a.role]
    request = next((e for e in requests if e["event_id"] == a.reply_to), None)
    if not request or requests[-1]["event_id"] != request["event_id"]:
        raise Fail(1, "review.started 는 해당 리뷰어의 최신 요청 --reply-to evt_… 가 필요합니다")
    if a.round is not None and a.round != request["round"]:
        raise Fail(1, "리뷰 요청과 round 가 다릅니다")
    with request_lock(request["request_id"]):
        if watcher("started", project, request):
            print("PJ_REVIEW_STARTED=duplicate — 이미 시작한 요청입니다. 리뷰를 다시 실행하지 마세요.")
            return 0
        job = next((j for j in watcher("jobs", project)
                    if j["request"]["event_id"] == request["event_id"]), {})
        if not TEST and (not job.get("surface") or
                os.environ.get("CMUX_SURFACE_ID", "").upper() != job["surface"].upper() or
                os.environ.get("CMUX_WORKSPACE_ID", "").upper() != job["workspace"].upper()):
            raise Fail(3, "등록된 리뷰어 탭에서만 시작을 확인할 수 있습니다")
        event = make_event("review_started", spec["stream"], "worker", a.slug,
                           {"role": a.role}, round_=request["round"], reply_to=request["event_id"])
        append_event(reviewer_stream(project, a.slug, a.role), event)
        watcher("record", project, request, "completed", reason="review started")
    print(f"PJ_REVIEW_STARTED=started event={event['event_id']}")
    return 0


def build_watcher_start(a, info, project, stdin):
    if not SLUG_RE.fullmatch(project or ""):
        raise Fail(1, "--project 가 필요합니다")
    board_launcher = getattr(a, "board", None)
    if board_launcher and not valid_launcher(board_launcher):
        raise Fail(1, "--board 에 유효한 보드 런처가 필요합니다")
    return make_event("watcher_start", "board", "board", None,
                      {"board_launcher": board_launcher,
                       "board_context": {"codex_home": str(pathlib.Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser())}},
                      request_id=new_id("req"))


def deliver_watcher_start(project, slug, ev):
    watcher("ensure", project)
    tab = watcher("ensure_tab", project, ev)
    return "board", {"watcher": "running", **tab}


def build_review_disposition(a, info, project, stdin):
    if not a.reply_to or not re.match(r"^evt_[0-9a-f]{16}$", a.reply_to):
        raise Fail(1, "--reply-to <처분 대상 findings 이벤트 id> 가 필요합니다")
    payload = validate_dispositions(stdin)
    return make_event("review_disposition", "worker", "worker", a.slug, payload,
                      round_=a.round or current_round(project, a.slug),
                      reply_to=a.reply_to)


def build_review_skip(a, info, project, stdin):
    reviewer_def(a.role)          # an unknown reviewer cannot be skipped
    return make_event("review_skipped", "worker", "worker", a.slug,
                      {"role": a.role, "reason": _s(stdin, "reason", 300)},
                      round_=a.round or current_round(project, a.slug))


def review_round_terminal(project: str, slug: str, rnd: int) -> tuple[bool, str]:
    """Is every reviewer the round EXPECTS terminal — replied and acked processed, skipped on
    record, or visibly failed? The pill must never say 완료 ahead of the streams.

    ROUND 1 is held to the round's expected set, not to whatever happens to have been requested.
    Those differ in exactly the case that matters: the planner picks a reviewer and the worker
    forgets to dispatch it. Walking the requests cannot see a reviewer that has none, so the
    round would read as clean with a lane nobody reviewed — the one thing this gate exists to
    stop. The expectation is what the group event recorded when the round opened, not a fresh
    `selected_reviewers()`: a later handoff must not lower the bar on a round in flight.

    LATER ROUNDS are held to what was actually requested in them. A re-review is targeted by
    design — the reviewer whose finding the fix materially changed looks again, and nobody
    else — and which reviewers those are is something only the worker, who made the fix, can
    say. Holding round 2 to the full round-1 set would make every fix re-run every reviewer
    (seen live: a planner woken for a second plan review it had no reason to give). The floor
    is one request: a round with none is not a round.

    A group from before `roles` existed has none, and for it the requested set is the only
    expectation there ever was.
    """
    events = load_events(project, slug)
    evs = [ev for _, ev in events]
    requested = [ev for ev in evs if ev["type"] == "review_requested" and ev.get("round") == rnd]
    skipped = {(ev.get("payload") or {}).get("role") for ev in evs
               if ev["type"] == "review_skipped" and ev.get("round") == rnd}
    if not requested and not skipped:
        return False, f"라운드 {rnd} 에 리뷰 요청·생략 기록이 없습니다"

    groups = [ev for ev in evs
              if ev["type"] == "review_group_opened" and ev.get("round") == rnd]
    by_role = {(ev.get("payload") or {}).get("role"): ev for ev in requested}
    expected = (groups[-1].get("payload") or {}).get("roles") if groups else None
    if rnd > 1 or not expected:
        expected = list(by_role)
        if not expected:
            return False, f"라운드 {rnd} 에 리뷰 요청이 하나도 없습니다"

    for role in expected:
        if role in skipped:
            continue
        req = by_role.get(role)
        if req is None:
            return False, (f"{role} 리뷰를 아직 요청하지 않았습니다 — 이 라운드가 기대하는 "
                           f"리뷰어입니다 (review.request --role {role}, 또는 review.skip "
                           f"--role {role} 으로 생략을 기록)")
        state = fold_status(events, req)["state"]
        if state == "failed":
            continue
        reply_type = reviewer_def(role)["reply_type"]
        # Several reviewers share a reply TYPE (every code-side lens replies as `code_review`),
        # so the type alone does not say who replied. The payload's role does. Events written
        # before reviewers were plural carry no role and can only have been the code reviewer.
        replies = [ev for ev in evs
                   if ev["type"] == reply_type and ev.get("round") == rnd
                   and (ev.get("payload") or {}).get("role", DEFAULT_REVIEWER) == role]
        if not any(fold_status(events, r)["state"] == "processed" for r in replies):
            return False, (f"{role} 리뷰가 아직 terminal 이 아닙니다 (요청 {state}, "
                           "processed 로 ack 된 회신 없음)")
    return True, ""


def night_state(project: str, slug: str) -> dict | None:
    """Read the queue owner's atomic snapshot; this transport never mutates it."""
    path = TASKS_DIR / project / "night.json"
    assert_no_symlinks(path)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Fail(4, f"night 상태를 읽을 수 없습니다: {e}")
    if not isinstance(state, dict):
        raise Fail(4, "night 상태는 객체여야 합니다")
    return state if slug in state.get("queue", []) else None


def check_night_review(project: str, slug: str) -> dict:
    """Night's merge gate: terminal failures/skips are not completed reviews.

    Preserve successful unchanged reviewers across targeted rechecks, but require a processed
    reply to each role's latest request in THIS run. A same-round older reply cannot satisfy
    a newer request. The board calls this again immediately before its automatic merge.
    """
    state = night_state(project, slug)
    if not state or state.get("state") != "running" or slug not in state.get("inflight", {}):
        raise Fail(3, "night 검사 거부: 현재 실행 중인 작업이 아닙니다")
    try:
        since = datetime.datetime.fromisoformat(state["inflight"][slug].replace("Z", "+00:00"))
        if since.tzinfo is None:
            raise ValueError("착수 시각에 timezone 없음")
    except (KeyError, TypeError, ValueError) as e:
        raise Fail(4, f"night 착수 시각이 잘못됐습니다: {e}")
    events = load_events(project, slug)

    def current(ev):
        try:
            stamp = datetime.datetime.fromisoformat(ev.get("created_at", "").replace("Z", "+00:00"))
            return stamp >= since
        except (TypeError, ValueError):
            return False

    groups = [ev for _, ev in events if ev.get("type") == "review_group_opened" and current(ev)
              and (not state.get("run_id")
                   or ev.get("payload", {}).get("night_run_id") == state["run_id"])]
    if not groups or not groups[0].get("payload", {}).get("roles"):
        raise Fail(3, "night 검사 거부: 이번 실행의 리뷰어 목록이 없습니다")
    group_ids = {ev["event_id"] for ev in groups}
    required = {role for ev in groups for role in ev.get("payload", {}).get("roles", [])}
    requests = [ev for _, ev in events if ev.get("type") == "review_requested"
                and ev.get("payload", {}).get("group_event") in group_ids]
    if not any(ev.get("payload", {}).get("group_event") == groups[-1]["event_id"] for ev in requests):
        raise Fail(3, "night 검사 거부: 마지막 리뷰 라운드에 요청이 없습니다")
    latest = {ev["payload"]["role"]: ev for ev in requests}
    for role in sorted(required | set(latest)):
        req = latest.get(role)
        if req is None:
            raise Fail(3, f"night 검사 거부: {role} 리뷰 요청이 없습니다 (생략은 통과가 아닙니다)")
        if fold_status(events, req)["state"] == "failed":
            raise Fail(3, f"night 검사 거부: {role} 리뷰 전달 실패")
        replies = [ev for _, ev in events
                   if ev.get("type") == reviewer_def(role)["reply_type"]
                   and ev.get("payload", {}).get("role", DEFAULT_REVIEWER) == role
                   and ev.get("reply_to") == req["event_id"]]
        if not replies or fold_status(events, replies[-1])["state"] not in ("processed", "already-processed"):
            raise Fail(3, f"night 검사 거부: {role} 최신 리뷰 회신·처리 확인이 없습니다")
        reply = replies[-1]
        dispositions = {d["finding_index"]: d for _, ev in events
                        if ev.get("type") == "review_disposition" and ev.get("reply_to") == reply["event_id"]
                        for d in ev.get("payload", {}).get("dispositions", [])}
        for index, finding in enumerate(reply.get("payload", {}).get("findings", [])):
            disposition = dispositions.get(index, {})
            if finding.get("severity") == "blocking" and not (
                    disposition.get("disposition") == "rejected" and disposition.get("note", "").strip()):
                raise Fail(3, f"night 검사 거부: {role} blocking finding 은 수정 후 재리뷰가 필요합니다")
    return {"project": project, "slug": slug, "roles": sorted(required | set(latest)),
            "run_id": state.get("run_id"), "since": state["inflight"][slug]}


def check_active_night_review(project: str, slug: str) -> None:
    state = night_state(project, slug)
    if state and state.get("state") == "running":
        check_night_review(project, slug)


def op_night_check(a) -> int:
    info = task_info(a.slug)
    print(json.dumps(check_night_review(info["project"], a.slug), ensure_ascii=False))
    return 0


def build_review_complete(a, info, project, stdin):
    rnd = a.round or current_round(project, a.slug)
    ok, why = review_round_terminal(project, a.slug, rnd)
    if not ok:
        raise Fail(1, f"review.complete 거부 — {why}. 라운드가 기대하는 모든 리뷰어가 terminal 이 된 뒤에 호출하세요")
    check_active_night_review(project, a.slug)
    return make_event("review_complete", "worker", "worker", a.slug,
                      {"round": rnd, "pill": dict(REVIEW_DONE_PILL)},
                      round_=rnd, request_id=new_id("req"))


def build_decision_request(a, info, project, stdin):
    if task_is_solo(project, a.slug):
        raise Fail(1, "solo 태스크: 플래너가 곧 이 세션입니다 — 계획 의도 안에서 직접 결정해 "
                      "계획 문서에 기록하고, 최초 요청을 바꾸는 결정만 사용자에게 물으세요")
    options = stdin.get("options", [])
    if not isinstance(options, list) or len(options) > 6 or \
            any(not isinstance(o, str) or len(o) > 300 for o in options):
        raise Fail(1, "options 는 최대 6개의 문자열 리스트입니다")
    payload = {"plan_reference": _s(stdin, "plan_reference", 300),
               "question": _s(stdin, "question", 1000),
               "evidence": _s(stdin, "evidence", 1000, required=False),
               "options": options}
    return make_event("decision_request", "worker", "planner", a.slug, payload,
                      round_=a.round or 0, request_id=new_id("req"))


def build_decision_reply(a, info, project, stdin):
    if not a.reply_to or not re.match(r"^evt_[0-9a-f]{16}$", a.reply_to):
        raise Fail(1, "--reply-to <decision_request 이벤트 id> 가 필요합니다")
    payload = {"decision": _s(stdin, "decision", 1000),
               "reason": _s(stdin, "reason", 1000, required=False)}
    return make_event("decision_reply", "planner", "worker", a.slug, payload,
                      round_=a.round or 0, reply_to=a.reply_to, request_id=new_id("req"))


COMMIT_VERDICTS = ("ok", "fail")
TYPECHECK_VERDICTS = ("ok", "fail", "none", "unparsed")


def check_verdicts(commit: str, typecheck: str, where: str) -> None:
    if commit not in COMMIT_VERDICTS:
        raise Fail(1, f"{where} commit 은 ok|fail 입니다: {commit!r}")
    if typecheck not in TYPECHECK_VERDICTS:
        raise Fail(1, f"{where} typecheck 은 ok|fail|none|unparsed 입니다: {typecheck!r}")


def parse_results(specs: list[str] | None) -> list[dict]:
    """`--result <repo>:<commit>[:<typecheck>]`; omitted optional checks were not run."""
    out: list[dict] = []
    for spec in specs or []:
        parts = spec.split(":")
        if len(parts) not in (2, 3) or not all(parts):
            raise Fail(1, f"--result 는 repo:commit[:typecheck] 형식입니다: {spec!r}")
        if len(parts) == 2:
            parts.append("none")
        repo, commit, typecheck = parts
        check_verdicts(commit, typecheck, f"--result {repo} 의")
        if any(r["repo"] == repo for r in out):
            raise Fail(1, f"--result 에 같은 repo 가 두 번 나옵니다: {repo}")
        out.append({"repo": repo, "commit": commit, "typecheck": typecheck})
    return out


def build_done_report(a, info, project, stdin):
    """One report per task, whatever number of repos it touched.

    Commit state is required per repo. Typecheck metadata is optional and does not cause
    the transport to run a checker or require one. `--result` carries the per-repo records;
    `--commit` with an optional `--typecheck` becomes a one-element list.

    A report is not a verdict on the whole task: `results` holds exactly what each check said,
    and a failing repo is reported as failing rather than suppressing the report — the board
    decides what to do with it.
    """
    check_active_night_review(project, a.slug)
    results = parse_results(getattr(a, "result", None))
    if results and (a.commit or a.typecheck):
        raise Fail(1, "--result 와 --commit/--typecheck 는 같이 쓸 수 없습니다")
    if not results:
        typecheck = a.typecheck if a.typecheck is not None else "none"
        check_verdicts(a.commit or "", typecheck, "--commit/--typecheck 의")
        results = [{"repo": "", "commit": a.commit, "typecheck": typecheck}]
    # The flat fields stay the single-repo shape a board built before this existed. With several
    # repos they carry the WORST verdict, so a reader that only knows the old shape cannot
    # mistake a partial failure for a clean report.
    commit = "fail" if any(r["commit"] == "fail" for r in results) else "ok"
    typechecks = {r["typecheck"] for r in results}
    typecheck = ("fail" if "fail" in typechecks
                 else "unparsed" if "unparsed" in typechecks
                 else "none" if typechecks == {"none"} else "ok")
    return make_event("done_report", "worker", "board", a.slug,
                      {"commit": commit, "typecheck": typecheck,
                       "results": results,
                       "branch": info.get("branch", "")},
                      request_id=new_id("req"))


ACTIONS: dict[str, dict] = {
    "watcher.start":     {"source": "board", "local": False, "needs_slug": False,
                          "build": build_watcher_start},
    "workspace.board":   {"source": "board", "local": False, "needs_slug": False,
                          "build": build_workspace_board},
    "workspace.open":    {"source": "board", "local": False, "needs_slug": True,
                          "build": build_workspace_open},
    "markdown.open":     {"source": "planner", "local": False, "needs_slug": True,
                          "build": build_markdown_open},
    "color.sync":        {"source": "board", "local": False, "needs_slug": False,
                          "build": build_color_sync},
    "worker.handoff":    {"source": "planner", "local": False, "needs_slug": True,
                          "build": build_worker_handoff},
    "review.group.open": {"source": "worker", "local": True, "needs_slug": True,
                          "build": build_review_group_open},
    "review.request":    {"source": "worker", "local": False, "needs_slug": True,
                          "build": build_review_request},
    # op_request handles this local claim atomically under the original request lock.
    "review.started":    {"source": None, "local": True, "needs_slug": True, "build": None},
    "review.reply":      {"source": None, "local": False, "needs_slug": True,
                          "build": build_review_reply},   # source depends on --role
    "review.disposition": {"source": "worker", "local": True, "needs_slug": True,
                           "build": build_review_disposition},
    "review.skip":       {"source": "worker", "local": True, "needs_slug": True,
                          "build": build_review_skip},
    "review.complete":   {"source": "worker", "local": False, "needs_slug": True,
                          "build": build_review_complete},
    "decision.request":  {"source": "worker", "local": False, "needs_slug": True,
                          "build": build_decision_request},
    "decision.reply":    {"source": "planner", "local": False, "needs_slug": True,
                          "build": build_decision_reply},
    "done.report":       {"source": "worker", "local": False, "needs_slug": True,
                          "build": build_done_report},
}


def action_source_role(action: str, ev: dict) -> str:
    """review.reply's source stream depends on which reviewer replied — its definition's
    `stream`, carried on the event's `from`. Every other action's source is fixed in ACTIONS."""
    spec = ACTIONS[action]
    if spec["source"]:
        return spec["source"]
    return ev["from"]


# ---------- relay ----------

def find_terminal(events: list[dict], request_event: dict,
                  target_role: str | None = None) -> dict | None:
    for ev in reversed(events):
        if ev.get("reply_to") != request_event["event_id"]:
            continue
        if ev.get("type") in ("delivered", "action_completed", "review_launch_queued"):
            if target_role and (ev.get("payload") or {}).get("target_role") != target_role:
                continue
            return ev
    return None


def cmux_tree(workspace: str) -> tuple[list[str], dict[str, list[tuple[str, str]]]]:
    out = run_cmux(["cmux", "tree", "--workspace", workspace, "--id-format", "both"])
    panes: list[str] = []
    surfaces: dict[str, list[tuple[str, str]]] = {}
    current = None
    for line in out.splitlines():
        m = PANE_LINE.search(line)
        if m:
            current = m.group(2).upper()
            if current not in surfaces:
                panes.append(current)
                surfaces[current] = []
            continue
        m = SURFACE_LINE.search(line)
        if m and current:
            surfaces[current].append((m.group(2).upper(), m.group(4)))
    return panes, surfaces


@contextlib.contextmanager
def surface_create_lock(workspace: str):
    """Serialize snapshot → new-surface → diff within one workspace.

    The new tab's UUID is found by set difference, so two creations overlapping in the same
    workspace would each see BOTH new tabs and pick the same one — two reviewers booted into one
    tab, one left an empty shell. reviewer_session_lock is per reviewer and does not cover this.
    Held only until the UUID is known; booting stays concurrent.
    """
    os.makedirs(LOCK_ROOT, exist_ok=True)
    key = hashlib.sha256(workspace.encode()).hexdigest()[:24]
    with open(os.path.join(LOCK_ROOT, "surface-" + key + ".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def new_surface_in_pane(workspace: str, pane: str, cwd: str, command: str | None = None) -> str:
    argv = ["cmux", "new-surface", "--type", "terminal", "--pane", pane,
            "--working-directory", cwd, "--workspace", workspace, "--focus", "false"]
    if command:
        argv += ["--command", command]
    if TEST:
        run_cmux(argv)
        return "TEST-NEW-SURFACE"
    with surface_create_lock(workspace):
        _, before = cmux_tree(workspace)
        known = {s for lst in before.values() for s, _ in lst}
        run_cmux(argv)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            _, after = cmux_tree(workspace)
            fresh = [s for s, _ in after.get(pane, []) if s not in known]
            if fresh:
                return fresh[-1]
            time.sleep(1)
    raise Fail(6, "새 리뷰어 탭의 UUID 를 찾지 못했습니다")


def boot_agent(workspace: str, surface: str, launcher: str, line: str, nonce: str) -> bool:
    """Codex/Grok take the boot line as an initial argument; Claude boots, then gets it sent.

    Only ONE line ever goes in (reviewer-boot.md), never the rendered prompt: a multi-KB paste
    lost whole sections on the way into the TUI — the reply command among them — and a Claude
    TUI not yet in paste mode submitted the first line on its own."""
    if "\n" in line:
        raise Fail(1, "부팅 지시는 한 줄이어야 합니다")
    command = launcher_shell_command(launcher)
    if launcher_runtime(launcher) in ("codex", "grok"):
        cmdline = f"{command} {shlex.quote(line)}"
        name = f"pj-boot-{nonce}"
        run_cmux(["cmux", "set-buffer", "--name", name, "--", cmdline])
        run_cmux(["cmux", "paste-buffer", "--name", name, "--workspace", workspace,
                  "--surface", surface])
        if not TEST:
            time.sleep(1)
        run_cmux(["cmux", "send", "--surface", surface, "--workspace", workspace, "--", r"\r"])
        return True
    run_cmux(["cmux", "send", "--surface", surface, "--workspace", workspace, "--", command])
    if not TEST:
        time.sleep(1)
    run_cmux(["cmux", "send", "--surface", surface, "--workspace", workspace, "--", r"\r"])
    # One nonblocking readiness probe; startup dialogs/waiting remain with the watcher.
    screen = run_cmux(["cmux", "read-screen", "--workspace", workspace,
                       "--surface", surface, "--lines", "35"], timeout=3)
    if watcher("screen_kind", screen) == "ready":
        send_wakeup(workspace, surface, line)
        return True
    return False


def sigil_for(launcher: str) -> str:
    return "$" if launcher_runtime(launcher) == "codex" else "/"


def deliver_send(project: str, slug: str, ev: dict) -> tuple[str, dict]:
    """Generic fixed-wake-up delivery to an existing verified agent surface."""
    target_role = ev["to"]
    review_role = (ev.get("payload") or {}).get("role")
    if (target_role in ("planner", "worker") and ev["from"] in ("planner", "worker")
            and task_is_solo(project, slug)):
        # solo: the target is the requesting session itself. The event stays the durable
        # record; typing a wake-up into a pane that is mid-turn is exactly what must never
        # happen, so nothing is sent and the session continues in-process.
        att = latest_role_attached(project, slug, target_role) or {}
        return target_role, {"workspace": att.get("workspace", ""),
                             "surface": att.get("surface", ""), "self": True,
                             **({"role": review_role} if review_role else {})}
    ws, surf, launcher = resolve_role_target(project, slug, target_role)
    validate_target(ws, surf, launcher)
    line = wakeup_line(project, slug, ev["from"], ev["event_id"])
    send_wakeup(ws, surf, line)
    return target_role, {"workspace": ws, "surface": surf,
                         **({"role": review_role} if review_role else {})}


def deliver_review_request(project: str, slug: str, ev: dict) -> tuple[str, dict]:
    payload = ev["payload"]
    if not reviewer_def(payload["role"])["spawn"]:
        ws, surf, launcher = resolve_role_target(project, slug, "planner")
        watcher("bind", project, ev, ws, surf, launcher)
        watcher("record", project, ev, "launched", sent_at=time.time())
        sent = send_review_when_ready(project, slug, ev, ws, surf, launcher)
        return "planner", {"workspace": ws, "surface": surf, "awaiting_start": not sent}
    # Every code review reuses the task's one reviewer, so discovery and creation are always
    # serialized: two requests racing here would otherwise each see "no reviewer yet".
    with reviewer_session_lock(project, slug, payload["role"]):
        return deliver_code_review(project, slug, ev)


def send_review_when_ready(project, slug, ev, workspace, surface, launcher) -> bool:
    if not watcher("alive", {"workspace": workspace, "surface": surface, "launcher": launcher}):
        raise Fail(3, "리뷰 대상 에이전트가 살아있지 않습니다 — watcher 상태를 확인하세요")
    screen = run_cmux(["cmux", "read-screen", "--workspace", workspace,
                       "--surface", surface, "--lines", "35"], timeout=3)
    if watcher("screen_kind", screen) != "ready":
        return False
    send_wakeup(workspace, surface, review_start_line(project, slug, ev))
    return True


def reviewer_prompt_for(project: str, slug: str, ev: dict) -> str:
    """A spawned reviewer's full instructions, rendered from its review_requested event."""
    payload = ev["payload"]
    if payload["role"] == "plan":
        return render_template("planner-review-start.md", {
            "PROJECT": project, "SLUG": slug, "ROUND": str(ev["round"]),
            "EVENT_ID": ev["event_id"], "PJ_CMUX": shlex.quote(str(SCRIPT_DIR / "pj-cmux.py")),
            "PJ_PLAN": shlex.quote(str(SCRIPT_DIR.parent.parent / "pj-plan" / "SKILL.md"))})
    recheck = payload.get("recheck") or []
    return render_reviewer_prompt(payload["role"], {
        "PROJECT": project, "SLUG": slug, "ROUND": str(ev["round"]),
        "EVENT_ID": ev["event_id"],
        "CONTEXT_LINE": project_context_line(project),
        "PLAN_PATH": payload["plan_path"],
        "DIFF_LINES": ("- Worker task location: "
                       + str((payload.get("requester") or {}).get("cwd")
                             or "resolve the task worktree before reviewing") + "\n"
                       + diff_ref_lines(payload["diff_base"], payload["diff_head"])),
        "SUMMARY": payload["summary"],
        "PJ_CMUX": shlex.quote(str(SCRIPT_DIR / "pj-cmux.py")),
        "RECHECK_LINE": (render_fragment("recheck-line", {"FINDINGS": ", ".join(recheck)})
                         if recheck else ""),
    })


def deliver_code_review(project: str, slug: str, ev: dict) -> tuple[str, dict]:
    payload = ev["payload"]
    role = payload["role"]
    spec = reviewer_def(role)

    attached = (reviewer_attached_for_request(project, slug, role, ev["event_id"])
                or latest_reusable_reviewer_attached(project, slug, role))
    if attached:
        ws = attached.get("workspace")
        surf = attached.get("surface")
        launcher = attached.get("launcher")
        if not ws or not surf or not valid_launcher(launcher):
            raise Fail(3, "연결된 리뷰어의 workspace/surface/launcher 기록이 불완전합니다")
        if launcher != payload["launcher"]:
            raise Fail(1, f"연결된 리뷰어 런처 {launcher} 와 요청 런처 "
                          f"{payload['launcher']} 가 다릅니다")
        watcher("bind", project, ev, ws, surf, launcher)
        watcher("record", project, ev, "launched", sent_at=time.time())
        sent = send_review_when_ready(project, slug, ev, ws, surf, launcher)
        return spec["to"], {"workspace": ws, "surface": surf, "launcher": launcher,
                            "role": role, "reviewer_mode": "reuse", "reused": True,
                            "awaiting_start": not sent}

    # The task's first review request: spawn the reviewer in the requester's pane.
    requester = payload.get("requester") or {}
    ws = requester.get("workspace")
    r_surf = requester.get("surface")
    if not ws or not r_surf:
        raise Fail(3, "요청 세션의 workspace/surface 가 기록돼 있지 않습니다")
    # Rendered here only to fail before a tab opens; the reviewer fetches it with `event prompt`.
    reviewer_prompt_for(project, slug, ev)
    line = render_template("reviewer-boot.md", {
        "PROJECT": project, "SLUG": slug, "EVENT_ID": ev["event_id"],
        "REVIEWER_ROLE": role, "PJ_CMUX": shlex.quote(str(SCRIPT_DIR / "pj-cmux.py"))})
    cwd = requester.get("cwd") or os.getcwd()
    side = side_pane(project, slug) if task_is_solo(project, slug) else None
    placement: dict = {}
    if side:
        # solo: the requester's pane holds the working session, so a tab there stays hidden
        # behind it. The reviewer goes to the side pane — the plan viewer's pane — as a tab,
        # like every other document.
        pane = side[0]
        surf = new_surface_in_pane(ws, pane, cwd)
        placement = {"pane": pane, "placement": "side"}
    else:
        if TEST:
            pane = "TEST-PANE"
        else:
            pane = None
            _, surfaces = cmux_tree(ws)
            for p, lst in surfaces.items():
                if any(s == r_surf.upper() for s, _ in lst):
                    pane = p
                    break
            if pane is None:
                raise Fail(3, "요청 세션의 pane 을 찾지 못했습니다")
        surf = new_surface_in_pane(ws, pane, cwd)
    watcher("bind", project, ev, ws, surf, payload["launcher"])
    append_event(reviewer_stream(project, slug, role),
                 make_event("role_attached", spec["to"], spec["to"], slug,
                            {"workspace": ws, "surface": surf, "role": role,
                             "launcher": payload["launcher"], "round": ev["round"],
                             # the marker latest_reusable_reviewer_attached() filters on
                             "reviewer_mode": "reuse", **placement},
                            round_=ev["round"], reply_to=ev["event_id"]))
    sent = boot_agent(ws, surf, payload["launcher"], line, ev["event_id"][-8:])
    watcher("record", project, ev, "launched", sent_at=time.time(), initial_sent=sent)
    return spec["to"], {"workspace": ws, "surface": surf, "launcher": payload["launcher"],
                        "role": role, "reviewer_mode": "reuse", "reused": False,
                        "awaiting_start": not sent, **placement}


def _pane_snapshot(workspace: str | None) -> dict[str, list[str]] | None:
    """pane → surface ids, or None when the tree cannot be read. Never fatal for the caller."""
    if TEST or not workspace:
        return {}
    try:
        _, surfaces = cmux_tree(workspace)
    except Fail:
        return None
    return {p: [s for s, _ in lst] for p, lst in surfaces.items()}


def _new_pane_since(workspace: str | None, before: dict | None) -> tuple[str, str] | None:
    """(pane, first surface) that appeared since `before`, polling briefly; None if unknown."""
    if TEST:
        return ("TEST-SIDE-PANE", "TEST-SIDE-SURFACE") if workspace else None
    if before is None or not workspace:
        return None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        after = _pane_snapshot(workspace)
        if after:
            fresh = [(p, lst[0]) for p, lst in after.items() if p not in before and lst]
            if fresh:
                return fresh[-1]
        time.sleep(1)
    return None


def deliver_markdown_open(project: str, slug: str, ev: dict) -> tuple[str, dict]:
    requester = ev["payload"].get("requester") or {}
    ws = requester.get("workspace")
    # A solo workspace boots the plan viewer at open. If that viewer (or an earlier one) is still
    # up, this request only confirms it — a second split would show the same file twice.
    existing = side_pane(project, slug)
    if existing and ws and surface_alive(ws, existing[1]):
        return "planner", {"argv": [], "reused": True,
                           "side_pane": existing[0], "side_surface": existing[1]}
    # No live viewer: open one where this topology keeps it (solo → own pane beside the
    # session, split → tab in the worker pane). Only a task never opened here (pre-separation)
    # falls through to the plain split beside the requester below.
    topo = task_topology(project, slug)
    if ws and topo in ("solo", "split"):
        planner = latest_role_attached(project, slug, "planner") or {}
        worker = latest_role_attached(project, slug, "worker") or {}
        p_surf, w_surf = planner.get("surface"), worker.get("surface")
        w_pane = pane_of_surface(ws, w_surf) if (topo == "split" and w_surf) else None
        if p_surf and (topo == "solo" or w_pane):
            result = {"argv": ["cmux", "markdown", "open", ev["payload"]["plan_path"]],
                      "placement": "own-pane" if topo == "solo" else "worker-tab"}
            side = place_plan_viewer(ev["payload"]["plan_path"], ws, topo == "solo",
                                     p_surf, w_surf, w_pane)
            if side:
                result["side_pane"], result["side_surface"] = side
            return "planner", result
    args = ["cmux", "markdown", "open", ev["payload"]["plan_path"]]
    if ws:
        args += ["--workspace", ws]
    if requester.get("surface"):
        args += ["--surface", requester["surface"]]
    before = _pane_snapshot(ws)
    run_cmux(args)
    result: dict = {"argv": args}
    # The viewer opens in a NEW pane beside the planner. Recorded as the task's side pane, it is
    # where documents open as tabs (`cmux open … --pane`) and where a solo task's reviewer
    # boots. Best effort: the viewer is open either way.
    side = _new_pane_since(ws, before)
    if side:
        result["side_pane"], result["side_surface"] = side
    return "planner", result


def deliver_review_complete(project: str, slug: str, ev: dict) -> tuple[str, dict]:
    """Sidebar pill on the task workspace (`pj work done`): every reviewer the round expected
    is terminal and /pj-done is the user's next move. Goes through the transport so Codex workers (sandboxed away from the cmux socket)
    get it too."""
    requester = ev["payload"].get("requester") or {}
    ws = requester.get("workspace")
    if not ws:
        raise Fail(3, "요청 세션의 workspace 가 기록돼 있지 않아 pill 을 놓을 곳이 없습니다")
    pill = ev["payload"].get("pill") or REVIEW_DONE_PILL
    argv = ["cmux", "set-status", pill["key"], pill["text"], "--priority", str(pill["priority"])]
    for flag in ("icon", "color"):          # optional styling, absent in the default pill
        if pill.get(flag):
            argv += [f"--{flag}", pill[flag]]
    argv += ["--workspace", ws]
    run_cmux(argv)
    return "worker", {"argv": argv, "workspace": ws}


def deliver_color_sync(project: str, slug: str | None, ev: dict) -> tuple[str, dict]:
    requester = ev["payload"].get("requester") or {}
    argv = [str(SYNC_COLOR)] + (["newc"] if ev["payload"]["mode"] == "new" else [])
    rc, out, err = run_helper(argv, cwd=requester.get("cwd"))
    if rc != 0 and not TEST:
        raise Fail(6, f"sync-color 실패: {err.strip() or out.strip()}")
    return "board", {"argv": argv, "output": out.strip()[:500]}


def boot_command(project: str, slug: str | None, role: str, launcher: str, prompt: str) -> str:
    """Persist prompt bytes; the terminal receives only one short command, never its body.

    Content-addressed files survive retries and remain available for manual recovery. The
    sentinel preserves trailing newlines in shell command substitution. File contents are
    passed as one argument, never parsed as shell code, and a read error prevents launch.
    """
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    directory = stream_path(project, slug, role).parent / "boot"
    path = directory / f"{role}-{digest}.txt"
    assert_no_symlinks(path)
    directory.mkdir(parents=True, exist_ok=True)
    assert_no_symlinks(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != prompt:
            raise Fail(4, f"부팅 프롬프트 파일 내용이 바뀌었습니다: {path}")
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt)
            fh.flush()
            os.fsync(fh.fileno())
    return (f"pj_boot_prompt=$(cat {shlex.quote(str(path))} && printf '.') && "
            f"{launcher_shell_command(launcher)} \"${{pj_boot_prompt%.}}\"")


def deliver_workspace_open(project: str, slug: str, ev: dict) -> tuple[str, dict]:
    p = ev["payload"]
    launchers = p["launchers"]
    solo = bool(p.get("solo"))
    pane1 = boot_command(project, slug, "planner", launchers["plan"], p["planner_prompt"])
    if solo:
        # one pane, one session: the planner's launcher boots into pj-plan and that same
        # session later runs pj-work — there is no waiting prompt to render.
        layout = json.dumps({"pane": {"surfaces": [{"type": "terminal", "command": pane1}]}})
    else:
        waiting = render_template("worker-waiting-prompt.md", {
            "PROJECT": project, "SLUG": slug,
            "SKILL_SIGIL": sigil_for(launchers["work"]),
            "PJ_CMUX": shlex.quote(str(SCRIPT_DIR / "pj-cmux.py")),
            # the worker resolves the project's context ITSELF, from where it stands: the
            # paths differ by session root, and the transport does not know its cwd here
            "PJ_CTX": shlex.quote(str(SCRIPT_DIR / "pj-ctx.py")),
            "OPTIONAL_LINES": "\n".join(render_fragment(f"switch-{s}")
                                        for s in p.get("switches", [])),
        })
        lead_preamble = "sleep 2; " if launcher_runtime(launchers["plan"]) == "grok" else POLL_PREAMBLE
        pane2 = (lead_preamble
                 + boot_command(project, slug, "worker", launchers["work"], waiting))
        layout = json.dumps({
            "direction": "horizontal", "split": 0.5,
            "children": [
                {"pane": {"surfaces": [{"type": "terminal", "command": pane1}]}},
                {"pane": {"surfaces": [{"type": "terminal", "command": pane2}]}},
            ]})
    ws_ref, ws_uuid, color = create_workspace(project, p, layout, workspace_cwd(p))
    # positional surface recording: pane order == layout tree-walk order (planner, worker).
    # solo has one pane and both roles attach to it — that record is how the transport later
    # knows there is no separate worker or planner session to type into.
    need = 1 if solo else 2
    if TEST:
        surfaces_in_order = (["TEST-SOLO-SURFACE"] if solo
                             else ["TEST-PLANNER-SURFACE", "TEST-WORKER-SURFACE"])
        panes_in_order = ["TEST-SOLO-PANE"] if solo else ["TEST-PLANNER-PANE", "TEST-WORKER-PANE"]
    else:
        panes, surfaces = cmux_tree(ws_uuid if UUID_RE.match(ws_uuid or "") else ws_ref)
        panes_in_order = [pn for pn in panes if surfaces.get(pn)]
        surfaces_in_order = [surfaces[pn][0][0] for pn in panes_in_order]
    if len(surfaces_in_order) < need:
        raise Fail(6, f"생성된 워크스페이스에서 pane {need}개의 surface 를 찾지 못했습니다")
    worker_surface = surfaces_in_order[0] if solo else surfaces_in_order[1]
    worker_pane = panes_in_order[0] if solo else panes_in_order[1]
    side = boot_plan_viewer(project, slug, ws_uuid, solo, surfaces_in_order[0],
                            worker_surface, worker_pane)
    for role, surf, key in (("planner", surfaces_in_order[0], "plan"),
                            ("worker", worker_surface, "work")):
        attached = {"workspace": ws_uuid, "surface": surf, "launcher": launchers[key],
                    "solo": solo}
        if side:
            attached["side_pane"], attached["side_surface"] = side
        append_event(stream_path(project, slug, role),
                     make_event("role_attached", role, role, slug, attached))
    return "board", {"workspace": ws_uuid, "workspace_ref": ws_ref,
                     "surfaces": surfaces_in_order[:need], "color": color or "",
                     "solo": solo, **({"side_pane": side[0], "side_surface": side[1]}
                                      if side else {})}


def workspace_cwd(p: dict) -> str:
    """Where the session stands. One worktree means that worktree, exactly as before. Several
    means their shared parent — which is deliberately not a git repo, because one session edits
    all of them and no single repo is "the" one. `pj-repos.py` is what turns either into
    [(repo, branch, worktree)], so nothing downstream has to know which case it got."""
    pairs = worktree_pairs(p)
    if len(pairs) > 1:
        parents = {os.path.dirname(os.path.realpath(wt)) for wt, _ in pairs}
        if len(parents) == 1:
            return parents.pop()
        log("워크트리들이 형제가 아니어서 첫 워크트리를 cwd 로 씁니다")
    return pairs[0][0]


def create_workspace(project: str, p: dict, layout: str,
                     cwd: str) -> tuple[str, str, str]:
    """Create the cmux workspace and everything that is true of it regardless of what runs
    inside: name, description, the project's colour, the requester's group, and a registry
    entry per worktree. Returns (ref, uuid, colour).

    Shared by the task opener and the board opener because only the LAYOUT and the sessions
    booted into it differ; duplicating the rest is how the two would drift.
    """
    out = run_cmux(["cmux", "new-workspace", "--name", p["name"], "--cwd", cwd,
                    "--layout", layout])
    m = re.search(r"workspace:\d+", out)
    ws_ref = m.group(0) if m else ("workspace:TEST" if TEST else None)
    if not ws_ref:
        raise Fail(6, f"워크스페이스 ref 를 찾지 못했습니다: {out.strip()}")
    ws_uuid = "TEST-WS-UUID"
    if not TEST:
        # measured shape: {"window_ref": "...", "workspaces": [{"id": UUID,
        # "custom_title": <name>, ...}]}
        listing = run_cmux(["cmux", "workspace", "list", "--json"], check=False)
        try:
            for row in json.loads(listing).get("workspaces", []):
                if row.get("custom_title") == p["name"] and row.get("id"):
                    ws_uuid = row["id"]
        except (ValueError, AttributeError):
            pass
        if ws_uuid == "TEST-WS-UUID":
            raise Fail(6, f"생성된 워크스페이스의 UUID 를 찾지 못했습니다: {p['name']!r} — "
                          "role_attached 는 UUID 로만 기록합니다 (ref 는 재번호 인덱스)")
    if p.get("description"):
        run_cmux(["cmux", "workspace-action", "--action", "set-description",
                  "--workspace", ws_ref, "--description", p["description"]], check=False)
    color = project_color(project)
    if color:
        run_cmux(["cmux", "workspace-action", "--action", "set-color",
                  "--color", color, "--workspace", ws_ref])
    requester = p.get("requester") or {}
    if requester.get("cwd"):
        rc, out2, _ = run_helper([str(RESOLVE_WS_GROUP), "--cwd", requester["cwd"]])
        group_ref = out2.split("\t")[0].strip() if rc == 0 and out2.strip() else ""
        if group_ref:
            run_cmux(["cmux", "workspace-group", "add", "--group", group_ref,
                      "--workspace", ws_ref], check=False)
    register_worktrees(p, ws_ref)
    return ws_ref, ws_uuid, color


def main_repo_of(worktree: str) -> str:
    """The main checkout a worktree belongs to, asked of git.

    git is asked first and always — including under TEST, because a test with real repos behind
    it must get the real answer. The path-shape fallback applies only when git CANNOT answer,
    which under TEST means a fixture path with no repo behind it; keying the fallback on "git
    said no" rather than on "we are testing" is what keeps the two cases from diverging.
    """
    try:
        r = subprocess.run(["git", "-C", worktree, "rev-parse",
                            "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True)
    except OSError:
        r = None
    if r is not None and r.returncode == 0:
        return r.stdout.strip().removesuffix("/.git").rstrip("/")
    if TEST and "/.worktrees/" in worktree:
        return worktree.split("/.worktrees/")[0]
    return ""


def register_worktrees(p: dict, ws_ref: str) -> None:
    """Record every worktree this workspace was opened on in its repo's registry.

    The repo is asked of git, not carved out of the path. It used to be
    `worktree.split("/.worktrees/")[0]`, which silently recorded NOTHING once worktrees moved
    outside the repo — the substring simply stops matching, and a skipped registration is
    invisible until worktree registry reconciliation later cannot find the workspace. `--git-common-dir` answers for
    both layouts, and `path` is stored absolute for the external one (the registry accepts
    either form).

    One entry per repo: a combined task has several worktrees under one parent and each repo
    keeps its own registry, so the same workspace name appears once in each.
    """
    for wt, branch in worktree_pairs(p):
        if not branch:
            continue
        main_repo = main_repo_of(wt)
        if not main_repo:
            log(f"레지스트리 등록 건너뜀 — repo 를 찾지 못했습니다: {wt}")
            continue
        nested = os.path.realpath(wt).startswith(os.path.realpath(main_repo) + os.sep)
        run_helper([str(WT_REGISTRY), "add", "--repo", main_repo,
                    "--branch", branch,
                    "--base", base_branch_for(p, wt) or "",
                    "--path", os.path.relpath(wt, main_repo) if nested else wt,
                    "--workspace-name", p["name"], "--workspace-id", ws_ref])


def base_branch_for(p: dict, worktree: str) -> str:
    """The branch this worktree was cut from. `base_branch` is the project's branch, which for a
    multi-repo project is a comma list paired positionally with `repo_wt` — so the right element
    is the one at this worktree's index, not the whole field."""
    base = p.get("base_branch") or ""
    bases = [b for b in base.split(",") if b]
    if len(bases) <= 1:
        return base
    for i, (wt, _) in enumerate(worktree_pairs(p)):
        if wt == worktree:
            return bases[i] if i < len(bases) else ""
    return ""


def worktree_pairs(p: dict) -> list[tuple[str, str]]:
    """[(worktree, branch)] for the payload — one pair for a single-repo task, and for a
    combined one the pairs recorded under `repo_wt`. Single-repo payloads keep the flat
    `worktree`/`branch` fields they always had, so nothing downstream had to change shape."""
    pairs = [(e["worktree"], e.get("branch") or "")
             for e in (p.get("repo_wt") or []) if e.get("worktree")]
    return pairs or [(p["worktree"], p.get("branch") or "")]


def project_context_line(project: str) -> str:
    """The project's context documents, for a reviewer that has none of the worker's context.

    Resolved through pj-ctx.py, the one place that knows the field's shape and how a path is
    prefixed. It used to be the repo alias's `ctxDomain`, which cannot answer either question:
    context belongs to the project (a repo may hold several), and a repo-keyed lookup has no
    entry at all for a task spanning two.

    Best effort — a reviewer with no context line is worse than one with, but a failure here
    must not block the review request.
    """
    rc, out, _ = run_helper([sys.executable, str(SCRIPT_DIR / "pj-ctx.py"),
                             "--project", project])
    paths = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if rc != 0 or not paths:
        return ""
    return render_fragment("context-line", {"PATHS": ", ".join(paths[:8])})


def boot_plan_viewer(project: str, slug: str, workspace: str, solo: bool,
                     planner_surface: str, worker_surface: str,
                     worker_pane: str) -> tuple[str, str] | None:
    """Put the plan document up from the first second. The skeleton (title + 최초 요청) is
    already on disk from pj-task-regi and the viewer watches the file, so it fills in as pj-plan
    writes. Best effort — on failure pj-plan's markdown.open opens it later."""
    plan_path = TASKS_DIR / project / f"{slug}.md"
    if not TEST and not plan_path.is_file():
        log(f"계획 문서가 없어 뷰어를 미리 열지 않습니다: {plan_path}")
        return None
    return place_plan_viewer(str(plan_path), workspace, solo, planner_surface,
                             worker_surface, worker_pane)


def place_plan_viewer(plan_path: str, workspace: str, solo: bool, planner_surface: str,
                      worker_surface: str | None,
                      worker_pane: str | None) -> tuple[str, str] | None:
    """Open the live plan viewer where the topology wants it and return (side_pane,
    side_surface). solo: its own pane right of the session. split: a TAB in the worker pane —
    `cmux markdown open` can only split, so it is split off the worker surface and moved in,
    which leaves the planner its full width and empties the temporary pane away."""
    src = planner_surface if solo else worker_surface
    if not src:
        return None
    args = ["cmux", "markdown", "open", plan_path, "--workspace", workspace,
            "--surface", src, "--direction", "right", "--focus", "false"]
    before = _pane_snapshot(workspace)
    try:
        run_cmux(args)
    except Fail as e:
        log(f"계획 뷰어 열기 실패: {e}")
        return None
    opened = _new_pane_since(workspace, before)
    if not opened or solo or not worker_pane:
        return opened
    viewer = opened[1]
    try:
        run_cmux(["cmux", "move-surface", "--surface", viewer, "--pane", worker_pane,
                  "--workspace", workspace, "--focus", "false"])
    except Fail as e:
        log(f"뷰어를 worker pane 으로 옮기지 못해 별도 pane 으로 남습니다: {e}")
        return opened
    return worker_pane, viewer


def pane_of_surface(workspace: str, surface: str) -> str | None:
    if TEST:
        return "TEST-WORKER-PANE"
    _, surfaces = cmux_tree(workspace)
    for p, lst in surfaces.items():
        if any(s == surface.upper() for s, _ in lst):
            return p
    return None


DELIVER = {
    "watcher_start": deliver_watcher_start,
    "workspace_open": deliver_workspace_open,
    "workspace_board": deliver_workspace_board,
    "markdown_open": deliver_markdown_open,
    "color_sync": deliver_color_sync,
    "review_complete": deliver_review_complete,
    "worker_handoff": deliver_send,
    "review_requested": deliver_review_request,
    "code_review": deliver_send,
    "plan_review": deliver_send,
    "decision_request": deliver_send,
    "decision_reply": deliver_send,
    "done_report": deliver_send,
}


def relay_request(request_id: str, slug: str | None) -> str:
    """Execute one pending request. Returns a one-line human summary for hook feedback."""
    return relay_request_detail(request_id, slug)[0]


def relay_request_detail(request_id: str, slug: str | None) -> tuple[str, dict]:
    """relay_request plus the terminal event's payload (e.g. `self` for a solo delivery)."""
    project, source_role, ev, src_path = find_request(request_id, slug)
    ev_slug = ev.get("slug")
    if ev["type"] == "review_requested":
        watcher("register", project, ev)
        watcher("ensure", project)
    with request_lock(request_id):
        existing = find_terminal(read_stream(src_path), ev)
        if existing:
            return (f"pj-cmux: {request_id} already "
                    f"{existing['type']} ({existing['event_id']})",
                    existing.get("payload") or {})
        deliver = DELIVER.get(ev["type"])
        if deliver is None:
            raise Fail(5, f"{ev['type']} 는 릴레이 대상이 아닙니다")
        try:
            target_role, result = deliver(project, ev_slug, ev)
        except Fail as e:
            append_event(src_path, make_event(
                "delivery_failed", source_role, ev["to"], ev_slug,
                {"reason": str(e), "code": e.code, "target_role": ev["to"]},
                round_=ev.get("round", 0), reply_to=ev["event_id"]))
            if ev["type"] == "review_requested":
                watcher("record", project, ev, "failed", reason=str(e))
            raise
        term_type = ("action_completed"
                     if ev["type"] in ("workspace_open", "workspace_board",
                                       "markdown_open", "color_sync",
                                       "review_complete", "watcher_start")
                     else "delivered")
        if result.get("awaiting_start"):
            term_type = "review_launch_queued"
        term = make_event(term_type, source_role, ev["to"], ev_slug,
                          {**result, "target_role": target_role},
                          round_=ev.get("round", 0), reply_to=ev["event_id"])
        append_event(src_path, term)
        tag = " (self)" if result.get("self") else ""
        return (f"pj-cmux: {request_id} {term_type} → {target_role}{tag} ({term['event_id']})",
                result)


def op_relay(a) -> int:
    msg = relay_request(a.request_id, a.slug)
    print(f"PJ_CMUX=relayed request={a.request_id}")
    log(msg)
    return 0


def op_retry(a) -> int:
    project, _, ev, src_path = find_request(a.request_id, a.slug)
    if find_terminal(read_stream(src_path), ev):
        print(f"PJ_CMUX=already-delivered request={a.request_id}")
        if ev["type"] == "review_requested":
            print("리뷰 시작 여부와 별개인 전송 기록입니다. 시작 대기/재전송은 watcher 전담입니다. "
                  "새 review.request로 우회하지 마세요. watcher status로 확인하고, 시작 실패는 "
                  "원인을 실제 수정하기 전까지 재요청하지 마세요.")
        return 0
    if should_relay_inline():
        msg = relay_request(a.request_id, a.slug)
        log(msg)
        print(f"PJ_CMUX=delivered request={a.request_id}")
        return 0
    print(f"event={ev['event_id']}")
    print(f"PJ_CMUX_REQUEST={a.request_id}")
    return 0


def op_ack(a) -> int:
    if a.status not in ACK_STATUSES:
        raise Fail(1, f"--status 는 {'/'.join(ACK_STATUSES)} 중 하나입니다")
    project, _, ev, _ = find_request(a.request_id, a.slug)
    processor = ev["to"]
    path = stream_path(project, ev.get("slug") if processor != "board" else None, processor)
    for existing in read_stream(path):
        if existing.get("type") == "ack" and existing.get("reply_to") == ev["event_id"]:
            prev = (existing.get("payload") or {}).get("status")
            if prev == a.status:
                print(f"PJ_CMUX=ok action=ack status={a.status} result=duplicate")
                return 0
            raise Fail(4, f"이미 {prev} 로 ack 된 요청입니다: {a.request_id}")
    append_event(path, make_event("ack", processor, ev["from"], ev.get("slug"),
                                  {"status": a.status}, round_=ev.get("round", 0),
                                  reply_to=ev["event_id"]))
    print(f"PJ_CMUX=ok action=ack status={a.status} request={a.request_id}")
    return 0


# ---------- event / status ----------

def op_event(a) -> int:
    info = task_info(a.slug)
    project = info["project"]
    events = load_events(project, a.slug)
    if a.mode in ("read", "prompt"):
        if not a.event_id:
            raise Fail(1, f"event {a.mode} 는 --event-id 가 필요합니다")
        for _, ev in events:
            if ev["event_id"] == a.event_id:
                if ev.get("digest") != digest_of(ev):
                    raise Fail(4, f"이벤트 digest 불일치: {a.event_id}")
                if a.mode == "read":
                    print(json.dumps(ev, ensure_ascii=False, indent=2))
                    return 0
                role = (ev.get("payload") or {}).get("role")
                if ev.get("type") != "review_requested" or role not in reviewer_roles():
                    raise Fail(1, f"event prompt 는 리뷰어의 review_requested "
                                  f"이벤트에만 쓸 수 있습니다: {a.event_id}")
                print(reviewer_prompt_for(project, a.slug, ev), end="")
                return 0
        raise Fail(5, f"이벤트를 찾지 못했습니다: {a.event_id}")
    rows = [dict(ev, stream=role) for role, ev in events
            if (not a.role or role == a.role)
            and (not a.type or ev.get("type") == a.type)
            and (a.round is None or ev.get("round") == a.round)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def fold_status(events: list[tuple[str, dict]], req_ev: dict) -> dict:
    state = "pending"
    detail: dict = {}
    for _, ev in events:
        if ev.get("reply_to") != req_ev["event_id"]:
            continue
        t = ev.get("type")
        if t == "review_started":
            state, detail = "started", ev.get("payload", {})
        elif t == "review_start_failed" and state != "started":
            state, detail = "failed", ev.get("payload", {})
        elif t == "review_launch_queued" and state == "pending":
            state, detail = "starting", ev.get("payload", {})
        elif t in ("delivered", "action_completed") and state in ("pending", "failed"):
            state, detail = t, ev.get("payload", {})
        elif t == "delivery_failed" and state == "pending":
            state, detail = "failed", ev.get("payload", {})
        elif t == "ack":
            state = (ev.get("payload") or {}).get("status", "processed")
            detail = ev.get("payload", {})
    return {"request_id": req_ev.get("request_id"), "event_id": req_ev["event_id"],
            "type": req_ev.get("type"), "round": req_ev.get("round"),
            "to": req_ev.get("to"), "state": state, "detail": detail}


def op_status(a) -> int:
    if a.request_id:
        project, _, ev, _ = find_request(a.request_id, a.slug)
        events = load_events(project, ev.get("slug"))
        print(json.dumps(fold_status(events, ev), ensure_ascii=False, indent=2))
        return 0
    if not a.slug:
        raise Fail(1, "status 는 --request-id 또는 --slug 가 필요합니다")
    info = task_info(a.slug)
    events = load_events(info["project"], a.slug)
    rows = [fold_status(events, ev) for _, ev in events
            if ev.get("request_id")
            and (a.round is None or ev.get("round") == a.round)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def op_topology(a) -> int:
    info = task_info(a.slug)
    print(f"PJ_TOPOLOGY={task_topology(info['project'], a.slug)}")
    side = side_pane(info["project"], a.slug)
    if side:
        print(f"side_pane={side[0]} side_surface={side[1]}")
    return 0


def op_launcher(a) -> int:
    runtime = valid_runtime(getattr(a, "runtime", None))
    code, source = parse_launcher(a.role, a.hint or "", a.repo, runtime,
                                  getattr(a, "reviewer", None))
    print(f"PJ_LAUNCHER={code} role={a.role} runtime={runtime} source={source}")
    return 0


# ---------- hook ----------

def hook_run(runtime: str) -> int:
    """Never break the enclosing tool call: parse errors, missing markers, and relay failures
    all end in exit 0 with valid hook JSON."""
    try:
        raw = sys.stdin.read()
    except OSError:
        raw = ""
    ids = list(dict.fromkeys(MARKER_RE.findall(raw)))[:4]
    feedback: list[str] = []
    for rid in ids:
        try:
            feedback.append(relay_request(rid, None))
        except Fail as e:
            feedback.append(f"pj-cmux: {rid} PENDING — {e}. "
                            f"Retry: pj-cmux.py retry --request-id {rid}")
        except Exception as e:  # noqa: BLE001 — a hook must never raise
            feedback.append(f"pj-cmux: {rid} PENDING — unexpected: {e!r}")
    if runtime == "codex":
        # Codex supports hookSpecificOutput.additionalContext too (verified on 0.147.0), but
        # NOT suppressOutput/continue — unsupported keys mark the hook run failed. Quiet = {}.
        if feedback:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n".join(feedback)}}))
        else:
            print("{}")
        return 0
    if feedback:
        print(json.dumps({"continue": True, "hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": "\n".join(feedback)}}))
    else:
        print(json.dumps({"continue": True, "suppressOutput": True}))
    return 0


HOOK_CMD = shlex.join([sys.executable, str(SCRIPT_DIR / "pj-cmux.py"), "hook", "run", "--runtime", "claude"])
CODEX_HOOK_SH = SCRIPT_DIR / "pj-cmux-codex-hook.sh"
CODEX_SENTINEL_BEGIN = "# BEGIN PJ-CMUX HOOKS"
CODEX_SENTINEL_END = "# END PJ-CMUX HOOKS"


def claude_hook_entry() -> dict:
    return {"matcher": "Bash",
            "hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 90}]}


def install_claude(settings_path: pathlib.Path, dry_run: bool) -> str:
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except OSError:
        data = {}
    except ValueError as e:
        raise Fail(2, f"{settings_path} 파싱 실패: {e} — 손대지 않습니다")
    hooks = data.setdefault("hooks", {})
    ptu = hooks.setdefault("PostToolUse", [])
    for entry in ptu:
        if any(HOOK_CMD in (h.get("command") or "") for h in entry.get("hooks", [])):
            return f"{settings_path}: 이미 설치됨"
    ptu.append(claude_hook_entry())
    if dry_run:
        return f"{settings_path}: [dry-run] PostToolUse 항목 추가 예정"
    backup = settings_path.with_suffix(
        f".json.bak-pj-cmux-{datetime.date.today().strftime('%Y%m%d')}")
    if settings_path.exists():
        shutil.copy2(settings_path, backup)
    settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return f"{settings_path}: 설치됨 (백업: {backup.name})"


def install_codex(config_path: pathlib.Path, dry_run: bool) -> str:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as e:
        raise Fail(2, f"{config_path} 를 읽을 수 없습니다: {e}")
    if CODEX_SENTINEL_BEGIN in text:
        return f"{config_path}: 이미 설치됨"
    # timeout is SECONDS on codex (Duration::from_secs — verified in source), and new
    # same-event blocks must APPEND after existing ones: the hook trust key includes the
    # group index, so inserting earlier would invalidate a neighbor's trust.
    block = (f"\n{CODEX_SENTINEL_BEGIN}\n"
             "[[hooks.PostToolUse]]\n"
             "matcher = \"Bash\"\n"
             "[[hooks.PostToolUse.hooks]]\n"
             "type = \"command\"\n"
             f"command = {json.dumps(shlex.quote(str(CODEX_HOOK_SH)))}\n"
             "timeout = 90\n"
             f"{CODEX_SENTINEL_END}\n")
    if dry_run:
        return f"{config_path}: [dry-run] PostToolUse 블록 추가 예정:\n{block}"
    backup = config_path.with_name(
        f"config.toml.bak-pj-cmux-{datetime.date.today().strftime('%Y%m%d')}")
    shutil.copy2(config_path, backup)
    config_path.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
    return f"{config_path}: 설치됨 (백업: {backup.name})"


def op_hook(a) -> int:
    if a.mode == "run":
        return hook_run(a.runtime or "claude")
    targets = []
    if a.claude:
        targets.append(("claude", pathlib.Path.home() / ".claude" / "settings.json"))
    if a.codex:
        targets.append(("codex", pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")) / "config.toml"))
    if a.mode == "doctor":
        if not targets:
            # hooks are codex-only: Claude/Grok/shell requests relay inline
            targets = [("codex", pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")) / "config.toml")]
        ok = True
        for kind, path in targets:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                print(f"MISSING  {path} (파일 없음)")
                ok = False
                continue
            needle = HOOK_CMD if kind == "claude" else CODEX_SENTINEL_BEGIN
            state = "OK      " if needle in text else "ABSENT  "
            ok = ok and needle in text
            print(f"{state} {path}")
        if not CODEX_HOOK_SH.exists() or not os.access(CODEX_HOOK_SH, os.X_OK):
            print(f"ABSENT   {CODEX_HOOK_SH} (실행 가능해야 함)")
            ok = False
        else:
            print(f"OK       {CODEX_HOOK_SH}")
        print("NOTE     This checks configuration only. Verify hook support and trust in your "
              "installed Codex. No permission or trust bypass flags are added. "
              "If hooks are unavailable, relay requests explicitly from an authorized shell.")
        return 0 if ok else 3
    if a.mode == "install":
        if not targets:
            raise Fail(1, "hook install 은 --claude / --codex 가 필요합니다")
        for kind, path in targets:
            msg = install_claude(path, a.dry_run) if kind == "claude" \
                else install_codex(path, a.dry_run)
            print(msg)
        return 0
    raise Fail(1, f"hook 모드가 잘못됐습니다: {a.mode}")


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("request")
    p.add_argument("action")
    p.add_argument("--slug")
    p.add_argument("--project")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--role")
    p.add_argument("--round", type=int)
    p.add_argument("--reply-to", dest="reply_to")
    p.add_argument("--launcher")
    p.add_argument("--findings", help="재리뷰 대상 finding 이벤트 id (쉼표 구분)")
    p.add_argument("--commit")
    p.add_argument("--result", action="append", metavar="REPO:COMMIT[:TYPECHECK]",
                   help="done.report: 작업이 타깃한 repo 마다 하나씩 (반복). "
                        "--commit/--typecheck 의 다중 repo 형태")
    p.add_argument("--typecheck", help="선택적 검사 결과 기록; 생략하면 none (미실행)")
    p.add_argument("--mode")
    p.add_argument("--planner", help="workspace.open: planner 런처")
    p.add_argument("--worker", help="workspace.open: worker 런처")
    p.add_argument("--reviewer", help="workspace.open: reviewer 런처")
    p.add_argument("--solo", action="store_true",
                   help="workspace.open: planner 와 worker 를 한 세션(pane 1개)으로 — "
                        "--worker 는 생략")
    p.add_argument("--name")
    p.add_argument("--worktree")
    p.add_argument("--branch", help="workspace.open: create-worktree 가 만든 태스크 브랜치")
    p.add_argument("--repo-wt", action="append", metavar="REPO:WORKTREE:BRANCH",
                   help="workspace.board: 이 보드가 소유하는 repo 마다 하나씩 (반복)")
    p.add_argument("--board", help="workspace.board: 보드 세션 런처 코드")
    p.add_argument("--board-prompt", help="workspace.board: 보드 세션의 첫 메시지 ('-' 는 stdin)")
    p.add_argument("--description")
    p.add_argument("--switch", action="append")
    p.add_argument("--planner-prompt", dest="planner_prompt",
                   help="'-' 이면 stdin (이때 --stdin 금지)")
    p.add_argument("--runtime", choices=list(RUNTIMES),
                   help=f"workspace.open: 이 태스크의 런타임 (기본 {DEFAULT_RUNTIME}). "
                        "모든 role·리뷰어 종류가 이 열의 런처로 해석됩니다")
    p.set_defaults(fn=op_request)

    p = sub.add_parser("relay")
    p.add_argument("--request-id", dest="request_id", required=True)
    p.add_argument("--slug")
    p.set_defaults(fn=op_relay)

    p = sub.add_parser("retry")
    p.add_argument("--request-id", dest="request_id", required=True)
    p.add_argument("--slug")
    p.set_defaults(fn=op_retry)

    p = sub.add_parser("ack")
    p.add_argument("--slug", required=True)
    p.add_argument("--request-id", dest="request_id", required=True)
    p.add_argument("--status", required=True)
    p.set_defaults(fn=op_ack)

    p = sub.add_parser("event")
    p.add_argument("mode", choices=["read", "list", "prompt"])
    p.add_argument("--slug", required=True)
    p.add_argument("--event-id", dest="event_id")
    p.add_argument("--role", choices=list(ROLES))
    p.add_argument("--type")
    p.add_argument("--round", type=int)
    p.set_defaults(fn=op_event)

    p = sub.add_parser("status")
    p.add_argument("--request-id", dest="request_id")
    p.add_argument("--slug")
    p.add_argument("--round", type=int)
    p.set_defaults(fn=op_status)

    p = sub.add_parser("topology")
    p.add_argument("--slug", required=True)
    p.set_defaults(fn=op_topology)

    p = sub.add_parser("watcher", help="보드별 리뷰 시작 감시")
    p.add_argument("mode", choices=["start", "run", "stop", "status", "tick"])
    p.add_argument("--project", required=True)
    p.set_defaults(fn=op_watcher)

    p = sub.add_parser("night-check", help="night: 실행 상태와 실제 리뷰 회신·처리를 확인")
    p.add_argument("--slug", required=True)
    p.set_defaults(fn=op_night_check)

    p = sub.add_parser("launcher")
    p.add_argument("mode", choices=["parse"])
    p.add_argument("--role", required=True, choices=list(ROLE_KEYS))
    p.add_argument("--hint", default="")
    p.add_argument("--repo")
    p.add_argument("--runtime", choices=list(RUNTIMES),
                   help=f"해석할 런처 테이블 열 (기본 {DEFAULT_RUNTIME})")
    p.add_argument("--reviewer",
                   help="--role review: 기본값을 이 리뷰어 정의에서 읽는다 (기본 code)")
    p.set_defaults(fn=op_launcher)

    p = sub.add_parser("hook")
    p.add_argument("mode", nargs="?", default="run", choices=["run", "install", "doctor"])
    p.add_argument("--runtime", choices=["claude", "codex"])
    p.add_argument("--claude", action="store_true")
    p.add_argument("--codex", action="store_true")
    p.add_argument("--dry-run", dest="dry_run", action="store_true")
    p.set_defaults(fn=op_hook)

    a = ap.parse_args()
    try:
        return a.fn(a)
    except Fail as e:
        log(str(e))
        return e.code


if __name__ == "__main__":
    sys.exit(main())
