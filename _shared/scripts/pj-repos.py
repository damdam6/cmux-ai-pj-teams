#!/usr/bin/env python3
"""pj-repos.py — Answer "which repos, branches and worktrees does this refer to?"

WHY THIS EXISTS
---------------
Every pj skill used to ask that question for itself, with a `git rev-parse` in the session's
cwd. That worked while one task meant one repo and the session always stood inside that repo's
worktree. Neither holds now:

  - A task may target SEVERAL repos, whose worktrees sit side by side under one parent
    (`{root}/{slug}/{frontend,backend}`), and the session stands in that parent — which is
    deliberately NOT a git repo, so a bare `rev-parse` there fails.
  - A board may own one repo or two, and the user decides where it stands.

Scattering that logic means every skill grows the same two-case branch and they drift. So it
lives here once and the callers ask instead of deriving. `pj-dispatch.py`, `pj-done`, `pj-wrap`
and the board registration all go through this.

ONE REPO IS N=1, NOT A SPECIAL CASE
-----------------------------------
A single-repo task returns a one-element list, and the callers loop over it. That is the whole
compatibility story: the existing 24 single-repo projects keep taking the same code path as the
new combined ones, rather than a second, less-travelled one.

MODES
    --cwd <path>        Resolve a location. A git worktree (the main checkout included) answers
                        with itself; a plain directory answers with the git worktrees among its
                        immediate children — that is the combined-task parent folder. Pure git,
                        no vault and no cmux: a caller must be able to ask this before anything
                        is registered anywhere.

    --project <name>    The pairs a board owns, read from the project list, each resolved to the
                        worktree holding that branch when one exists.

    --slug <slug>       The pairs a task targets: its own `repos` subset when it has one, else
                        every repo of its project, each resolved to the worktree holding
                        `{prefix}/{slug}` when one exists.

Both list modes accept the old single-value shape (`repo: "example-backend"`) and the multi-repo
shape (`repo: ["example-backend", "example-frontend"]`) so this script is not sequenced behind the
task-list schema change.

OUTPUT
    JSON array on stdout (default), one object per repo:

        {"repo": "example-backend",        # folder name — the repo-aliases.json key
         "branch": "feat/ctx-compact",    # null when the list mode knows no branch
         "worktree": "/abs/path",         # null when nothing is checked out for it
         "main_repo": "/abs/path",        # the repo's main checkout
         "linked": true}                  # false when `worktree` IS the main checkout

    `--format tsv` prints `repo\\tbranch\\tworktree\\tmain_repo\\tlinked` for shell callers.

EXIT CODES
    0  resolved — a non-empty list was printed
    2  bad usage
    3  nothing resolved — the reason is on stderr and stdout holds `[]`
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()


TASKS = pathlib.Path(__file__).resolve().parent / "pj-tasks.py"


def git(cwd: str, *args: str) -> str | None:
    """None when git failed — callers treat that as 'not a repo here', never as empty output."""
    try:
        p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    except OSError:
        return None
    return p.stdout if p.returncode == 0 else None


def as_list(v) -> list[str]:
    """Accept both task-list shapes: a bare string, or a list. A comma-separated string is
    split too, because that is how the multi-repo form is written in the markdown comment."""
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(s).strip() for s in v if str(s).strip()]


def main_repo_of(path: str) -> str | None:
    out = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if out is None:
        return None
    return out.strip().removesuffix("/.git").removesuffix("/.git/") or None


def entry(main_repo: str, branch: str | None, worktree: str | None) -> dict:
    return {"repo": os.path.basename(main_repo.rstrip("/")),
            "branch": branch or None,
            "worktree": worktree,
            "main_repo": main_repo,
            "linked": bool(worktree) and os.path.realpath(worktree) != os.path.realpath(main_repo)}


def resolve_worktree(main_repo: str, branch: str) -> str | None:
    """The worktree that has `branch` checked out, or None. Repo-wide, so it answers the same
    from anywhere."""
    out = git(main_repo, "worktree", "list", "--porcelain")
    if out is None:
        return None
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            if line[len("branch "):].strip().removeprefix("refs/heads/") == branch:
                return path
    return None


# ---------------------------------------------------------------- --cwd


def from_cwd(cwd: str) -> tuple[list[dict], str]:
    cwd = os.path.abspath(os.path.expanduser(cwd))
    if not os.path.isdir(cwd):
        return [], f"경로가 디렉터리가 아닙니다: {cwd}"

    main_repo = main_repo_of(cwd)
    if main_repo:
        branch = (git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        top = (git(cwd, "rev-parse", "--show-toplevel") or "").strip() or cwd
        return [entry(main_repo, branch if branch != "HEAD" else None, top)], ""

    # Not a repo: this is the combined-task parent. Its immediate children are the worktrees.
    # One level only — a deeper scan would start reporting unrelated repos that merely live
    # somewhere below.
    found: list[dict] = []
    for child in sorted(pathlib.Path(cwd).iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        mr = main_repo_of(str(child))
        if not mr:
            continue
        branch = (git(str(child), "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        top = (git(str(child), "rev-parse", "--show-toplevel") or "").strip() or str(child)
        found.append(entry(mr, branch if branch != "HEAD" else None, top))
    if not found:
        return [], (f"{cwd} 는 git 워크트리가 아니고 하위에 워크트리도 없습니다 "
                    f"(결합 폴더라면 레포별 하위 폴더가 있어야 합니다)")
    return found, ""


# ---------------------------------------------------------------- --project / --slug


def tasks_json(*args: str) -> dict | list | None:
    try:
        p = subprocess.run([sys.executable, str(TASKS), *args],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout)
    except (OSError, ValueError):
        return None


def pairs_of(repos: list[str], branches: list[str]) -> list[tuple[str, str | None]]:
    """Zip repos to branches positionally, which is how the two comma lists are written. A
    length mismatch is not silently truncated — the extra repos come back with no branch, so a
    caller can see that the record is incomplete instead of losing a repo."""
    return [(r, branches[i] if i < len(branches) else None) for i, r in enumerate(repos)]


def resolve_pairs(pairs: list[tuple[str, str | None]], repo_roots: dict[str, str]
                  ) -> tuple[list[dict], list[str]]:
    out, unknown = [], []
    for repo, branch in pairs:
        root = repo_roots.get(repo)
        if not root:
            unknown.append(repo)
            continue
        wt = resolve_worktree(root, branch) if branch else None
        out.append(entry(root, branch, wt))
    return out, unknown


def known_repo_roots(hint: str | None = None) -> dict[str, str]:
    """repo folder name -> main checkout path.

    The strongest evidence is where the caller is STANDING: a worktree names its own repo, and a
    combined task's parent folder names every repo its children belong to. That is resolved
    first, so this works from a parent that is not a git repo at all — which is exactly where a
    combined task's session sits, and where a sibling search has nothing to go on.

    After that, a sibling scan of the hint's repo parent and of `$PJ_REPOS_ROOT` fills in
    the repos the location does not mention (the other half of a project whose worktree was
    never created, say).
    """
    roots: dict[str, str] = {}
    bases = [pathlib.Path(os.environ.get("PJ_REPOS_ROOT", pathlib.Path.home() / "projects"))]
    if hint:
        located, _ = from_cwd(hint)
        for e in located:
            roots.setdefault(e["repo"], e["main_repo"])
            bases.insert(0, pathlib.Path(e["main_repo"]).parent)
    for base in bases:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in roots:
                continue
            if (child / ".git").exists():
                roots[child.name] = str(child)
    return roots


def from_project(project: str, hint: str | None) -> tuple[list[dict], str]:
    rec = tasks_json("proj-get", "--project", project)
    if not isinstance(rec, dict):
        return [], f"프로젝트를 찾을 수 없습니다: {project}"
    meta = rec.get("meta", rec)
    repos = as_list(meta.get("repo"))
    branches = as_list(meta.get("branch"))
    if not repos:
        return [], f"{project} 에 repo 가 기록돼 있지 않습니다"
    found, unknown = resolve_pairs(pairs_of(repos, branches), known_repo_roots(hint))
    if not found:
        return [], f"{project} 의 repo 를 디스크에서 찾지 못했습니다: {', '.join(unknown)}"
    if unknown:
        print(f"경고: 디스크에서 찾지 못한 repo: {', '.join(unknown)}", file=sys.stderr)
    return found, ""


def from_slug(slug: str, hint: str | None) -> tuple[list[dict], str]:
    rec = tasks_json("get", "--slug", slug)
    if not isinstance(rec, dict):
        return [], f"작업 목록에 없는 slug 입니다: {slug}"
    # A task targets a SUBSET of its project's repos (the `repos` field); no field means all
    # of them. The subset is intersected rather than trusted, so a stale `repos` entry cannot
    # point at a repo the project does not own.
    project_repos = as_list(rec.get("repo"))
    subset = as_list(rec.get("repos"))
    repos = [r for r in project_repos if r in subset] if subset else project_repos
    if subset and not repos:
        return [], (f"{slug} 의 repos={','.join(subset)} 가 프로젝트의 "
                    f"repo={','.join(project_repos)} 와 겹치지 않습니다")
    if not repos:
        return [], f"{slug} 의 프로젝트에 repo 가 기록돼 있지 않습니다"

    roots = known_repo_roots(hint)
    branches = as_list(rec.get("branch"))          # the task's own branches, once started
    by_repo = dict(pairs_of(project_repos, branches)) if len(branches) > 1 else {}
    out, unknown = [], []
    for repo in repos:
        root = roots.get(repo)
        if not root:
            unknown.append(repo)
            continue
        branch = by_repo.get(repo) or (branches[0] if len(branches) == 1 else None)
        wt = resolve_worktree(root, branch) if branch else None
        if wt is None and branch is None:
            # Not started yet: find any worktree whose branch ends in this slug, which is the
            # `{prefix}/{slug}` convention, so a caller can still be told where it lives.
            listing = git(root, "worktree", "list", "--porcelain") or ""
            path = None
            for line in listing.splitlines():
                if line.startswith("worktree "):
                    path = line[len("worktree "):].strip()
                elif line.startswith("branch "):
                    b = line[len("branch "):].strip().removeprefix("refs/heads/")
                    if "/" in b and b.split("/", 1)[1] == slug:
                        branch, wt = b, path
                        break
        out.append(entry(root, branch, wt))
    if not out:
        return [], f"{slug} 의 repo 를 디스크에서 찾지 못했습니다: {', '.join(unknown)}"
    if unknown:
        print(f"경고: 디스크에서 찾지 못한 repo: {', '.join(unknown)}", file=sys.stderr)
    return out, ""


# ---------------------------------------------------------------- cli


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cwd", help="위치를 해석 (워크트리 1개 또는 결합 폴더의 하위 N개)")
    g.add_argument("--project", help="보드가 소유한 repo↔브랜치 쌍")
    g.add_argument("--slug", help="작업이 타깃하는 repo↔브랜치 쌍")
    ap.add_argument("--format", choices=["json", "tsv"], default="json")
    ap.add_argument("--repo-hint", help="--project/--slug 에서 repo 탐색의 출발점 (기본 cwd)")
    a = ap.parse_args()

    hint = a.repo_hint or os.getcwd()
    if a.cwd is not None:
        found, reason = from_cwd(a.cwd)
    elif a.project:
        found, reason = from_project(a.project, hint)
    else:
        found, reason = from_slug(a.slug, hint)

    if a.format == "tsv":
        for e in found:
            print("\t".join([e["repo"], e["branch"] or "", e["worktree"] or "",
                             e["main_repo"], "true" if e["linked"] else "false"]))
    else:
        print(json.dumps(found, ensure_ascii=False, indent=2))

    if not found:
        print(reason, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
