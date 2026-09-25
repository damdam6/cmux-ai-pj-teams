#!/usr/bin/env python3
"""pj-ctx.py — Resolve a pj project's context documents, per repo, for the session that will
read them.

WHY pj OWNS THIS
----------------
`project.md`'s `ctx:` used to name a `/ctx-domain` domain. Two things are wrong with that now:

  - `ctx-domain` resolves the repo from the CWD's git-common-dir, and a combined task's session
    stands in the parent of several worktrees, which is not a git repo at all. The lookup cannot
    even start there.
  - Context is the PROJECT's, and a project may span two repos whose relevant documents are
    different. One domain name per project cannot say that.

And a domain was only ever a list of repo-relative document paths, so nothing is lost by pj
holding those paths itself — while a great deal is gained: the board owns them, they are visible
in the project document a person edits, and they can differ per repo.

THE FIELD
---------
`ctx:` in `raw/tasks/{project}/project.md`, in either shape:

    ctx:                          # per repo — the keys are the repo's folder under the session
      backend:                    # root (`backend`), or the repo's own name (`example-backend`)
        - docs/dev-guide.md
      frontend:
        - docs/ds/PRD.md
      shared:                     # reserved: not any repo's, so vault-relative or absolute
        - raw/docs/ai-tools/pj-flow.md

    ctx:                          # flat — the project's only repo. What every existing
      - docs/dev-guide.md         # project already looks like.

`docs:` is the legacy spelling of a flat `ctx` and is read when `ctx` is absent, so the projects
registered before this keep working untouched.

HOW A PATH RESOLVES
-------------------
An absolute path, or one under `shared:`, is used as written (vault-relative paths are resolved
against the vault). Everything else is that repo's, and the prefix depends on where the session
stands — which is the whole reason this is a script and not a rule in prose:

    the repo is in the session   ->  <its worktree>/docs/dev-guide.md
                                    (the session root itself, or the child under a parent)
    the repo is NOT in the session -> <the project's worktree for it>/docs/dev-guide.md
                                    (a backend-only task still reads frontend docs from the
                                    board's frontend checkout — never from the backend one)
    neither exists               ->  reported by --check as unresolved

`--root` is how you say where the session stands; `pj-repos.py` answers what that location is.
Without it the repo-qualified form is printed, which is what a person reading the board wants.

USAGE
    pj-ctx.py --project ctx-compact [--root <session root>] [--format lines|json]
    pj-ctx.py --project ctx-compact --check      # report unresolvable entries, write nothing

EXIT CODES
    0  resolved (possibly empty — a project may have no context, and that is allowed)
    1  bad usage / no such project
    2  the frontmatter could not be read
    3  --check found entries that do not exist on disk
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ALIASES = _aliases_path()
REPOS = SCRIPT_DIR / "pj-repos.py"
VAULT = pathlib.Path(os.environ.get("PJ_VAULT")
                     or pathlib.Path.home() / ".local" / "share" / "pj")
TASKS_DIR = VAULT / "raw" / "tasks"
SHARED_KEY = "shared"

FM_RE = re.compile(r"\A---\n(.*?)\n---", re.S)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------- frontmatter


def parse_block(lines: list[str], start: int) -> tuple[object, int]:
    """The subset of YAML these documents use: a scalar, a `- ` list, or a map of `- ` lists.

    Written by hand rather than with a YAML library because there is none installed and this
    file is edited by a person in Obsidian — a parser that accepts exactly the three shapes the
    schema documents will reject a typo instead of silently reinterpreting it.
    """
    indent = len(lines[start]) - len(lines[start].lstrip())
    items: list[str] = []
    mapping: dict[str, list[str]] = {}
    key = None
    i = start + 1          # `start` is the `ctx:`/`docs:` header itself
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        ind = len(raw) - len(raw.lstrip())
        if ind <= indent:
            break
        body = raw.strip()
        if body.startswith("- "):
            (mapping[key] if key else items).append(body[2:].strip())
        elif body.endswith(":"):
            key = body[:-1].strip()
            mapping.setdefault(key, [])
        else:
            # A line inside the block that is neither `- item` nor `key:` — a flow list
            # (`repo: [a.md]`), a missing colon, a stray word. This used to `break`, which
            # returned whatever had been read so far — usually nothing — and the project lost
            # its whole context with no message. Found live. Refusing here is the promise the
            # docstring makes: a typo is rejected, never silently reinterpreted as "no context".
            raise SystemExit(
                f"frontmatter {i + 1}행을 해석할 수 없습니다: {body!r} — ctx/docs 는 "
                f"`- 경로` 줄이나 `repo:` 뒤에 `- 경로` 줄만 받습니다 "
                f"(`repo: [a.md]` 같은 한 줄 목록은 지원하지 않습니다)")
        i += 1
    if mapping:
        return mapping, i
    return items, i


def read_field(project: str) -> tuple[object, str]:
    """(value, which field it came from). `ctx` wins; `docs` is its legacy flat spelling."""
    path = TASKS_DIR / project / "project.md"
    if not path.is_file():
        raise SystemExit(f"프로젝트 문서가 없습니다: {path}")
    m = FM_RE.match(path.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"frontmatter 가 없습니다: {path}")
    lines = m.group(1).splitlines()
    found: dict[str, object] = {}
    for i, line in enumerate(lines):
        mm = re.match(r"^(ctx|docs):(.*)$", line)
        if not mm:
            continue
        name, inline = mm.group(1), mm.group(2).strip()
        if inline:
            # A bare word on the same line is the OLD spelling: a `/ctx-domain` domain name,
            # not a path. Resolving it as one would silently produce a path that does not
            # exist, so it is surfaced as needing migration instead.
            found[name] = ({"_legacy_domain": [inline]}
                           if "/" not in inline and not inline.endswith(".md")
                           else [inline])
        else:
            value, _ = parse_block(lines, i)
            found[name] = value
    ctx, docs = found.get("ctx"), found.get("docs")
    legacy = isinstance(ctx, dict) and "_legacy_domain" in ctx
    if legacy and docs:
        # The old `ctx` is a dead pointer and `docs` holds real paths: a dead pointer must not
        # shadow a live list. Warn about the one, use the other.
        log(f"경고: {project} 의 ctx 가 옛 형식(도메인 이름 "
            f"{ctx['_legacy_domain'][0]!r})이라 무시하고 docs 를 씁니다")
        return docs, "docs"
    if ctx:
        return ctx, "ctx"
    if docs:
        return docs, "docs"
    return [], "ctx"


# ---------------------------------------------------------------- repos


def alias(repo: str) -> dict:
    try:
        data = json.loads(ALIASES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entry = (data.get("aliases") or {}).get(repo)
    return entry if isinstance(entry, dict) else {}


def wt_folder(repo: str) -> str:
    return alias(repo).get("wtFolder") or repo


def session_repos(root: str | None) -> list[dict]:
    if not root:
        return []
    try:
        p = subprocess.run([sys.executable, str(REPOS), "--cwd", root],
                           capture_output=True, text=True)
        return json.loads(p.stdout or "[]")
    except (OSError, ValueError):
        return []


def project_worktrees(project: str, hint: str | None) -> dict[str, str]:
    """repo → the PROJECT's worktree for that repo (the board's checkout of the project branch).

    This is where a repo's documents are read when the repo is not part of the session — a
    BE-only task under a BE+FE project has no frontend checkout of its own, and the project
    branch's frontend worktree is the one that reflects what the project agreed on.
    """
    if not project:
        return {}
    argv = [sys.executable, str(REPOS), "--project", project]
    if hint:
        argv += ["--repo-hint", hint]
    try:
        p = subprocess.run(argv, capture_output=True, text=True)
        return {e["repo"]: e["worktree"] for e in json.loads(p.stdout or "[]")
                if e.get("worktree")}
    except (OSError, ValueError):
        return {}


def key_matches(key: str, repo: str) -> bool:
    """A key may be the repo's folder under the session root (`backend`) or the repo's own name
    (`example-backend`). The two sets do not collide, so accepting both costs nothing."""
    return key == repo or key == wt_folder(repo)


# ---------------------------------------------------------------- resolution


def resolve(value: object, root: str | None, project_repos: list[str],
            project_wts: dict[str, str] | None = None) -> list[dict]:
    """→ [{repo, path, resolved, source}] — `repo` empty for shared/absolute entries.

    A repo-relative path is resolved against a checkout OF THAT REPO, chosen in this order:

      session   the repo is part of the session (`--root` is its worktree, or the parent of
                several worktrees one of which is its) → that worktree
      project   the repo is not in the session → its PROJECT worktree (the board's checkout of
                the project branch), passed in as `project_wts`
      unresolved  neither exists → the repo-qualified form is printed, and --check reports it

    It used to assume that a session with ONE live repo was "the" repo for every key, and
    resolved another repo's documents against it. Found live: a backend-only task under a
    backend+frontend project was told to read `frontend`'s README at `<backend worktree>/README.md`
    — a file that exists, and is the wrong one, so not even --check noticed.
    """
    live = {e["repo"]: e["worktree"] for e in session_repos(root) if e.get("worktree")}
    project_wts = project_wts or {}

    def one(repo: str, raw: str) -> dict:
        if raw.startswith("/") or raw.startswith("~"):
            return {"repo": repo, "path": raw, "source": "absolute",
                    "resolved": str(pathlib.Path(raw).expanduser())}
        if not repo:                                    # shared: vault-relative
            return {"repo": "", "path": raw, "source": "shared",
                    "resolved": str(VAULT / raw)}
        if repo in live:
            return {"repo": repo, "path": raw, "source": "session",
                    "resolved": str(pathlib.Path(live[repo]) / raw)}
        if repo in project_wts:
            return {"repo": repo, "path": raw, "source": "project",
                    "resolved": str(pathlib.Path(project_wts[repo]) / raw)}
        return {"repo": repo, "path": raw, "source": "unresolved",
                "resolved": f"{wt_folder(repo)}/{raw}"}

    out: list[dict] = []
    if isinstance(value, dict):
        if "_legacy_domain" in value:
            name = value["_legacy_domain"][0]
            log(f"경고: ctx 가 옛 형식(도메인 이름 {name!r})입니다 — pj 는 더 이상 "
                f"ctx-domain 을 거치지 않습니다. 그 도메인이 가리키던 문서 경로를 "
                f"repo 별로 적어주세요 (pj-board 가 안내합니다)")
            return []
        for key, paths in value.items():
            if key == SHARED_KEY:
                out += [one("", p) for p in paths]
                continue
            repo = next((r for r in project_repos if key_matches(key, r)), key)
            out += [one(repo, p) for p in paths]
    else:
        # Flat: the project's only repo. With several this is ambiguous and says so.
        repo = project_repos[0] if len(project_repos) == 1 else ""
        if not repo and len(project_repos) > 1:
            log(f"경고: ctx 가 평평한 목록인데 프로젝트가 repo 를 {len(project_repos)}개 "
                f"갖습니다 — 어느 repo 의 경로인지 알 수 없어 repo 없이 해석합니다 "
                f"(repo 별 맵으로 바꾸세요)")
        out += [one(repo, p) for p in value]
    return out


def project_repo_list(project: str) -> list[str]:
    try:
        p = subprocess.run([sys.executable, str(SCRIPT_DIR / "pj-tasks.py"),
                            "proj-get", "--project", project],
                           capture_output=True, text=True)
        meta = json.loads(p.stdout)
        raw = meta.get("repo") or meta.get("meta", {}).get("repo") or ""
    except (OSError, ValueError, AttributeError):
        return []
    return [s for s in str(raw).split(",") if s]


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--root", help="세션이 서는 디렉터리 (워크트리 또는 그 부모)")
    ap.add_argument("--format", choices=["lines", "json"], default="lines")
    ap.add_argument("--check", action="store_true",
                    help="디스크에 없는 항목을 보고 (쓰지 않음)")
    a = ap.parse_args()

    value, field = read_field(a.project)
    entries = resolve(value, a.root, project_repo_list(a.project),
                      project_worktrees(a.project, a.root))

    if a.check:
        missing = [e for e in entries
                   if e["source"] == "unresolved"
                   or not pathlib.Path(e["resolved"]).exists()]
        print(f"project={a.project} field={field} entries={len(entries)} "
              f"missing={len(missing)}")
        for e in missing:
            why = "no checkout of this repo (not in the session, no project worktree)" \
                if e["source"] == "unresolved" else "not on disk"
            print(f"  MISSING {e['repo'] or 'shared'}: {e['path']} -> {e['resolved']}  ({why})")
        return 3 if missing else 0

    if a.format == "json":
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    else:
        for e in entries:
            print(e["resolved"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            sys.exit(2)
        raise
