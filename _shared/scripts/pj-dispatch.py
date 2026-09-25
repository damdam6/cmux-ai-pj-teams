#!/usr/bin/env python3
"""pj-dispatch.py — Decide whether a task needs an environment built or worked in.

WHY NOT "am I in the main checkout?"
------------------------------------
The obvious dispatch — main checkout means bootstrap, linked worktree means implement — is
wrong, because opening a worktree FROM a worktree is a normal move: you are in one task's
worktree, a related task comes up, and you want its branch cut from where you are. Location
says "you're in a worktree, go implement"; what you actually want is a new worktree.

The question is **whose worktree am I in, and does this task have one yet.** That is decided
by the branch name (which carries the slug, `{prefix}/{slug}`) plus a repo-wide look at the
worktree list. Both work identically from the main checkout and from any worktree, so there
is one rule instead of a special case per location.

LOCATION IS NOT ASKED OF GIT DIRECTLY
-------------------------------------
A task may target several repos, and then its worktrees sit side by side under one parent
(`{root}/{slug}/{frontend,backend}`) with the session standing in that parent — which is not a
git repo at all, so `rev-parse` there fails. `pj-repos.py` is what turns a location into
[(repo, branch, worktree)], answering with one entry inside a worktree and with the children
when standing in such a parent. This script asks it instead of running git itself, so a
single-repo task (N=1) and a combined one (N>1) take the same path through the logic below.

SLUG IS THE KEY
---------------
The jt family keyed on a Jira ticket (`[A-Z]+-[0-9]+`, a token issued by a server). Here the
slug is the key and nothing issues it but us, so the checks differ in one way that matters: a
slug is only meaningful if `pj-tasks.py` has it. A branch whose slug is not in the list is a
branch this family did not create, and saying `work` for it would let pj-work implement
against a task that does not exist. That case reports `ask`.

A PROJECT BRANCH IS NOT A TASK BRANCH
-------------------------------------
Each project has its own long-lived branch and a board session standing in its worktree, and
`slug_of()` cannot tell the two kinds apart: `feat/design-system` yields `design-system`, and if
a task happens to carry that slug the board's worktree would be reported as that task's — the
board would be handed to pj-work. So the current branch is checked against the registered
project branches FIRST, and while standing on one no `work` verdict is ever emitted. Bootstrapping
from there is still correct and still supported: `open-ws base=<project branch>` is exactly how a
task branch is meant to be cut.

The same guard makes a bare `/pj-work` in a board session report `ask` rather than guessing a
slug out of the project branch's name.

Every verdict that names a task also prints `project=`, because the task's plan document lives at
`raw/tasks/<project>/<slug>.md` and its context at `raw/tasks/<project>/project.md`. Callers get it
here instead of each running its own lookup.

MULTI-REPO FIELDS
----------------
`branch=`, `worktree=` and `base=` keep their single-value form whenever the task targets one
repo — which every existing task does — so callers that read them verbatim are unaffected. When
a task targets several, each gains a `repo:` prefixed list and `repos=` names the set:

    branch=be:feat/x,fe:feat/y      worktree=/root/slug (the shared parent)
    base=be:feat/proj-be,fe:feat/proj-fe

A caller that needs per-repo detail should ask `pj-repos.py` rather than parsing these.

VERDICTS
    PJ_DISPATCH=work     slug=<slug> project=<project> repos=<...> branch=<...> worktree=<abs path>
        You are standing in this task's worktree (or, for a combined task, the parent holding
        them). Implement here (pj-work).

    PJ_DISPATCH=goto     slug=<slug> project=<project> repos=<...> branch=<...> worktree=<abs path>
        This task already has a worktree, but it isn't this one. This is a question, not a
        refusal: show the path and ask whether to use that worktree or cut another one. A
        second worktree is legitimate, so the caller must not decide this alone.

    PJ_DISPATCH=open-ws  slug=<slug> project=<project> repos=<...> base=<the project's branch(es)>
        This task has no worktree anywhere. Build one per target repo (pj-open-ws), branching
        from `base` — which is the project's branch in that repo, NOT wherever the caller
        stands. Callers must not substitute their own base; that is the whole reason this is
        printed here.

    PJ_DISPATCH=ask      reason=<...>
        No slug given and none derivable, or the slug is not in the task list.

USAGE
    pj-dispatch.py [--slug item-search-hide]
        --slug omitted -> derive it from the current branch (bare "/pj-work" in a worktree).

EXIT CODES
    0 a verdict was printed
    2 no usable location AND no slug — nothing to go on
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
REPOS = pathlib.Path(__file__).resolve().parent / "pj-repos.py"


def as_list(v) -> list[str]:
    """One accessor for both task-list shapes — a bare value or a comma list. Every field this
    script reads (`repo`, `branch`, `project_branch`, `repos`) widened the same way."""
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(s).strip() for s in v if str(s).strip()]


def repos_of(*args: str) -> list[dict]:
    """pj-repos.py, the single answer to "which repos does this refer to". Empty list when it
    resolves nothing (exit 3) — that is an answer, not a failure."""
    try:
        p = subprocess.run([sys.executable, str(REPOS), *args],
                           capture_output=True, text=True)
        return json.loads(p.stdout or "[]")
    except (OSError, ValueError):
        return []


def fmt_pairs(entries: list[dict], key: str) -> str:
    """`feat/x` for one repo, `be:feat/x,fe:feat/y` for several. The single-repo form is the
    old output verbatim, which is what keeps existing callers working."""
    vals = [(e["repo"], e.get(key) or "") for e in entries]
    if len(vals) == 1:
        return vals[0][1]
    return ",".join(f"{r}:{v}" for r, v in vals)


def git(*args: str) -> str:
    try:
        p = subprocess.run(["git", *args], capture_output=True, text=True)
    except OSError:
        return ""
    return p.stdout if p.returncode == 0 else ""


def slug_of(branch: str) -> str | None:
    """Branch is `{prefix}/{slug}`; the prefix never contains a slash."""
    if not branch or "/" not in branch:
        return None
    return branch.split("/", 1)[1].strip() or None


def task_info(slug: str) -> dict | None:
    """The task's record, or None when the lists don't have this slug — then it is not our
    branch. Carries `project_branch`: a task belongs to a project, and that project's branch is
    the only correct base for its worktree, whichever branch you happen to be standing on."""
    try:
        p = subprocess.run([sys.executable, str(TASKS), "get", "--slug", slug],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout)
    except (OSError, ValueError):
        return None


def project_branches() -> set[str]:
    """Every branch a registered project sits on. Standing on one means this is a board's
    worktree, not a task's. A multi-repo project contributes one entry per repo, which is why
    this flattens instead of taking `branch` whole. Unreadable list -> empty set, which only
    costs the guard."""
    try:
        p = subprocess.run([sys.executable, str(TASKS), "proj-list"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return set()
        return {b for r in json.loads(p.stdout) for b in as_list(r.get("branch"))}
    except (OSError, ValueError):
        return set()


def project_branch_map(info: dict) -> dict[str, str]:
    """repo -> the project's branch in that repo, paired positionally with `repo`, which is how
    the two comma lists are written. A task's base is never the caller's location."""
    return dict(zip(as_list(info.get("repo")), as_list(info.get("project_branch"))))


def shared_location(paths: list[str]) -> str | None:
    """The one directory a combined task's session stands in: the parent its worktrees are
    direct children of. None when they are not siblings — mixed layouts must not collapse into
    some far-up ancestor that means nothing."""
    if len(paths) == 1:
        return paths[0]
    parents = {os.path.dirname(os.path.realpath(p)) for p in paths}
    return parents.pop() if len(parents) == 1 else None


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--slug", help="대상 작업 slug (생략하면 현재 위치에서 추출)")
    ap.add_argument("--cwd", help="판정할 위치 (기본: 현재 디렉터리)")
    a = ap.parse_args()

    cwd = os.path.abspath(a.cwd or os.getcwd())
    here = repos_of("--cwd", cwd)

    # Location tells us what we are standing in; the slug tells us what to answer about. With
    # neither there is nothing to go on. With a slug but no usable location the verdict is
    # still computable (goto / open-ws), and refusing it would break exactly the combined and
    # board cases this dispatch exists to serve.
    if not here and not (a.slug or "").strip():
        print("not inside a git repo, and no slug given", file=sys.stderr)
        return 2

    # A project branch is not a task branch — see the docstring. The same set filters the
    # worktrees below, or a board's own worktree gets reported as the task's.
    pbranches = project_branches()
    branch_slug = next((s for s in (slug_of(e.get("branch") or "") for e in here
                                    if (e.get("branch") or "") not in pbranches) if s), None)
    slug = (a.slug or "").strip() or branch_slug

    if not slug:
        print("PJ_DISPATCH=ask reason=slug 이 인자에도 현재 위치에도 없음")
        return 0

    info = task_info(slug)
    if info is None:
        print(f"PJ_DISPATCH=ask reason={slug} 이(가) 작업 목록에 없음 "
              f"(pj-task-regi 로 적재하지 않은 브랜치)")
        return 0

    project = info.get("project", "")

    # The task's target repos, with each one's branch and worktree already resolved. One entry
    # for a single-repo task, several for a combined one — the loop below is the same either way.
    targets = repos_of("--slug", slug)
    if not targets:
        print(f"PJ_DISPATCH=ask reason={slug} 의 대상 repo 를 해석하지 못함 "
              f"(pj-repos.py --slug {slug} 로 확인하세요)")
        return 0
    repos_field = ",".join(e["repo"] for e in targets)

    live = [e for e in targets if e.get("worktree")]
    if live:
        loc = shared_location([e["worktree"] for e in live])
        here_paths = {os.path.realpath(e["worktree"]) for e in here if e.get("worktree")}
        live_paths = {os.path.realpath(e["worktree"]) for e in live}
        standing_in_it = bool(here_paths & live_paths) or (
            loc is not None and os.path.realpath(cwd) == os.path.realpath(loc))
        verdict = "work" if standing_in_it else "goto"
        worktree = loc if loc else fmt_pairs(live, "worktree")
        print(f"PJ_DISPATCH={verdict} slug={slug} project={project} repos={repos_field} "
              f"branch={fmt_pairs(live, 'branch')} worktree={worktree}")
        return 0

    # Nothing exists for this task → build it, branching from its PROJECT's branch in each
    # target repo. Not from where we stand: kicking off from a sibling task's worktree is a
    # supported move, and cutting from that sibling would drag its commits into this task's
    # eventual squash.
    pbmap = project_branch_map(info)
    bases = [{"repo": e["repo"], "base": pbmap.get(e["repo"], "")} for e in targets]
    missing = [b["repo"] for b in bases if not b["base"]]
    if missing:
        print(f"PJ_DISPATCH=ask reason={project} 프로젝트에 {', '.join(missing)} 의 브랜치가 없음 "
              f"(등록이 깨졌습니다 — 그 프로젝트 워크트리에서 /pj-board 재실행)")
        return 0
    print(f"PJ_DISPATCH=open-ws slug={slug} project={project} repos={repos_field} "
          f"base={fmt_pairs(bases, 'base')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
