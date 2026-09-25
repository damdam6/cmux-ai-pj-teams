#!/usr/bin/env python3
"""Rebase both worktrees in the current PJ combo workspace; emit per-repo JSON."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

SHARED = Path(__file__).resolve().parents[2] / "_shared/scripts"
STATES = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
          "REVERT_HEAD", "sequencer")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"},
        stdin=subprocess.DEVNULL,
    )


def checked(repo, *args):
    result = git(repo, *args)
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "Git 조회 실패")
    return result.stdout.strip()


def discover(cwd):
    sys.path.insert(0, str(SHARED))
    spec = importlib.util.spec_from_file_location("pj_rebase_repos", SHARED / "pj-repos.py")
    repos = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repos)
    cwd = Path(cwd).resolve()
    top = git(cwd, "rev-parse", "--show-toplevel")
    root = Path(top.stdout.strip()).parent if top.returncode == 0 else cwd
    entries, reason = repos.from_cwd(str(root))
    alias_path = repos._aliases_path()
    aliases = json.loads(alias_path.read_text()).get("aliases", {}) if alias_path.exists() else {}
    found = {}
    for entry in entries:
        path = Path(entry["worktree"]).resolve()
        if path.parent != root:
            continue
        config = aliases.get(entry["repo"], {})
        labels = {path.name.lower(), str(config.get("short", "")).lower(),
                  str(config.get("wtFolder", "")).lower()}
        roles = [role for role, names in (("FE", {"frontend", "fe"}),
                                         ("BE", {"backend", "be"})) if labels & names]
        if len(roles) > 1 or (roles and roles[0] in found):
            raise ValueError("FE/BE 경로가 모호합니다. 저장소 alias 설정을 확인하세요.")
        if roles:
            found[roles[0]] = path
    if set(found) != {"FE", "BE"}:
        raise ValueError(reason or "frontend와 backend가 있는 콤보 루트에서 실행하세요.")
    common = [checked(found[role], "rev-parse", "--path-format=absolute", "--git-common-dir")
              for role in ("FE", "BE")]
    if Path(common[0]).resolve() == Path(common[1]).resolve():
        raise ValueError("FE/BE는 서로 다른 Git 저장소여야 합니다.")
    return found


def operation_state(repo):
    return [name for name in STATES if Path(checked(
        repo, "rev-parse", "--path-format=absolute", "--git-path", name)).exists()]


def conflicts(repo):
    result = git(repo, "diff", "--name-only", "--diff-filter=U", "-z")
    if result.returncode:
        raise ValueError(result.stderr.strip() or "충돌 파일 조회 실패")
    return [name for name in result.stdout.split("\0") if name]


def target_ref(repo, branch):
    candidates = ([branch] if branch.startswith(("refs/heads/", "refs/remotes/"))
                  else ["refs/heads/" + branch, "refs/remotes/" + branch])
    present = [ref for ref in candidates if git(repo, "show-ref", "--verify", "--quiet", ref).returncode == 0]
    if len(present) != 1:
        raise ValueError("대상 브랜치 없음" if not present else "대상 브랜치 이름이 모호함")
    checked(repo, "rev-parse", "--verify", present[0] + "^{commit}")
    return present[0]


def rebase_one(role, repo, branch):
    report = {"repo": role, "path": str(repo), "status": "skipped", "conflicts": []}
    try:
        current = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        report["branch"] = current.stdout.strip() or None
        report["before"] = checked(repo, "rev-parse", "HEAD")
        report["conflicts"] = conflicts(repo)
        active = operation_state(repo)
        if active:
            report.update(reason="진행 중인 Git 작업", operation=active)
            return report
        if current.returncode:
            report["reason"] = "detached HEAD"
            return report
        if checked(repo, "status", "--porcelain=v1", "--untracked-files=all"):
            report["reason"] = "미커밋 변경 또는 기존 충돌"
            return report
        ref = target_ref(repo, branch)
        report["target_ref"] = ref
        report["status"] = "failed"
        result = git(repo, "-c", "rebase.autoStash=false", "rebase", ref)
        report.update(exit_code=result.returncode, output=(result.stdout + result.stderr).strip())
        report["conflicts"] = conflicts(repo)
        report["operation"] = operation_state(repo)
        report["worktree_status"] = checked(repo, "status", "--porcelain=v1")
        report["after"] = checked(repo, "rev-parse", "HEAD")
        if report["conflicts"]:
            report["status"] = "conflict"
        elif result.returncode == 0 and not report["operation"]:
            report["status"] = "success"
        else:
            report["reason"] = "Git 오류 또는 rebase 중단"
    except (ValueError, OSError) as exc:
        report["reason"] = str(exc)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=os.getcwd(), help="combo root or directory inside FE/BE")
    parser.add_argument("branch")
    args = parser.parse_args()
    output = {"target": args.branch, "results": []}
    try:
        if args.branch.startswith("-") or git(args.cwd, "check-ref-format", "refs/heads/" + args.branch).returncode:
            raise ValueError("유효한 브랜치 이름 하나를 입력하세요.")
        found = discover(args.cwd)
        for role in ("FE", "BE"):
            output["results"].append(rebase_one(role, found[role], args.branch))
    except (ValueError, OSError) as exc:
        output["error"] = str(exc)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if len(output["results"]) == 2 and all(
        item["status"] == "success" for item in output["results"]) else 1


if __name__ == "__main__":
    sys.exit(main())
