"""Board-scoped startup watcher. Only fixed startup actions; never review execution.

All durable state is append-only exchange events. The relay registers/binds requests;
one process per project polls them until the exact review_started acknowledgement.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

INTERVAL = 10
TIMEOUT = 180
MAX_ACTIONS = 4
MAX_RESENDS = 2
TERMINAL = {"completed", "failed", "cancelled"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def tab_attachment(t, project):
    return next((e["payload"] for e in reversed(t.read_stream(t.stream_path(project, None, "board")))
                 if e["type"] == "watcher_attached"), None)


def tab_launcher(t, project, workspace, surface, entry, request):
    """Reuse the board's configured profile, never a worker's runtime or name prefix."""
    payload = request.get("payload") or {}
    board = payload.get("board_launcher")
    if not board:
        rows = t.read_stream(t.stream_path(project, None, "board"))
        opened = {e["reply_to"] for e in rows if e["type"] == "action_completed"
                  and e["payload"].get("workspace") == workspace}
        board = next((e["payload"].get("launchers", {}).get("board") for e in reversed(rows)
                      if e["type"] == "workspace_board" and e["event_id"] in opened), None)
    board = board or entry.get("profile")
    if board and t.valid_launcher(board):
        if entry.get("kind") and t.launcher_runtime(board) != entry["kind"]:
            raise t.Fail(3, "보드 프로필과 등록된 runtime이 다릅니다")
        return board
    raise t.Fail(3, "보드 프로필을 확인할 수 없습니다. watcher.start --board <profile-id>를 지정하세요")


def ensure_tab(t, project, request):
    with t.request_lock(daemon_key(t, project) + "-tab"):
        previous = tab_attachment(t, project)
        if previous:
            _, panes = t.cmux_tree(previous["workspace"])
            if any(s.upper() == previous["surface"].upper() for rows in panes.values() for s, _ in rows):
                t.run_cmux(["cmux", "rename-tab", "--workspace", previous["workspace"],
                            "--surface", previous["surface"], "watcher"])
                return {**previous, "reused": True}
        workspace, board_surface, _ = t.resolve_role_target(project, None, "board")
        t.validate_target(workspace, board_surface)
        try:
            _, entry = t.registry_find_surface(board_surface, workspace)
        except t.Fail:
            entry = {}  # Native boards need explicit profile and their own request context.
        try:
            entry = {**entry, **json.loads(entry.get("context", "{}"))}
        except (ValueError, TypeError):
            raise t.Fail(3, "보드 registry context가 잘못됐습니다")
        payload = request.get("payload") or {}
        caller = payload.get("requester") or {}
        own_request = (caller.get("workspace", "").upper() == workspace.upper()
                       and caller.get("surface", "").upper() == board_surface.upper())
        if own_request:
            entry = {**(payload.get("board_context") or {}), **entry}
            entry.setdefault("cwd", caller.get("cwd"))
        launcher = tab_launcher(t, project, workspace, board_surface, entry, request)
        _, panes = t.cmux_tree(workspace)
        pane = next((p for p, rows in panes.items()
                     if any(s.upper() == board_surface.upper() for s, _ in rows)), None)
        if not pane:
            raise t.Fail(3, "보드의 pane을 찾을 수 없어 watcher 탭을 만들지 못했습니다")
        cwd = entry.get("cwd")
        if not cwd:
            raise t.Fail(3, "보드 작업 디렉터리가 등록돼 있지 않습니다")
        skill = t.SCRIPT_DIR.parent.parent / "pj-watcher" / "SKILL.md"
        prompt = (f"Read {skill} and act as the watcher console for project {project}. "
                  "The board-scoped Python daemon owns periodic startup checks, dialogs and resends. "
                  "Run watcher status once, report in the user's language, then yield until a failure event "
                  "or user message arrives. Never create another watcher tab, poll in a loop, or "
                  "generate replacement review requests on timeout. Follow the console procedure.")
        command = t.boot_command(project, None, "board", launcher, prompt)
        if t.launcher_runtime(launcher) == "codex":
            account = t.launcher_profiles()[launcher].get("env", {}).get("CODEX_HOME") or entry.get("codex_home")
            if not isinstance(account, str) or not account or "\0" in account:
                raise t.Fail(3, "보드의 CODEX_HOME을 확인할 수 없습니다. 보드에서 watcher.start를 실행하거나 프로필 env를 지정하세요")
            command = f"export CODEX_HOME={shlex.quote(str(Path(account).expanduser()))}; " + command
        surface = t.new_surface_in_pane(workspace, pane, cwd, command=command)
        attached = {"workspace": workspace, "surface": surface, "pane": pane,
                    "board_surface": board_surface, "launcher": launcher, "cwd": cwd}
        # Persist before renaming, so a rename failure/retry cannot create a second tab.
        t.append_event(t.stream_path(project, None, "board"),
                       t.make_event("watcher_attached", "board", "board", None, attached,
                                    reply_to=request.get("event_id")))
        t.run_cmux(["cmux", "rename-tab", "--workspace", workspace, "--surface", surface, "watcher"])
        return {**attached, "reused": False}


def record(t, project, request, state, **fields):
    rows = t.read_stream(t.stream_path(project, request["slug"], "worker"))
    previous = next((e["payload"] for e in reversed(rows)
                     if e["type"] == "review_watch" and e.get("reply_to") == request["event_id"]), {})
    payload = {**previous, **fields, "state": state}
    event = t.make_event("review_watch", "worker", "worker", request["slug"], payload,
                         round_=request["round"], reply_to=request["event_id"])
    t.append_event(t.stream_path(project, request["slug"], "worker"), event)
    return payload


def registered(t, project, request):
    return any(e["type"] == "review_watch" and e.get("reply_to") == request["event_id"]
               for e in t.read_stream(t.stream_path(project, request["slug"], "worker")))


def register(t, project, request):
    if registered(t, project, request):
        return
    # Trust only disk-resolved worktrees belonging to this task, not arbitrary screen paths.
    cwd = (request["payload"].get("requester") or {}).get("cwd", "")
    roots = []
    try:
        rc, out, _ = t.run_helper([str(t.SCRIPT_DIR / "pj-repos.py"), "--slug", request["slug"]], cwd=cwd or None)
        repos = json.loads(out) if rc == 0 and out else []
        roots = [os.path.realpath(r["worktree"]) for r in repos if r.get("worktree")]
        parents = {str(Path(p).parent) for p in roots}
        if len(roots) > 1 and len(parents) == 1:
            roots.extend(parents)
    except (t.Fail, ValueError, TypeError, KeyError):
        pass  # Registration works without trust permission; unknown paths stay blocked.
    record(t, project, request, "registered", registered_at=time.time(),
           role=request["payload"]["role"], trusted_roots=roots, cwd=cwd,
           actions=0, resends=0)


def bind(t, project, request, workspace, surface, launcher):
    record(t, project, request, "attached", workspace=workspace, surface=surface, launcher=launcher)


def jobs(t, project):
    result = []
    directory = t.exchanges_dir(project)
    if not directory.exists():
        return result
    t.assert_no_symlinks(directory)
    for path in sorted(directory.glob("*/worker.jsonl")):
        rows = t.read_stream(path)
        requests = {e["event_id"]: e for e in rows if e["type"] == "review_requested"}
        states = {}
        for e in rows:
            if e["type"] == "review_watch":
                states[e["reply_to"]] = e["payload"]
        for event_id, state in states.items():
            request = requests.get(event_id)
            if request:
                result.append({**state, "request": request})
    return result


def started(t, project, request):
    role = request["payload"]["role"]
    return any(e.get("reply_to") == request["event_id"] and
               e["type"] in ("review_started", t.reviewer_def(role)["reply_type"])
               for e in t.read_stream(t.reviewer_stream(project, request["slug"], role)))


def classify(screen, trusted_roots):
    """Return only actions whose whole dialog can be identified. Unknown means no typing."""
    text = ANSI.sub("", screen)
    low = text.lower()
    # Grok's native TUI: empty boxed composer followed by its branded status border.
    # `always-approve` is an input-mode label there, not a pending approval dialog.
    grok_idle = bool(re.search(
        r"^\s*│\s*❯\s*│\s*\n\s*╰[─━]*\s*grok\s+[\w.-]+\s+\([^\n)]+\)[^\n]*╯\s*$",
        "\n".join(low.splitlines()[-8:]), re.MULTILINE))
    if grok_idle:
        low = re.sub(r"(?m)^(\s*╰[^\n]*\bgrok\b[^\n]*)\balways-approve\b",
                     r"\1automatic-mode", low)
    # A quoted/stale startup dialog must not override a current approval or busy indicator.
    if re.search(r"select login method|sign in|do you want to proceed|would you like to run|approve|allow this|permission required", low):
        return "blocked", []
    if re.search(r"esc to (?:interrupt|cancel|stop)|thinking|working…|working\.\.\.", low):
        return "busy", []
    # Exact selectable option text; an arbitrary 'proceed?' or tool approval is never accepted.
    options = []
    for line in text.splitlines():
        match = re.match(r"^\s*([❯›>▶]?)\s*(\d+)[.)]\s+(.+?)\s*$", line)
        if match:
            options.append((bool(match[1]), int(match[2]), match[3].strip()))
    selected = next((i for i, option in enumerate(options) if option[0]), None)
    def choose(predicate, kind):
        if selected is None:
            return None
        target = next((i for i, option in enumerate(options) if predicate(option[2].lower())), None)
        if target is None:
            return None
        distance = target - selected
        if abs(distance) > 4:
            return None
        return kind, (["down"] * distance if distance > 0 else ["up"] * -distance) + ["enter"]
    if "trust this folder" in low or "trust the files in this folder" in low:
        # Require a displayed standalone path matching the registered worktree exactly.
        displayed = [line.strip().removeprefix("Folder: ").removeprefix("Directory: ")
                     for line in text.splitlines()]
        if not any(p in displayed for p in trusted_roots):
            return "blocked", []
        return choose(lambda s: s in ("yes, i trust this folder", "yes, proceed"), "trust") or ("blocked", [])
    if re.search(r"update (?:available|now)|new version available", low):
        return choose(lambda s: s in ("skip", "skip for now", "skip this version", "not now",
                                     "later", "remind me later"), "update-skip") or ("blocked", [])
    # Only a startup welcome/update-complete acknowledgement. Not a generic command confirmation.
    if (re.search(r"(?:welcome to (?:claude code|codex|grok)|update (?:complete|completed|successful))", low)
            and re.search(r"press (?:enter|return) to continue", low)
            and not re.search(r"permission|approve|execute|run this|trust|login|sign in", low)):
        return "continue", ["enter"]
    if re.search(r"command not found|not found:|fatal error", low):
        return "launch-failed", []
    if grok_idle:
        return "ready", []
    # Codex's empty composer has its own placeholder; it need not show Claude shortcuts.
    # Match a complete prompt near the bottom, never a quoted phrase in review output.
    if re.search(r"^\s*[›❯]\s+ask codex to do anything\s*$",
                 "\n".join(low.splitlines()[-6:]), re.MULTILINE):
        return "ready", []
    if any(m in low for m in ("for shortcuts", "shift+tab to cycle", "bypass permissions", "accept edits", "? for help")):
        return "ready", []
    return "unknown", []


def screen_kind(t, screen):
    return classify(screen, [])[0]


def alive(t, job):
    """Allow pre-trust native CLI processes too, but never a shell with old TUI text."""
    ws, surface, launcher = job["workspace"], job["surface"], job.get("launcher") or ""
    try:
        t.validate_target(ws, surface, launcher)
        return True
    except t.Fail:
        pass
    _, panes = t.cmux_tree(ws)
    if not any(s.upper() == surface.upper() for rows in panes.values() for s, _ in rows):
        return False
    tree = t.run_cmux(["cmux", "tree", "--workspace", ws, "--id-format", "both"])
    ref = next((f"surface:{m.group(1)}" for m in t.SURFACE_LINE.finditer(tree)
                if m.group(2).upper() == surface.upper()), None)
    if not ref or not t.valid_launcher(launcher):
        return False
    expected = t.launcher_runtime(launcher)
    top = t.run_cmux(["cmux", "top", "--workspace", ws, "--processes", "--flat", "--format", "tsv"])
    for line in top.splitlines():
        cols = line.split("\t")
        if len(cols) < 7 or cols[3] != "process" or cols[5] != ref:
            continue
        try:
            words = shlex.split(cols[6])
        except ValueError:
            continue
        if words and Path(words[0]).name == expected:
            return True
    return False


def fail(t, project, request, reason):
    job = record(t, project, request, "failed", reason=reason)
    event = t.make_event("review_start_failed", "worker", "worker", request["slug"],
                         {"role": request["payload"]["role"], "reason": reason,
                          "workspace": job.get("workspace"), "surface": job.get("surface"),
                          "observed": job.get("observed"), "last_screen": job.get("last_screen"),
                          "resends": job.get("resends", 0), "actions": job.get("actions", 0),
                          "recovery": "자동 재요청을 중단하세요. 이 요청의 시작 감시는 종료됐습니다. "
                                      "같은 round라도 새 review.request로 재시도 한도를 초기화하지 마세요. "
                                      "원인과 실제 수정 내용을 기록한 뒤에만 복구 요청하세요."},
                         round_=request["round"], reply_to=request["event_id"])
    t.append_event(t.stream_path(project, request["slug"], "worker"), event)
    console = tab_attachment(t, project)
    if console:
        try:
            t.validate_target(console["workspace"], console["surface"], console["launcher"])
            t.send_wakeup(console["workspace"], console["surface"],
                          t.wakeup_line(project, request["slug"], "worker", event["event_id"]))
        except t.Fail as exc:
            record(t, project, request, "failed", console_notification_error=str(exc))
    # Also wake the exact requester; never substitute another worker/board surface.
    caller = request["payload"].get("requester") or {}
    try:
        if not caller.get("workspace") or not caller.get("surface"):
            raise t.Fail(3, "원래 요청자의 workspace/surface 기록이 없습니다")
        t.validate_target(caller.get("workspace", ""), caller.get("surface", ""))
        t.send_wakeup(caller["workspace"], caller["surface"],
                      t.wakeup_line(project, request["slug"], "worker", event["event_id"]))
    except t.Fail as exc:
        record(t, project, request, "failed", notification_error=str(exc))
    return job


@contextlib.contextmanager
def try_lock(t, key):
    with t.request_lock(key, blocking=False) as handle:
        yield handle


def tick(t, project, clock=None):
    now = time.time() if clock is None else clock
    for initial in jobs(t, project):
        if initial["state"] in TERMINAL:
            continue
        request = initial["request"]
        with try_lock(t, request["request_id"]) as lock:
            if lock is None:
                continue  # A launching tab never holds up another tab's poll.
            job = next(j for j in jobs(t, project) if j["request"]["event_id"] == request["event_id"])
            if job["state"] in TERMINAL:
                continue
            try:
                _tick(t, project, job, now)
            except (t.Fail, OSError, ValueError) as exc:
                fail(t, project, request, str(exc))


def _tick(t, project, job, now):
    request = job["request"]
    if started(t, project, request):
        record(t, project, request, "completed", reason="review started")
        return
    try:
        task = t.task_info(request["slug"])
    except t.Fail:
        record(t, project, request, "cancelled", reason="task no longer registered")
        return
    if task.get("status") in ("완료", "취소"):
        record(t, project, request, "cancelled", reason="task ended")
        return
    rows = t.read_stream(t.stream_path(project, request["slug"], "worker"))
    newer = next((e for e in reversed(rows) if e["type"] == "review_requested"
                  and e["payload"].get("role") == job["role"]), request)
    if newer["event_id"] != request["event_id"] or any(e["type"] == "review_skipped" and
            e["round"] == request["round"] and e["payload"].get("role") == job["role"] for e in rows):
        record(t, project, request, "cancelled", reason="superseded or skipped")
        return
    if now - job["registered_at"] >= TIMEOUT:
        fail(t, project, request, "리뷰 시작 확인 시간 초과 — 탭/확인창 또는 릴레이 상태를 확인하세요")
        return
    if not job.get("surface") or job["state"] == "attached":
        return
    if not alive(t, job):
        if now - job.get("sent_at", job["registered_at"]) < 15:
            return  # Give a freshly launched CLI time to register its process.
        fail(t, project, request, "리뷰어 프로세스 또는 탭이 사라졌습니다")
        return
    screen = t.run_cmux(["cmux", "read-screen", "--workspace", job["workspace"],
                         "--surface", job["surface"], "--lines", "35"])
    kind, keys = classify(screen, job.get("trusted_roots", []))
    if kind in ("unknown", "blocked") and screen != job.get("last_screen"):
        record(t, project, request, job["state"], last_screen=screen[-1500:], observed=kind)
    if kind == "launch-failed":
        fail(t, project, request, "리뷰어 런처 실행 실패")
        return
    if keys:
        fingerprint = hashlib.sha256(screen.encode()).hexdigest()
        if fingerprint == job.get("handled_screen"):
            return
        if job.get("actions", 0) >= MAX_ACTIONS:
            fail(t, project, request, "시작 확인창 처리 횟수 초과")
            return
        # Record intent first: a watcher crash must not repeatedly approve the same dialog.
        record(t, project, request, "handled", handled_screen=fingerprint,
               actions=job.get("actions", 0) + 1, needs_resend=True, handled_at=now, dialog=kind)
        for key in keys:
            t.run_cmux(["cmux", "send-key", "--workspace", job["workspace"],
                        "--surface", job["surface"], key])
        return
    if kind == "blocked":
        fail(t, project, request, "자동 처리 대상이 아닌 확인창 — 로그인/권한 또는 알 수 없는 선택지")
        return
    if kind != "ready":
        return
    # Give a CLI's initial prompt time to start; resends retain the event and round IDs.
    last_send = job.get("sent_at", job["registered_at"])
    if now - max(last_send, job.get("handled_at", 0)) < 10:
        return
    if job.get("resends", 0) >= MAX_RESENDS:
        fail(t, project, request, "리뷰 시작 응답 없음 — 동일 요청 재전달 횟수 초과")
        return
    if started(t, project, request):
        record(t, project, request, "completed", reason="review started")
        return
    record(t, project, request, "resent", resends=job.get("resends", 0) + 1,
           sent_at=now, needs_resend=False)
    t.send_wakeup(job["workspace"], job["surface"],
                  t.review_start_line(project, request["slug"], request))


def daemon_key(t, project):
    return "watcher-" + hashlib.sha256(f"{t.TASKS_DIR.resolve()}\0{project}".encode()).hexdigest()[:24]


def control(t, project, state):
    t.append_event(t.stream_path(project, None, "board"),
                   t.make_event("watcher_control", "board", "board", None,
                                {"state": state, "pid": os.getpid()}))


def running(t, project):
    with try_lock(t, daemon_key(t, project)) as lock:
        return lock is None


def ensure(t, project):
    if t.TEST:
        return
    with t.request_lock(daemon_key(t, project) + "-launch"):
        if running(t, project):
            return
        directory = t.exchanges_dir(project)
        t.assert_no_symlinks(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "watcher.log"
        t.assert_no_symlinks(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "ab") as log:
            info = os.fstat(log.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise t.Fail(3, "watcher log는 단일 링크 일반 파일이어야 합니다")
            process = subprocess.Popen([sys.executable, str(t.SCRIPT_DIR / "pj-cmux.py"),
                                        "watcher", "run", "--project", project],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if running(t, project):
                return
            if process.poll() is not None:
                break
            time.sleep(.05)
        raise t.Fail(6, f"watcher 시작 실패 — {path}")


def run(t, project):
    with try_lock(t, daemon_key(t, project)) as lock:
        if lock is None:
            return 0
        control(t, project, "running")
        while True:
            controls = [e for e in t.read_stream(t.stream_path(project, None, "board"))
                        if e["type"] == "watcher_control"]
            if controls and controls[-1]["payload"]["state"] == "stop":
                return 0
            tick(t, project)
            time.sleep(INTERVAL)


def command(t, args):
    if not t.SLUG_RE.fullmatch(args.project or ""):
        raise t.Fail(1, "--project 가 필요합니다")
    # Thread identity may also be inherited by a trusted external hook. Only the explicit
    # sandbox marker denotes this execution boundary; never unset it to launch a monitor.
    if args.mode in ("start", "run", "tick") and not t.TEST and os.environ.get("CODEX_SANDBOX"):
        raise t.Fail(3, "sandbox에서는 watcher를 직접 실행하지 않습니다. request watcher.start와 허용된 relay를 사용하세요")
    if args.mode == "run":
        return run(t, args.project)
    if args.mode == "start":
        ensure(t, args.project)
    elif args.mode == "stop":
        control(t, args.project, "stop")
    elif args.mode == "tick":
        tick(t, args.project)
    rows = [{k: v for k, v in j.items() if k != "request"} |
            {"slug": j["request"]["slug"], "event_id": j["request"]["event_id"]}
            for j in jobs(t, args.project)]
    print(json.dumps({"project": args.project, "running": running(t, args.project),
                      "interval_seconds": INTERVAL,
                      "tab": tab_attachment(t, args.project), "jobs": rows}, ensure_ascii=False, indent=2))
    return 0
