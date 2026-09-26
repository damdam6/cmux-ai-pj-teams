#!/usr/bin/env python3
"""night-queue.py — the queue and the watch for one pj night run.

Owns ``raw/tasks/{project}/night.json`` and its ``night.lock`` sidecar, and reads everything else through
the scripts that already own it: ``pj-tasks.py`` for the task lists and ``pj-cmux.py`` for the
exchange streams. It writes neither, and it never talks to cmux. A night run must not become a
second writer of the task lists — an unlocked second writer does not produce a conflict, it
produces a silently lost line (see pj/SKILL.md anti-patterns).

Tasks run concurrently up to `parallel`; the merges do not. Nothing in the transport
serializes tasks — its locks are per-request and per-(project, slug) — and the board session
that consumes done reports is single, so merges queue behind each other on their own. What
concurrency costs is that two tasks cut from the same base can touch the same files, and the
second merge then conflicts; pj-wrap leaves that task 검토 대기 for the morning.

Subcommands
  plan    build the run's queue and open night.json
  next    fill a free slot, or say why it cannot — busy, waiting on deps, or done
  record  the outcome of one task
  watch   block until ONE in-flight task reports done, goes quiet, or runs out of time
  check   recheck the active run and processed reviews immediately before an automatic merge
  report  the morning summary
  stop    end the run
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import functools
import json
import os
import pathlib
import re
import secrets
import stat
import subprocess
import sys
import time
import uuid

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "_shared" / "scripts"))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()


# Storage root is configurable; see README.md.
VAULT = pathlib.Path(os.environ.get("PJ_VAULT")
                     or pathlib.Path.home() / ".local" / "share" / "pj")
TASKS_DIR = VAULT / "raw" / "tasks"
SHARED = pathlib.Path(__file__).resolve().parents[2] / "_shared" / "scripts"
PJ_TASKS = SHARED / "pj-tasks.py"
PJ_CMUX = SHARED / "pj-cmux.py"

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

DONE = "완료"
TODO = "할 일"
OUTCOMES = ("merged", "failed", "skipped")

# A task that has said nothing for this long has either finished without reporting or given up
# mid-round; either way the loop must move on rather than hold the night on one task.
# Matches the concurrency the day-time flow already sustains in one project; --parallel 1
# restores the strictly sequential run, where each task branches off the previous merge.
PARALLEL_DEFAULT = 3
QUIET_DEFAULT = 1800
TIMEOUT_DEFAULT = 7200
POLL_INTERVAL = 20


class Fail(RuntimeError):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_ts(s: str):
    """Any ISO instant these two clocks produce, as an aware datetime.

    pj-cmux.py stamps events UTC with a Z ("2026-09-08T15:41:05Z"); this file stamps its own
    state local with an offset ("2026-09-09T00:41:05+09:00"). Those are the same moment and
    compare backwards as strings, so every comparison goes through here instead.
    """
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def run_json(script: pathlib.Path, *args: str):
    proc = subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise Fail(proc.returncode or 1,
                   f"{script.name} {' '.join(args)} 실패: "
                   f"{(proc.stderr or proc.stdout).strip()[:400]}")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise Fail(4, f"{script.name} 가 JSON 이 아닌 것을 냈습니다: {proc.stdout[:200]}")


# ---------- state ----------

@contextlib.contextmanager
def project_dir(project: str):
    """Anchor file operations inside a real project directory, never a project symlink.

    TASKS_DIR is the user's trusted configured storage root. dir_fd keeps an opened
    project stable even if its path is renamed while an operation is in progress.
    """
    if not PROJECT_RE.fullmatch(project or ""):
        raise Fail(1, f"프로젝트 slug 형식이 잘못됐습니다: {project!r}")
    root_fd = fd = None
    try:
        root_fd = os.open(TASKS_DIR, os.O_RDONLY | os.O_DIRECTORY)
        fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=root_fd)
        yield fd
    except OSError as exc:
        raise Fail(1, f"프로젝트 상태 파일 접근 거부: {project} ({exc})") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if root_fd is not None:
            os.close(root_fd)


def state_path(project: str) -> pathlib.Path:
    with project_dir(project):
        return TASKS_DIR / project / "night.json"


def open_regular(directory: int, name: str, flags: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise Fail(1, f"일반 단일 링크 파일만 사용할 수 있습니다: {name}")
    return fd


def load_state(project: str) -> dict:
    with project_dir(project) as directory:
        try:
            fd = open_regular(directory, "night.json", os.O_RDONLY)
        except FileNotFoundError:
            raise Fail(5, "진행 중인 밤 실행이 없습니다 — night-queue.py plan 이 먼저입니다")
        with os.fdopen(fd, encoding="utf-8") as fh:
            return json.load(fh)


def save_state(project: str, st: dict) -> None:
    with project_dir(project) as directory:
        # Refuse existing symlinks/hardlinks/special files before replacing state.
        try:
            existing = open_regular(directory, "night.json", os.O_RDONLY)
        except FileNotFoundError:
            pass
        else:
            os.close(existing)
        temporary = f".night-{secrets.token_hex(16)}.tmp"
        fd = open_regular(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(st, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, "night.json", src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


@contextlib.contextmanager
def state_lock(project: str):
    # Lock a stable sidecar, not night.json: save_state replaces that file's inode.
    # Keep the sidecar after unlocking so waiting processes all use the same lock.
    with project_dir(project) as directory:
        # Separate existing-file open from exclusive creation. Concurrent first opens
        # with O_CREAT can surface ENOENT on some filesystems; neither path may follow
        # a link or truncate an existing lock, and retries stay bounded.
        for attempt in range(3):
            try:
                fd = open_regular(directory, "night.lock", os.O_RDWR)
                break
            except FileNotFoundError:
                try:
                    fd = open_regular(directory, "night.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL)
                    break
                except (FileExistsError, FileNotFoundError):
                    if attempt == 2:
                        raise
        with os.fdopen(fd, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def locked_state(fn):
    """Serialize the entire read/modify/write operation, including task lookups.

    Readers see atomic snapshots via os.replace. In particular, watch must not hold this
    lock while waiting for events: record/next/stop need to keep progressing meanwhile.
    """
    @functools.wraps(fn)
    def locked(a):
        with state_lock(a.project):
            return fn(a)
    return locked


# ---------- task list reads ----------

def all_tasks(project: str) -> list[dict]:
    rows = run_json(PJ_TASKS, "list", "--project", project)
    if not isinstance(rows, list):
        raise Fail(4, "pj-tasks.py list 가 배열을 내지 않았습니다")
    return rows


def task_status(slug: str) -> str | None:
    try:
        return run_json(PJ_TASKS, "get", "--slug", slug).get("status")
    except Fail:
        return None


# ---------- plan ----------

@locked_state
def op_plan(a) -> int:
    project = a.project
    # Before anything else, so a mistyped project reads as a mistyped project. Reached only
    # through save_state it would surface as "할 일 인 작업이 없습니다", and the user would go
    # to sleep believing the queue was empty.
    if state_path(project).exists() and load_state(project).get("state") == "running":
        raise Fail(3, f"{project} 의 밤 실행이 이미 running 입니다 — "
                      "next 로 이어가거나 stop 으로 종료한 뒤 새로 plan 하세요")
    rows = all_tasks(project)
    by_slug = {r["slug"]: r for r in rows}

    if a.slug:
        queue = []
        for s in a.slug:
            if not SLUG_RE.match(s):
                raise Fail(1, f"slug 형식이 잘못됐습니다: {s!r}")
            if s not in by_slug:
                raise Fail(1, f"{project} 의 목록에 없는 slug 입니다: {s}")
            if by_slug[s]["status"] != TODO:
                raise Fail(1, f"{s} 는 '{by_slug[s]['status']}' 입니다 — "
                              f"밤 큐에는 '{TODO}' 인 것만 넣습니다")
            queue.append(s)
    else:
        queue = [r["slug"] for r in rows if r["status"] == TODO]

    if a.max is not None:
        if a.max < 1:
            raise Fail(1, "--max 는 1 이상입니다")
        queue = queue[:a.max]
    if not queue:
        raise Fail(5, f"{project} 에 '{TODO}' 인 작업이 없습니다 — 돌릴 것이 없습니다")

    # A dep inside this queue only orders the run — the dependent waits for it and the slot
    # goes to something else meanwhile. A dep OUTSIDE it that is not 완료 never will be
    # tonight, so that dependent is dead on arrival; say so while the user can still refile.
    in_queue = set(queue)
    warnings = []
    for s in queue:
        for dep in by_slug[s].get("deps", []):
            if dep in in_queue:
                continue
            if (by_slug.get(dep, {}).get("status") or task_status(dep)) != DONE:
                warnings.append(f"{s} 의 선행 {dep} 가 완료가 아니고 이번 큐에도 없습니다 — "
                                f"{s} 는 건너뛰게 됩니다")

    if a.parallel < 1:
        raise Fail(1, "--parallel 은 1 이상입니다")
    st = {"project": project, "run_id": uuid.uuid4().hex,
          "started_at": now_iso(), "state": "running",
          "attempts": a.attempts, "parallel": a.parallel, "queue": queue,
          "results": {}, "inflight": {}}
    save_state(project, st)

    print(f"PJ_NIGHT=planned project={project} count={len(queue)} "
          f"parallel={a.parallel} attempts={a.attempts}")
    for i, s in enumerate(queue, 1):
        print(f"  {i}. {s} — {by_slug[s].get('title', '')}")
    for w in warnings:
        print(f"WARN {w}")
    return 0


# ---------- next ----------

@locked_state
def op_next(a) -> int:
    st = load_state(a.project)
    if st.get("state") != "running":
        print(f"PJ_NIGHT=stopped project={a.project}")
        return 0

    inflight, results = st["inflight"], st["results"]
    if len(inflight) >= st["parallel"]:
        print(f"PJ_NIGHT=busy inflight={','.join(inflight)} limit={st['parallel']}")
        return 0

    in_queue, held = set(st["queue"]), []
    for slug in st["queue"]:
        if slug in results or slug in inflight:
            continue
        info = run_json(PJ_TASKS, "get", "--slug", slug)
        if info.get("status") != TODO:
            results[slug] = {"outcome": "skipped", "at": now_iso(),
                             "reason": f"착수 전 상태 변경: {info.get('status')}",
                             "status": info.get("status")}
            save_state(a.project, st)
            print(f"PJ_NIGHT=skipped slug={slug} reason=status:{info.get('status')}")
            continue
        unmet = [d for d in info.get("deps", []) if task_status(d) != DONE]
        if unmet:
            # A dep still live in this run — queued or in flight — becomes 완료 later tonight,
            # so this task waits and the slot goes to something else. A dep that failed, was
            # skipped, or sits outside the queue never will be; waiting on it burns the night.
            coming = [d for d in unmet if d in in_queue and d not in results]
            if coming:
                held.append(slug)
                continue
            results[slug] = {"outcome": "skipped", "at": now_iso(),
                             "reason": f"선행 미완료: {', '.join(unmet)}"}
            save_state(a.project, st)
            print(f"PJ_NIGHT=skipped slug={slug} reason=deps:{','.join(unmet)}")
            continue
        inflight[slug] = now_iso()
        save_state(a.project, st)
        print(f"PJ_NIGHT=next slug={slug} since={inflight[slug]} "
              f"inflight={len(inflight)}/{st['parallel']}")
        return 0

    if inflight or held:
        print(f"PJ_NIGHT=waiting inflight={','.join(inflight) or '-'} "
              f"held={','.join(held) or '-'}")
        return 0

    st["state"] = "finished"
    st["finished_at"] = now_iso()
    save_state(a.project, st)
    print(f"PJ_NIGHT=done project={a.project}")
    return 0


# ---------- record ----------

@locked_state
def op_record(a) -> int:
    st = load_state(a.project)
    if a.outcome not in OUTCOMES:
        raise Fail(1, f"--outcome 은 {'|'.join(OUTCOMES)} 입니다")
    if a.slug not in st["queue"]:
        raise Fail(1, f"이번 밤 큐에 없는 slug 입니다: {a.slug}")
    st["results"][a.slug] = {"outcome": a.outcome, "at": now_iso(),
                             "reason": a.reason or "", "status": task_status(a.slug)}
    st["inflight"].pop(a.slug, None)
    save_state(a.project, st)
    print(f"PJ_NIGHT=recorded slug={a.slug} outcome={a.outcome}")
    return 0


# ---------- watch ----------

def events_for(slug: str) -> list[dict]:
    rows = run_json(PJ_CMUX, "event", "list", "--slug", slug)
    return rows if isinstance(rows, list) else []


@locked_state
def op_check(a) -> int:
    st = load_state(a.project)
    if st.get("state") != "running" or a.slug not in st.get("inflight", {}):
        raise Fail(3, "머지 검사 거부: 현재 실행 중인 night 작업이 아닙니다")
    result = run_json(PJ_CMUX, "night-check", "--slug", a.slug)
    if result.get("project") != a.project:
        raise Fail(3, "머지 검사 거부: 작업의 프로젝트가 다릅니다")
    print(f"PJ_NIGHT=ready slug={a.slug} roles={','.join(result['roles'])}")
    return 0


def op_watch(a) -> int:
    """Block until ONE in-flight task resolves. The caller records it, refills the slot with
    `next`, and watches again — so a verdict always names the single task it is about."""
    override_since = parse_ts(a.since) if a.since is not None else None
    if a.since is not None and override_since is None:
        raise Fail(1, f"--since 는 ISO 시각이어야 합니다: {a.since!r}")
    st = load_state(a.project)
    generation = (st.get("run_id"), st.get("started_at"))
    targets = {s: v for s, v in st["inflight"].items() if not a.slug or s == a.slug}

    def refresh_targets():
        with state_lock(a.project):
            current = load_state(a.project)
            if (current.get("state") != "running"
                    or (current.get("run_id"), current.get("started_at")) != generation):
                print(f"PJ_NIGHT=stopped project={a.project}")
                return False
            for slug, started in list(targets.items()):
                if current.get("inflight", {}).get(slug) != started or slug in current.get("results", {}):
                    targets.pop(slug)
            if not targets:
                print("PJ_NIGHT=idle inflight=0")
                return False
            return True

    if not refresh_targets():
        return 0

    since = {s: override_since if override_since is not None else parse_ts(v or "")
             for s, v in targets.items()}
    deadline = time.monotonic() + a.timeout
    last_seen = {s: time.monotonic() for s in targets}
    seen_ids: dict[str, set[str]] = {s: set() for s in targets}

    while True:
        if not refresh_targets():
            return 0
        for slug in list(targets):
            if slug not in targets:
                continue
            try:
                evs = events_for(slug)
            except Fail as e:
                if not refresh_targets():
                    return 0
                if slug not in targets:
                    continue
                print(f"PJ_NIGHT=error slug={slug} detail={e}")
                return 0
            if not refresh_targets():
                return 0
            if slug not in targets:
                continue
            base = since[slug]
            fresh = [e for e in evs
                     if base is None
                     or (parse_ts(e.get("created_at") or "") or base) >= base]
            report = next((e for e in fresh if e.get("type") == "done_report"), None)
            if report:
                try:
                    run_json(PJ_CMUX, "night-check", "--slug", slug)
                except Fail as e:
                    if not refresh_targets():
                        return 0
                    if slug not in targets:
                        continue
                    print(f"PJ_NIGHT=blocked slug={slug} reason={e}")
                    return 0
                if not refresh_targets():
                    return 0
                if slug not in targets:
                    continue
                pl = report.get("payload") or {}
                print(f"PJ_NIGHT=report slug={slug} event={report['event_id']} "
                      f"commit={pl.get('commit')} typecheck={pl.get('typecheck')}")
                return 0
            # IDs are random UUIDs, so a lexicographic maximum is not the newest event.
            ids = {e["event_id"] for e in fresh if e.get("event_id")}
            if ids - seen_ids[slug]:
                last_seen[slug] = time.monotonic()
                seen_ids[slug].update(ids)

        if not refresh_targets():
            return 0
        now = time.monotonic()
        for slug in targets:
            if now - last_seen[slug] >= a.quiet:
                print(f"PJ_NIGHT=quiet slug={slug} for={int(now - last_seen[slug])}s")
                return 0
        if now >= deadline:
            print(f"PJ_NIGHT=timeout slugs={','.join(targets)} after={a.timeout}s")
            return 0
        time.sleep(min(POLL_INTERVAL, max(1, deadline - now)))


# ---------- report / stop ----------

def op_report(a) -> int:
    st = load_state(a.project)
    res = st["results"]
    order = {"merged": [], "failed": [], "skipped": [], "미실행": []}
    for slug in st["queue"]:
        r = res.get(slug)
        order[r["outcome"] if r else "미실행"].append((slug, (r or {}).get("reason", "")))

    print(f"PJ_NIGHT=report project={st['project']} state={st['state']} "
          f"started={st['started_at']} parallel={st.get('parallel')} "
          f"attempts={st.get('attempts')}")
    if st.get("inflight"):
        print(f"진행 중 ({len(st['inflight'])}) — {', '.join(st['inflight'])}")
    for key, label in (("merged", "머지됨"), ("failed", "실패"),
                       ("skipped", "건너뜀"), ("미실행", "미실행")):
        rows = order[key]
        print(f"{label} ({len(rows)})")
        for slug, reason in rows:
            print(f"  - {slug}{' — ' + reason if reason else ''}")
    return 0


@locked_state
def op_stop(a) -> int:
    st = load_state(a.project)
    st["state"] = "stopped"
    st["finished_at"] = now_iso()
    left = list(st.get("inflight") or {})
    st["inflight"] = {}
    save_state(a.project, st)
    print(f"PJ_NIGHT=stopped project={a.project} "
          f"still-running={','.join(left) or '-'}")
    return 0


OPS = {"plan": op_plan, "next": op_next, "record": op_record,
       "watch": op_watch, "check": op_check, "report": op_report, "stop": op_stop}


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("op", choices=list(OPS))
    ap.add_argument("--project", required=True)
    ap.add_argument("--slug", action="append")
    ap.add_argument("--max", type=int, help="plan: 큐 길이 상한")
    ap.add_argument("--attempts", type=int, default=3,
                    help="plan: 태스크 세션이 스스로 복구를 시도하는 최대 횟수 (기본 3)")
    ap.add_argument("--parallel", type=int, default=PARALLEL_DEFAULT,
                    help=f"plan: 동시에 돌릴 태스크 수 (기본 {PARALLEL_DEFAULT}, 1 이면 순차)")
    ap.add_argument("--outcome", help=f"record: {'|'.join(OUTCOMES)}")
    ap.add_argument("--reason", help="record: 실패·건너뜀 사유")
    ap.add_argument("--since", help="watch: 이 시각 이후 이벤트만 본다 (기본: 그 태스크의 착수 시각)")
    ap.add_argument("--quiet", type=int, default=QUIET_DEFAULT,
                    help=f"watch: 이만큼 새 이벤트가 없으면 멈춘 것으로 본다 (기본 {QUIET_DEFAULT}s)")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_DEFAULT,
                    help=f"watch: 전체 상한 (기본 {TIMEOUT_DEFAULT}s)")
    a = ap.parse_args()

    if a.op != "plan" and a.slug and len(a.slug) > 1:
        raise SystemExit("--slug 는 plan 에서만 여러 번 줄 수 있습니다")
    if a.op in ("record", "check") and not a.slug:
        raise SystemExit(f"{a.op} 는 --slug 가 필요합니다")
    if a.op in ("record", "watch", "check"):
        # watch with no --slug watches every in-flight task at once
        a.slug = a.slug[0] if a.slug else None
    if a.op == "record" and not a.outcome:
        raise SystemExit("record 는 --outcome 이 필요합니다")

    try:
        return OPS[a.op](a)
    except Fail as e:
        print(str(e), file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
