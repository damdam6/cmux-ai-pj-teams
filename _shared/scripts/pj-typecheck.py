#!/usr/bin/env python3
"""Optional task-worktree type checker, invoked only when requested or required by a repo.

PJ completion, review and integration do not automatically call this helper.
Use --changed-only for the bundled Node checker, which loads the repository's TypeScript
and tsconfig and diagnoses only files changed against the task's project branch.
A typecheckChangedCmd argv array may override that checker for another toolchain.
This mode never falls back to the legacy whole-project typecheckCmd.

Without --changed-only, the legacy explicit invocation executes typecheckCmd and classifies
its diagnostics by changed paths. That compatibility mode is not part of the PJ workflow.
Each invocation checks one repository; --repo disambiguates multi-repository tasks.

Exit/result: 0/ok = passed, 1/fail = reported errors, 2/unparsed = could not complete the check,
3/none = no applicable changed files (scoped mode) or no configured command (legacy mode).
Results describe the check only; they do not independently gate PJ completion or integration.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from pathlib import PurePosixPath

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()


TASKS = pathlib.Path(__file__).resolve().parent / "pj-tasks.py"
REPOS = pathlib.Path(__file__).resolve().parent / "pj-repos.py"
ALIASES = _aliases_path()

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Both tsc layouts: `file.ts(12,5): error TS2345:` and pretty `file.ts:12:5 - error TS2345:`.
ERROR_AT = re.compile(
    r"^\s*(?P<file>\S[^(]*?)"
    r"(?:\((?P<rc1>\d+,\d+)\)|:(?P<rc2>\d+:\d+))"
    r"\s*[-:]?\s*error\s+(?P<code>[A-Z]+\d+)\b")
# A file-less error line — a config or CLI failure, which is nobody's file and everybody's problem.
ERROR_GLOBAL = re.compile(r"^\s*error\s+(?P<code>[A-Z]+\d+)\b")


def git(*args: str) -> str:
    try:
        p = subprocess.run(["git", *args], capture_output=True, text=True)
    except OSError:
        return ""
    return p.stdout if p.returncode == 0 else ""


def task_info(slug: str) -> dict | None:
    try:
        p = subprocess.run([sys.executable, str(TASKS), "get", "--slug", slug],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout)
    except (OSError, ValueError):
        return None


def items_of(value: str) -> list[str]:
    """One comma field -> its elements. `repo` and `project_branch` are lists paired
    positionally, and a single value is a one-element list."""
    return [s for s in (value or "").split(",") if s]


def target_repos(slug: str) -> list[dict]:
    """The repos this task targets, each with its branch and worktree. Empty on failure, which
    the caller reports rather than guessing around."""
    try:
        p = subprocess.run([sys.executable, str(REPOS), "--slug", slug],
                           capture_output=True, text=True)
        return json.loads(p.stdout or "[]")
    except (OSError, ValueError):
        return []


def pick_repo(slug: str, info: dict, wanted: str | None) -> tuple[str, str, str]:
    """(repo, project branch, worktree) for the one repo this run checks.

    A repo named explicitly wins. Otherwise the task must target exactly one — several with no
    `--repo` is ambiguous, and picking one would silently check half the task and report `ok`.
    """
    repos = items_of(info.get("repos")) or items_of(info.get("repo"))
    if wanted:
        if repos and wanted not in repos:
            raise ValueError(f"{slug} 은(는) {wanted} 를 타깃하지 않습니다 "
                             f"(타깃: {', '.join(repos)})")
        repo = wanted
    elif len(repos) == 1:
        repo = repos[0]
    elif not repos:
        raise ValueError(f"{slug} 의 프로젝트에 repo 가 기록돼 있지 않습니다")
    else:
        raise ValueError(f"{slug} 은(는) repo 를 {len(repos)}개 타깃합니다 "
                         f"({', '.join(repos)}) — --repo 로 하나를 지정하세요")

    # The project branch is paired positionally with the PROJECT's repo list, not the task's
    # subset, so the index has to come from the project's own ordering.
    proj_repos = items_of(info.get("repo"))
    bases = items_of(info.get("project_branch"))
    base = ""
    if repo in proj_repos:
        i = proj_repos.index(repo)
        base = bases[i] if i < len(bases) else (bases[0] if len(bases) == 1 else "")
    elif len(bases) == 1:
        base = bases[0]

    worktree = next((e["worktree"] for e in target_repos(slug)
                     if e["repo"] == repo and e.get("worktree")), "")
    return repo, base, worktree


def typecheck_cmd(repo: str) -> str | None:
    """The repo's `typecheckCmd`, or None. A string alias value is the legacy `short`-only form
    and carries no commands."""
    try:
        data = json.loads(ALIASES.resolve().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = (data.get("aliases") or {}).get(repo)
    if not isinstance(entry, dict):
        return None
    return (entry.get("typecheckCmd") or "").strip() or None


def changed_files(base: str, root: str) -> list[str]:
    """Files this branch brings to `base`. Run in `root` explicitly: with a combined task the
    process cwd is the parent folder, which is not a repo at all. Deletions are dropped — tsc
    cannot report on a file that is gone, and keeping them would only widen the suffix
    matching."""
    out = git("-C", root, "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def run_changed_only(repo: str, base: str, root: str) -> int:
    """Run the bundled file-scoped checker (or explicit override), never typecheckCmd."""
    try:
        diff = subprocess.run(
            ["git", "-C", root, "diff", "--name-only", "-z", "--diff-filter=d",
             f"{base}...HEAD", "--"], capture_output=True, text=True)
        if diff.returncode:
            raise ValueError(diff.stderr.strip() or "변경 파일 조회 실패")
        files = [name for name in diff.stdout.split("\0") if name]
        if not files:
            print(f"PJ_TYPECHECK=none repo={repo} scope=changed reason=검사할 변경 파일 없음")
            return 3
        data = json.loads(ALIASES.resolve().read_text(encoding="utf-8")) if ALIASES.exists() else {}
        entry = (data.get("aliases") or {}).get(repo, {})
        cmd = entry.get("typecheckChangedCmd") if isinstance(entry, dict) else None
        if not cmd:
            helper = pathlib.Path(__file__).with_name("pj-typecheck-changed.cjs")
            result = subprocess.run(["node", str(helper)], cwd=root, capture_output=True,
                                    text=True, input=json.dumps({"root": root, "files": files}))
            receipt = json.loads(result.stdout)
            verdict = receipt["status"]
            codes = {"ok": 0, "fail": 1, "unparsed": 2, "none": 3}
            if verdict not in codes or result.returncode != codes[verdict]:
                raise ValueError("변경 파일 검사기의 결과가 종료 코드와 일치하지 않음")
            print(f"PJ_TYPECHECK={verdict} repo={repo} scope=changed "
                  f"changed={len(files)} checked={len(receipt.get('checked', []))}")
            print(result.stdout.strip())
            return codes[verdict]
        if (not isinstance(cmd, list) or not cmd or
                not all(isinstance(arg, str) and arg for arg in cmd) or
                cmd.count("{files}") != 1 or cmd[0] == "{files}"):
            raise ValueError("typecheckChangedCmd는 독립된 {files} 인자가 하나 있는 argv 배열이어야 함")
        # Prefix with ./ so a filename beginning with '-' cannot become a checker option.
        argv = [value for arg in cmd for value in
                (["./" + name for name in files] if arg == "{files}" else [arg])]
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True)
    except (OSError, ValueError) as exc:
        print(f"PJ_TYPECHECK=unparsed repo={repo} scope=changed reason={exc}")
        return 2
    verdict = "ok" if result.returncode == 0 else "fail"
    print(f"PJ_TYPECHECK={verdict} repo={repo} scope=changed "
          f"changed={len(files)} exit={result.returncode}")
    show("CHANGED FILES", files, 50)
    show("SCOPED CHECK OUTPUT", (result.stdout + "\n" + result.stderr).strip().splitlines(), 50)
    return 0 if result.returncode == 0 else 1


def parse_errors(output: str) -> tuple[list[tuple[str, str]], list[str]]:
    """→ ([(file or "", full line)], [lines with no file]). One entry per error line; tsc's
    following context lines carry no `error TSxxxx` and are skipped."""
    attributed: list[tuple[str, str]] = []
    for raw in output.splitlines():
        line = ANSI.sub("", raw).rstrip()
        m = ERROR_AT.match(line)
        if m:
            attributed.append((m.group("file").strip(), line.strip()))
            continue
        if ERROR_GLOBAL.match(line):
            attributed.append(("", line.strip()))
    return attributed, [ln for f, ln in attributed if not f]


def touches(err_path: str, changed: list[str]) -> bool:
    """Trailing-component comparison — see the module docstring on monorepo path roots."""
    e = PurePosixPath(err_path.replace("\\", "/")).parts
    if not e:
        return False
    for c in changed:
        cp = PurePosixPath(c).parts
        n = min(len(e), len(cp))
        if n and e[-n:] == cp[-n:]:
            return True
    return False


def show(title: str, lines: list[str], cap: int) -> None:
    if not lines:
        return
    print(f"--- {title} ({len(lines)}) ---")
    for ln in lines[:cap]:
        print(ln)
    if len(lines) > cap:
        print(f"... +{len(lines) - cap} more")


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--slug", required=True, help="대상 작업 slug")
    ap.add_argument("--repo", help="이 작업이 여러 repo 를 타깃할 때 검사할 repo 하나")
    ap.add_argument("--changed-only", action="store_true", help="변경 파일 전용 명령만 실행 (pj-done 필수)")
    a = ap.parse_args()

    slug = a.slug.strip()
    info = task_info(slug)
    if info is None:
        print(f"PJ_TYPECHECK=unparsed reason={slug} 이(가) 작업 목록에 없음")
        return 2

    try:
        repo, base, worktree = pick_repo(slug, info, (a.repo or "").strip() or None)
    except ValueError as e:
        print(f"PJ_TYPECHECK=unparsed reason={e}")
        return 2

    # cwd wins when it is itself a worktree: a run from inside one must check THAT checkout,
    # not whichever the registry names first. From a combined task's parent folder cwd is not a
    # repo at all, and then the resolved per-repo worktree is the only answer.
    root = git("rev-parse", "--show-toplevel").strip() or worktree
    if not root:
        print(f"PJ_TYPECHECK=unparsed repo={repo} "
              f"reason={repo} 의 워크트리를 찾지 못했습니다")
        return 2

    if a.changed_only:
        if not base:
            print(f"PJ_TYPECHECK=unparsed repo={repo} reason=변경 범위를 구할 프로젝트 브랜치 없음")
            return 2
        return run_changed_only(repo, base, root)

    cmd = typecheck_cmd(repo) if repo else None
    if not cmd:
        print(f"PJ_TYPECHECK=none repo={repo} reason=repo-aliases.json 에 typecheckCmd 없음")
        return 3
    if not base:
        print(f"PJ_TYPECHECK=unparsed repo={repo} "
              f"reason={info.get('project', '')} 프로젝트에 {repo} 의 브랜치가 없어 비교 "
              f"기준이 없음")
        return 2

    changed = changed_files(base, root)
    p = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True)
    attributed, global_errs = parse_errors(p.stdout + "\n" + p.stderr)

    if p.returncode != 0 and not attributed:
        tail = (p.stdout + "\n" + p.stderr).strip().splitlines()[-20:]
        print(f"PJ_TYPECHECK=unparsed repo={repo} base={base} exit={p.returncode} "
              f"cmd={cmd} reason=에러 라인을 못 찾음 — 타입체크가 실행되지 않았음")
        show("OUTPUT TAIL", tail, 20)
        return 2

    gating = [ln for f, ln in attributed if not f or touches(f, changed)]
    preexisting = [ln for f, ln in attributed if f and not touches(f, changed)]

    verdict = "fail" if gating else "ok"
    print(f"PJ_TYPECHECK={verdict} repo={repo} base={base} exit={p.returncode} "
          f"changed={len(changed)} errors={len(attributed)} gating={len(gating)} "
          f"preexisting={len(preexisting)} global={len(global_errs)}")
    show("GATING — 이 브랜치가 건드린 파일", gating, 50)
    show("PRE-EXISTING — 이 브랜치와 무관", preexisting, 5)
    return 1 if gating else 0


if __name__ == "__main__":
    sys.exit(main())
