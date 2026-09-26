#!/usr/bin/env python3
"""pj-tasks.py — The write gate for the pj project + task lists.

WHY A SCRIPT AND NOT "the skill edits the markdown"
---------------------------------------------------
The pj family's premise is parallel work: several worktrees alive at once, each its own
session, plus one board session per project. Two sessions editing the same markdown by hand do
not produce a merge conflict — git is not involved, the vault is a single working copy — they
produce a SILENT LOST UPDATE, the second write erasing the first.

So the rule is **write only through this script**, which locks, re-reads, and rewrites under
that lock. It also keeps the line format in exactly one place: the lists are read by humans in
Obsidian and by skills through `list`/`get`, and a format that drifts breaks the second group
quietly.

THE FILES
---------
    <vault>/raw/tasks/index.md              projects
    <vault>/raw/tasks/<project>/tasks.md    that project's tasks

A PROJECT is one board session, the repos it owns a worktree in, and one long-lived branch per
repo — and those branches are where its tasks get squash-merged. "Two separate efforts in one
repo" means two projects, which is why the repo is NOT the grouping axis: it is the environment
(`typecheckCmd`, `linkPaths`), while the project is the unit of work.

`repo` and `branch` are therefore comma lists paired POSITIONALLY, and a board owning one repo
writes a one-element list — which is byte-identical to what every existing project already has.
A task may narrow to a subset of those repos with its own `repos` field; without one it covers
the whole project. That subset is why a two-repo project can still hold single-repo tasks.

    ## 진행 중       보드가 등록돼 있고 작업이 도는 중
    ## 완료          사람이 끝났다고 선언한 것

A TASK lives under exactly one project. Status is its section, and there is no status field to
disagree with the section it sits under:

    ## 검토 대기     구현·리뷰까지 끝났고 머지 승인을 기다림
    ## 진행 중       워크트리가 살아 있음
    ## 할 일         적재만 됨, 아직 착수 안 함
    ## 완료          프로젝트 브랜치에 머지됨

One item is one line. Everything a machine needs lives in the trailing HTML comment, which
Obsidian hides in reading view. The visible part is for the human and may be reworded by hand
without breaking anything; the comment is the record. Parse the comment, never the prose.

    - `FE` **디자인 시스템 마이그레이션** — [[ds-migration/project]] · `feat/design-system` · 08-04 <!-- project=ds-migration repo=example-frontend branch=feat/design-system surface=a1b2c3 created=08-04 -->
    - **버튼 variant 정리** — [[button-variants]] · 선행: icon-token-sync · `feat/button-variants` · 08-04 <!-- slug=button-variants created=08-04 deps=icon-token-sync -->

Task lines carry no `repo` — it is the project's, and one fact gets one home. `get`/`list` fill
it in on the way out so callers need not chase it. A task line carries `repos` only when it
targets FEWER repos than its project owns, because a subset equal to the whole repeats the
project's own record and would drift from it.

WHY index.md IS THE LOCK EVEN FOR TASK WRITES
---------------------------------------------
Task data lives per project, but `slug` is global (branches are `{prefix}/{slug}`, worktrees
are `$PJ_WORKTREE_ROOT/{slug}/{repo folder}`, and dispatch recovers the slug from the
branch). Verifying a slug is
unique therefore means reading EVERY project's tasks.md, and that read must not race with
another session's write. So every mutation takes index.md's flock first and does its work under
it — even when index.md itself is not modified. One mutex, one critical section.

The project namespace shares that check: a task may not take a project's name, because the
worktree root is one namespace — `worktrees/{name}/` is the parent for a project's board AND for
a task, so the two would collide there. Catching it at 적재 is cheaper than catching it at 착수.

NOT OURS
--------
`<project>/project.md` is a HUMAN document (goal, scope, `color`, `ctx`, `docs`). This script neither
reads nor writes it — pj-board creates it once, people edit it in Obsidian. That is also why
`docs:` may be a YAML list: no flat frontmatter parser here has to survive it.

DEPENDENCIES
------------
`deps` is a comma-separated list of slugs this task must follow, recorded because the ordering
inside a split is knowledge that only exists at that moment. It is recorded and displayed, not
enforced: nothing here refuses to start a task whose dependency is still open. Deps may cross
projects — the check scans all of them.

A dep must already exist. A typo'd slug is a dependency on nothing, which reads as "no
dependency" — the one failure mode worth catching, so it fails loudly instead.

USAGE
-----
    pj-tasks.py proj-reg  --project ds-migration --branch feat/design-system --surface <uuid>
                          [--repo example-frontend --title "디자인 시스템 마이그레이션"]  # 신규일 때 필수
                          [--force-branch]                                             # 브랜치 변경 승인
    pj-tasks.py proj-reg  --project ctx --surface <uuid> --title "결합 작업" \
                          --pair example-backend:feat/ctx-be \
                          --pair example-frontend:feat/ctx-fe        # repo 를 여럿 소유하는 보드
    pj-tasks.py proj-list [--status 진행 중|완료]
    pj-tasks.py proj-get  --project ds-migration
    pj-tasks.py proj-done --project ds-migration

    pj-tasks.py add    --project ds-migration --slug button-variants --title "버튼 variant 정리"
                       [--deps icon-token-sync,uom-schema]
                       [--repos example-frontend]        # 프로젝트 repo 의 부분집합만 만질 때
    pj-tasks.py start  --slug button-variants --branch feat/button-variants
                       [--repos example-backend,example-frontend]   # 브랜치가 여럿이면 필수
    pj-tasks.py review --slug button-variants
    pj-tasks.py done   --slug button-variants
                       [--merged example-backend]   # repo 를 여럿 타깃할 때, 머지된 repo 누적
    pj-tasks.py list   [--project ds-migration] [--status 할 일|진행 중|검토 대기|완료]
    pj-tasks.py get    --slug button-variants

`proj-list` / `proj-get` / `list` / `get` print JSON on stdout. The mutations print one
parseable line:

    PJ_TASKS=ok op=<op> ...

MONOTONIC TRANSITIONS
---------------------
`start`/`review`/`done` never move a task backwards. Cross-session delivery is uncertain
(a relay can crash between sending and recording) and a manual retry after that is a normal
recovery step, not an error — so a same-state retry answers ok with `result=noop`, a state
that is already further along answers `result=already-ahead`, and only a genuinely backward
or out-of-order request is refused. Refusals are exit 3, not 1, so callers can tell "policy
said no" apart from "you called it wrong".

    start   할 일 → 진행 중    진행 중 + 같은 branch → noop    검토 대기/완료, 다른 branch → 거부
    review  진행 중 → 검토 대기  검토 대기 → noop · 완료 → already-ahead   할 일 → 거부
    done    검토 대기 → 완료    완료 → noop                     할 일/진행 중 → 거부
            --merged 로 일부 repo 만 머지된 경우 검토 대기 유지 + result=partial

EXIT CODES
    0 ok (including a same-state retry: `result=noop` / `result=already-ahead`, zero writes)
    1 bad usage / validation failure (unknown slug or project, duplicate, bad shape,
      unknown dep, branch change without --force-branch, surface already another board)
    2 a list file could not be read or written
    3 transition refused — see MONOTONIC TRANSITIONS
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import pathlib
import re
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()


# Storage root is configurable; see README.md.
VAULT = pathlib.Path(os.environ.get("PJ_VAULT")
                     or pathlib.Path.home() / ".local" / "share" / "pj")
TASKS_DIR = VAULT / "raw" / "tasks"
INDEX = TASKS_DIR / "index.md"
ALIASES = _aliases_path()

PROJ_SECTIONS = ["진행 중", "완료"]
TASK_SECTIONS = ["검토 대기", "진행 중", "할 일", "완료"]

META_RE = re.compile(r"<!--\s*(.*?)\s*-->\s*$")
FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)

# Every machine field is validated on the way IN and re-validated on the way OUT, because these
# values leave Python: `project` and `slug` become filesystem paths, `surface` and `branch` get
# interpolated into cmux and git command lines by the skills that read them. The files are also
# hand-editable in Obsidian, so "we only ever write good values" is not a property this can rely
# on — a field that fails on read is dropped rather than trusted.
#
# The shapes are deliberately narrow: a folder name is not a path (no `/`, no `..`), and nothing
# may contain whitespace or `-->` (the first would break the space-separated kv parsing, the
# second would end the HTML comment early and spill machine state into the visible line).
SLUG_PAT = r"[a-z0-9][a-z0-9-]*"
REPO_PAT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
BRANCH_PAT = r"[A-Za-z0-9][A-Za-z0-9._/-]*"

# `repo`, `branch` and `repos` are COMMA LISTS, and a single value is a one-element list — that
# is the whole compatibility story for the projects already recorded. The separator is a comma
# with no space precisely because the kv parsing above is space-separated, so a list stays one
# token. An empty element (`a,`, `,a`, `a,,b`) is refused rather than dropped: it would pair the
# wrong branch to a repo, and the pairing is positional.
FIELD_RE = {
    "project": re.compile(rf"^{SLUG_PAT}$"),
    "slug":    re.compile(rf"^{SLUG_PAT}$"),
    "repo":    re.compile(rf"^{REPO_PAT}(,{REPO_PAT})*$"),
    "repos":   re.compile(rf"^{REPO_PAT}(,{REPO_PAT})*$"),
    "branch":  re.compile(rf"^{BRANCH_PAT}(,{BRANCH_PAT})*$"),
    "surface": re.compile(r"^[A-Za-z0-9-]+$"),
    "deps":    re.compile(rf"^{SLUG_PAT}(,{SLUG_PAT})*$"),
    "merged":  re.compile(rf"^{REPO_PAT}(,{REPO_PAT})*$"),
    "created": re.compile(r"^[0-9]{2}-[0-9]{2}$"),
    "closed":  re.compile(r"^[0-9]{2}-[0-9]{2}$"),
}

INDEX_HEADER = ("# Projects\n\n"
                "프로젝트 목록. 쓰기는 `pj-tasks.py` 를 통해서만 — 직접 고치면 진행 중인\n"
                "세션과 어긋날 수 있습니다. 각 프로젝트의 작업 목록은 `{project}/tasks.md`,\n"
                "정의와 맥락은 `{project}/project.md` 입니다.\n")


class Fail(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def today() -> str:
    return datetime.date.today().strftime("%m-%d")


def check(field: str, value: str) -> str:
    rx = FIELD_RE.get(field)
    if rx and not rx.match(value or ""):
        raise Fail(1, f"{field} 형식이 잘못됐습니다: {value!r}")
    return value


def clean(meta: dict) -> dict:
    """Drop machine fields that don't match their shape. Used on READ — a hand-edited or
    corrupted line loses the bad field instead of handing it to a shell."""
    return {k: v for k, v in meta.items()
            if k not in FIELD_RE or FIELD_RE[k].match(v or "")}


def items_of(value: str) -> list[str]:
    """One comma field -> its elements. The single accessor for `repo`, `repos` and `branch`,
    so nothing has to know whether a given project has one repo or two."""
    return [s for s in (value or "").split(",") if s]


def short_one(repo: str, aliases: dict) -> str:
    entry = aliases.get(repo)
    if isinstance(entry, dict):
        return entry.get("short") or repo
    return entry or repo


def short_of(repo: str) -> str:
    """The display token for a repo field: `BE`, or `BE+FE` when the project owns both. Falls
    back to the repo key so an unregistered repo still lists cleanly instead of failing the
    write."""
    try:
        al = json.loads(ALIASES.read_text(encoding="utf-8")).get("aliases", {})
    except (OSError, ValueError):
        al = {}
    return "+".join(short_one(r, al) for r in items_of(repo))


def flatten(title: str) -> str:
    """The title is free Korean text and the only field with no shape, so it is flattened here:
    a newline would split one item into two lines (the second losing its machine comment and
    becoming invisible), and a stray comment delimiter would move where the machine half is
    thought to begin."""
    return " ".join(title.split()).replace("<!--", "").replace("-->", "").strip()


def title_of(line: str) -> str:
    m = re.search(r"\*\*(.+?)\*\*", line)
    return m.group(1) if m else ""


def span_of(meta: dict) -> str:
    span = meta.get("created", "")
    if meta.get("closed"):
        span = f"{span} → {meta['closed']}"
    return span


def kv_of(meta: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in meta.items() if v)


# ---------- parse / render ----------

def parse(text: str, sections: list[str], key: str) -> tuple[dict, dict[str, list[dict]]]:
    """(frontmatter, {section: [item, ...]}). Items without `key` in their machine comment are
    skipped — a hand-written line with no comment is left where it is."""
    fm: dict = {}
    m = FM_RE.match(text)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
        text = text[m.end():]

    out: dict[str, list[dict]] = {s: [] for s in sections}
    section = None
    for line in text.splitlines():
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h:
            section = h.group(1) if h.group(1) in sections else None
            continue
        if section is None or not line.startswith("- "):
            continue
        mm = META_RE.search(line)
        if not mm:
            continue
        meta = clean(dict(kv.split("=", 1) for kv in mm.group(1).split() if "=" in kv))
        if key not in meta:
            continue
        out[section].append({"meta": meta, "line": line})
    return fm, out


def render(sections: list[str], items: dict[str, list[dict]], header: str) -> str:
    parts = [header]
    for s in sections:
        parts.append(f"\n## {s}\n")
        rows = items.get(s) or []
        parts.append(("\n".join(r["line"] for r in rows) + "\n") if rows else "\n")
    return "".join(parts)


def branch_bits(meta: dict, repo_field: str = "repo") -> list[str]:
    """The visible branch token(s). One branch renders exactly as it always did — a bare
    backticked name. Several are each labelled with their repo's short token, because the
    branch↔repo pairing is positional in the machine comment and a reader cannot recover it
    from `feat/x · feat/y`."""
    branches = items_of(meta.get("branch", ""))
    if not branches:
        return []
    if len(branches) == 1:
        return [f"`{branches[0]}`"]
    try:
        al = json.loads(ALIASES.read_text(encoding="utf-8")).get("aliases", {})
    except (OSError, ValueError):
        al = {}
    repos = items_of(meta.get(repo_field, ""))
    return [f"`{short_one(repos[i], al)} {b}`" if i < len(repos) else f"`{b}`"
            for i, b in enumerate(branches)]


def compose_proj(meta: dict, title: str) -> str:
    bits = [f"[[{meta['project']}/project]]"]
    bits += branch_bits(meta)
    if span_of(meta):
        bits.append(span_of(meta))
    return (f"- `{short_of(meta.get('repo', ''))}` **{flatten(title)}** — "
            + " · ".join(bits) + f" <!-- {kv_of(meta)} -->")


def compose_task(meta: dict, title: str) -> str:
    bits = [f"[[{meta['slug']}]]"]
    # `repos` is present only when the task targets a SUBSET of its project's repos, so the
    # badge appears exactly when it carries information — a task covering the whole project
    # would only repeat the project's own badge.
    if meta.get("repos"):
        bits.append(f"`{short_of(meta['repos'])}`")
    if meta.get("deps"):
        bits.append("선행: " + ", ".join(meta["deps"].split(",")))
    bits += branch_bits(meta, repo_field="repos")
    if span_of(meta):
        bits.append(span_of(meta))
    return (f"- **{flatten(title)}** — " + " · ".join(bits) + f" <!-- {kv_of(meta)} -->")


# ---------- file access ----------

def tasks_path(project: str) -> pathlib.Path:
    return TASKS_DIR / project / "tasks.md"


def tasks_header(project: str) -> str:
    return (f"# {project} — 작업\n\n"
            "쓰기는 `pj-tasks.py` 를 통해서만 — 직접 고치면 진행 중인 세션과 어긋날 수\n"
            "있습니다. 상태는 항목이 속한 섹션입니다.\n")


@contextlib.contextmanager
def locked_index(exclusive: bool = True):
    """Hold index.md's flock for the whole operation. Task writes happen under it too — see the
    module docstring."""
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    if not INDEX.exists():
        INDEX.write_text(render(PROJ_SECTIONS, {s: [] for s in PROJ_SECTIONS}, INDEX_HEADER),
                         encoding="utf-8")
    try:
        fh = open(INDEX, "r+", encoding="utf-8")
    except OSError as e:
        raise Fail(2, f"프로젝트 목록을 열 수 없습니다: {INDEX} ({e})")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        fh.seek(0)
        yield fh, parse(fh.read(), PROJ_SECTIONS, "project")
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def write_index(fh, items: dict[str, list[dict]]) -> None:
    """Rewrite in place while holding the lock. Writing a sibling temp file and renaming would
    drop the lock with the inode, so the point of the lock would be lost."""
    fh.seek(0)
    fh.truncate()
    fh.write(render(PROJ_SECTIONS, items, INDEX_HEADER))
    fh.flush()
    os.fsync(fh.fileno())


def read_tasks(project: str) -> dict[str, list[dict]]:
    p = tasks_path(project)
    if not p.exists():
        return {s: [] for s in TASK_SECTIONS}
    try:
        _, items = parse(p.read_text(encoding="utf-8"), TASK_SECTIONS, "slug")
    except OSError as e:
        raise Fail(2, f"작업 목록을 읽을 수 없습니다: {p} ({e})")
    return items


def write_tasks(project: str, items: dict[str, list[dict]]) -> None:
    p = tasks_path(project)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render(TASK_SECTIONS, items, tasks_header(project)), encoding="utf-8")
    except OSError as e:
        raise Fail(2, f"작업 목록을 쓸 수 없습니다: {p} ({e})")


# ---------- lookups ----------

def all_projects(projs: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    return [(s, it) for s in PROJ_SECTIONS for it in projs[s]]


def find_project(projs: dict[str, list[dict]], project: str) -> tuple[str, dict]:
    for s, it in all_projects(projs):
        if it["meta"].get("project") == project:
            return s, it
    raise Fail(1, f"목록에 없는 프로젝트입니다: {project} — `/pj-board {project}` 로 먼저 "
                  f"등록하세요 (그 프로젝트 브랜치의 워크트리에서).")


def locate_task(projs, slug: str) -> tuple[str, dict, str, dict]:
    """(project, project meta, section, task item) — scans every project."""
    for _, pit in all_projects(projs):
        project = pit["meta"]["project"]
        items = read_tasks(project)
        for s in TASK_SECTIONS:
            for it in items[s]:
                if it["meta"].get("slug") == slug:
                    return project, pit["meta"], s, it
    raise Fail(1, f"목록에 없는 slug 입니다: {slug}")


def all_task_slugs(projs) -> set[str]:
    out: set[str] = set()
    for _, pit in all_projects(projs):
        items = read_tasks(pit["meta"]["project"])
        for s in TASK_SECTIONS:
            out |= {i["meta"]["slug"] for i in items[s]}
    return out


def move_task(project: str, slug: str, to: str, **updates: str) -> dict:
    items = read_tasks(project)
    src = it = None
    for s in TASK_SECTIONS:
        for cand in items[s]:
            if cand["meta"].get("slug") == slug:
                src, it = s, cand
    if it is None:
        raise Fail(1, f"{project} 안에 {slug} 이 없습니다")
    items[src].remove(it)
    it["meta"].update({k: v for k, v in updates.items() if v})
    it["line"] = compose_task(it["meta"], title_of(it["line"]))
    items[to].insert(0, it)
    write_tasks(project, items)
    return it


def task_dict(project: str, pmeta: dict, status: str, it: dict) -> dict:
    m = dict(it["meta"])
    d = {"status": status, "title": title_of(it["line"]),
         "project": project, "repo": pmeta.get("repo"),
         "project_branch": pmeta.get("branch"), "surface": pmeta.get("surface"), **m}
    d["deps"] = m["deps"].split(",") if m.get("deps") else []
    return d


def proj_dict(status: str, it: dict) -> dict:
    return {"status": status, "title": title_of(it["line"]), **dict(it["meta"])}


# ---------- project operations ----------

def op_proj_reg(a) -> int:
    """Register this session as the project's board, creating the project if it is new.

    Creation and registration are one act: a project IS a board session standing on a branch, so
    there is nothing to create before a board exists and nothing to configure afterwards — the
    branch and surface are byproducts of registering, which is why they cannot drift from
    reality.
    """
    check("project", a.project)
    check("branch", a.branch)
    check("surface", a.surface)
    with locked_index() as (fh, (_, projs)):
        # A session stands on exactly one branch, so it cannot be two projects' board. Refuse
        # rather than ask: registering here would point that project's merges at this branch.
        for _, it in all_projects(projs):
            if it["meta"].get("surface") == a.surface and it["meta"]["project"] != a.project:
                raise Fail(1, f"이 세션은 이미 {it['meta']['project']} 의 보드입니다 "
                              f"({it['meta'].get('branch')}). {a.project} 는 그 프로젝트의 "
                              f"워크트리에서 등록하세요.")
        try:
            _, it = find_project(projs, a.project)
        except Fail:
            it = None

        if it is not None:
            old = it["meta"].get("branch")
            if old and old != a.branch and not a.force_branch:
                raise Fail(1, f"{a.project} 는 {old} 에 등록돼 있습니다. 지금 브랜치는 "
                              f"{a.branch} 입니다 — 머지 타깃이 바뀝니다. 사용자에게 확인한 "
                              f"뒤 --force-branch 로 다시 실행하세요.")
            it["meta"]["surface"] = a.surface
            it["meta"]["branch"] = a.branch
            if a.repo:
                it["meta"]["repo"] = check("repo", a.repo)
            it["line"] = compose_proj(it["meta"], title_of(it["line"]))
            write_index(fh, projs)
            print(f"PJ_TASKS=ok op=proj-reg project={a.project} created=no "
                  f"branch={a.branch}")
            return 0

        if not (a.repo and a.title):
            raise Fail(1, f"{a.project} 는 새 프로젝트입니다 — --repo 와 --title 이 필요합니다")
        check("repo", a.repo)
        if len(items_of(a.repo)) != len(items_of(a.branch)):
            raise Fail(1, f"repo {len(items_of(a.repo))}개와 "
                          f"branch {len(items_of(a.branch))}개의 수가 다릅니다 — 순서로 "
                          "짝지어지므로 repo 마다 브랜치가 하나씩 있어야 합니다.")
        meta = {"project": a.project, "repo": a.repo, "branch": a.branch,
                "surface": a.surface, "created": today()}
        projs["진행 중"].insert(0, {"meta": meta, "line": compose_proj(meta, a.title)})
        write_index(fh, projs)
    print(f"PJ_TASKS=ok op=proj-reg project={a.project} created=yes branch={a.branch}")
    return 0


def op_proj_done(a) -> int:
    check("project", a.project)
    with locked_index() as (fh, (_, projs)):
        src, it = find_project(projs, a.project)
        if src == "완료":
            raise Fail(1, f"{a.project} 는 이미 완료입니다")
        projs[src].remove(it)
        it["meta"]["closed"] = today()
        it["line"] = compose_proj(it["meta"], title_of(it["line"]))
        projs["완료"].insert(0, it)
        write_index(fh, projs)
    print(f"PJ_TASKS=ok op=proj-done project={a.project} status=완료")
    return 0


def op_proj_list(a) -> int:
    with locked_index(exclusive=False) as (_, (_, projs)):
        rows = [proj_dict(s, it) for s, it in all_projects(projs)
                if not a.status or s == a.status]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def op_proj_get(a) -> int:
    check("project", a.project)
    with locked_index(exclusive=False) as (_, (_, projs)):
        s, it = find_project(projs, a.project)
        d = proj_dict(s, it)
        items = read_tasks(a.project)
        d["task_counts"] = {sec: len(items[sec]) for sec in TASK_SECTIONS}
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return 0


# ---------- task operations ----------

def op_add(a) -> int:
    check("project", a.project)
    check("slug", a.slug)
    if a.deps:
        check("deps", a.deps)
    with locked_index() as (_, (_, projs)):
        find_project(projs, a.project)          # must exist — a task needs a board to report to
        known = all_task_slugs(projs)
        if a.slug in known:
            raise Fail(1, f"이미 쓰이고 있는 slug 입니다: {a.slug}. 다른 slug 을 쓰세요 — "
                          "자동으로 번호를 붙이지 않습니다.")
        # The worktree root is one namespace: `worktrees/{name}/` is the parent for a board
        # AND for a task, so the two would collide there — and create-worktree.sh would only
        # catch it at 착수.
        if a.slug in {it["meta"]["project"] for _, it in all_projects(projs)}:
            raise Fail(1, f"{a.slug} 는 프로젝트 이름입니다. 작업 slug 과 프로젝트는 "
                          "워크트리 루트 이름공간을 공유하므로 다른 slug 을 쓰세요.")
        for d in (a.deps.split(",") if a.deps else []):
            if d not in known:
                raise Fail(1, f"선행 작업 {d} 이(가) 목록에 없습니다. 먼저 적재하세요 — "
                              "없는 slug 을 가리키는 의존은 '의존 없음'과 구분되지 않습니다.")
        # A task may target a SUBSET of its project's repos. The subset is checked here rather
        # than trusted, for the same reason a dep is: a repo the project does not own reads as
        # "no restriction" downstream, which is the one failure worth catching at 적재.
        repos = ""
        if a.repos:
            check("repos", a.repos)
            owned = items_of(find_project(projs, a.project)[1]["meta"].get("repo", ""))
            stray = [r for r in items_of(a.repos) if r not in owned]
            if stray:
                raise Fail(1, f"{a.project} 가 소유하지 않은 repo 입니다: {', '.join(stray)} "
                              f"(프로젝트 repo: {', '.join(owned) or '(없음)'}). 보드가 그 "
                              "repo 의 워크트리도 소유해야 결합 작업을 적재할 수 있습니다.")
            # A subset equal to the whole project carries no information — drop it so the line
            # does not grow a badge that only repeats the project's own.
            repos = a.repos if len(items_of(a.repos)) < len(owned) else ""

        items = read_tasks(a.project)
        meta = {"slug": a.slug, "created": today()}
        if repos:
            meta["repos"] = repos
        if a.deps:
            meta["deps"] = a.deps
        items["할 일"].insert(0, {"meta": meta, "line": compose_task(meta, a.title)})
        write_tasks(a.project, items)
    print(f"PJ_TASKS=ok op=add project={a.project} slug={a.slug} status=할 일")
    return 0


# Per-op outcome by the section the task is standing in. "same-branch" is start's special
# case: a retry of the same start is a no-op, but the same slug arriving on a DIFFERENT branch
# is a conflict, not a retry — the recorded branch is the merge source and must not be
# silently rewritten.
RULES = {
    "start":  {"할 일": "move", "진행 중": "same-branch", "검토 대기": "refuse", "완료": "refuse"},
    "review": {"진행 중": "move", "검토 대기": "noop", "완료": "already-ahead", "할 일": "refuse"},
    "done":   {"검토 대기": "move", "완료": "noop", "할 일": "refuse", "진행 중": "refuse"},
}

REFUSE_HINT = {
    "start":  "검토 대기/완료 작업은 다시 시작하지 않습니다",
    "review": "할 일 작업은 start 뒤에만 검토 대기로 갑니다",
    "done":   "완료는 검토 대기에서만 갑니다 — 보드가 review 를 먼저 기록해야 합니다",
}


def _transition(a, to: str, **updates: str) -> int:
    check("slug", a.slug)
    with locked_index() as (_, (_, projs)):
        project, _, cur, it = locate_task(projs, a.slug)
        outcome = RULES[a.op][cur]
        if outcome == "same-branch":
            old = it["meta"].get("branch")
            if old == a.branch:
                outcome = "noop"
            else:
                raise Fail(3, f"{a.slug} 은(는) 이미 진행 중입니다 — 기록된 브랜치는 "
                              f"{old or '(없음)'}, 요청된 브랜치는 {a.branch} 입니다. "
                              "같은 브랜치의 재시도만 no-op 으로 허용됩니다.")
        if outcome == "refuse":
            raise Fail(3, f"{a.slug} 은(는) {cur} 상태입니다 — {a.op} 는 여기서 허용되지 "
                          f"않습니다 ({REFUSE_HINT[a.op]}).")
        if outcome == "move":
            move_task(project, a.slug, to, **updates)
            print(f"PJ_TASKS=ok op={a.op} project={project} slug={a.slug} status={to}")
        else:
            # noop / already-ahead: the requested fact is already recorded (or surpassed).
            # Answer ok without touching the file so a retry storm cannot churn the list.
            print(f"PJ_TASKS=ok op={a.op} project={project} slug={a.slug} status={cur} "
                  f"result={outcome}")
    return 0


def op_start(a) -> int:
    check("branch", a.branch)
    updates = {"branch": a.branch}
    if a.repos:
        updates["repos"] = check("repos", a.repos)
    # The branch↔repo pairing is positional, so several branches with no `repos` to pair them
    # against is an unreadable record. Refuse instead of inferring an order from the project:
    # the caller (pj-open-ws) just created those worktrees and knows which repo each is.
    if len(items_of(a.branch)) > 1 and not items_of(updates.get("repos", "")):
        raise Fail(1, f"브랜치가 {len(items_of(a.branch))}개인데 --repos 가 없습니다 — "
                      "브랜치와 repo 는 순서로 짝지어지므로 어느 브랜치가 어느 repo 의 "
                      "것인지 기록돼야 합니다.")
    if (len(items_of(a.branch)) != len(items_of(updates.get("repos", "")))
            and updates.get("repos")):
        raise Fail(1, f"--branch {len(items_of(a.branch))}개와 "
                      f"--repos {len(items_of(updates['repos']))}개의 수가 다릅니다 — "
                      "순서로 짝지어지므로 같은 개수여야 합니다.")
    if updates.get("repos"):
        # 착수 may NARROW the target set but never widen it. The set recorded at 적재 is what
        # the plan and the created worktrees are built around, so a wider one here would
        # silently disagree with both — and with `done`, which gates 완료 on this field.
        with locked_index() as (_, (_, projs)):
            _, _, _, it = locate_task(projs, check("slug", a.slug))
            filed = items_of(it["meta"].get("repos", ""))
        stray = [r for r in items_of(updates["repos"]) if filed and r not in filed]
        if stray:
            raise Fail(1, f"{a.slug} 은(는) {', '.join(filed)} 만 타깃하도록 적재됐습니다 — "
                          f"착수에서 {', '.join(stray)} 를 추가할 수 없습니다. 타깃을 "
                          "넓히려면 작업을 새로 적재하세요.")
    return _transition(a, "진행 중", **updates)


def op_review(a) -> int:
    return _transition(a, "검토 대기")


def op_done(a) -> int:
    """완료 means every target repo's branch landed.

    A task touching two repos is merged twice, and the second merge can conflict while the
    first already landed. Moving it to 완료 on the first success would record work as finished
    with a branch still outstanding, so `--merged` accumulates which repos are in and the move
    happens only when they all are. A single-repo task needs no `--merged` at all — one merge
    is the whole set — which keeps the existing call unchanged.
    """
    if not a.merged:
        return _transition(a, "완료", closed=today())
    check("merged", a.merged)
    with locked_index() as (_, (_, projs)):
        project, _, cur, it = locate_task(projs, a.slug)
        targets = items_of(it["meta"].get("repos")
                           or find_project(projs, project)[1]["meta"].get("repo", ""))
        stray = [r for r in items_of(a.merged) if r not in targets]
        if stray:
            raise Fail(1, f"{a.slug} 이(가) 타깃하지 않는 repo 입니다: {', '.join(stray)} "
                          f"(타깃: {', '.join(targets) or '(없음)'})")
        landed = items_of(it["meta"].get("merged", ""))
        landed += [r for r in items_of(a.merged) if r not in landed]
        remaining = [r for r in targets if r not in landed]
    if remaining:
        # Record the partial state and stay put. The task is still 검토 대기, and the remaining
        # repos are named so a retry knows what is left.
        move_task(project, a.slug, cur, merged=",".join(landed))
        print(f"PJ_TASKS=ok op=done project={project} slug={a.slug} status={cur} "
              f"result=partial merged={','.join(landed)} "
              f"remaining={','.join(remaining)}")
        return 0
    a.merged = ",".join(landed)
    return _transition(a, "완료", closed=today(), merged=a.merged)


def op_list(a) -> int:
    rows = []
    with locked_index(exclusive=False) as (_, (_, projs)):
        for _, pit in all_projects(projs):
            project = pit["meta"]["project"]
            if a.project and project != a.project:
                continue
            items = read_tasks(project)
            for s in TASK_SECTIONS:
                if a.status and s != a.status:
                    continue
                rows += [task_dict(project, pit["meta"], s, it) for it in items[s]]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def op_get(a) -> int:
    check("slug", a.slug)
    with locked_index(exclusive=False) as (_, (_, projs)):
        project, pmeta, s, it = locate_task(projs, a.slug)
        d = task_dict(project, pmeta, s, it)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return 0


OPS = {"proj-reg": op_proj_reg, "proj-list": op_proj_list, "proj-get": op_proj_get,
       "proj-done": op_proj_done,
       "add": op_add, "start": op_start, "review": op_review, "done": op_done,
       "list": op_list, "get": op_get}

NEED = {"proj-reg": ["project", "branch", "surface"], "proj-list": [],
        "proj-get": ["project"], "proj-done": ["project"],
        "add": ["project", "slug", "title"], "start": ["slug", "branch"],
        "review": ["slug"], "done": ["slug"], "list": [], "get": ["slug"]}


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("op", choices=list(OPS))
    ap.add_argument("--project", help="프로젝트 slug (예: ds-migration)")
    ap.add_argument("--repo", help="메인 저장소 폴더명 (예: example-frontend)")
    ap.add_argument("--slug")
    ap.add_argument("--title")
    ap.add_argument("--branch")
    ap.add_argument("--surface", help="proj-reg: 이 보드 세션의 cmux surface UUID")
    ap.add_argument("--force-branch", action="store_true",
                    help="proj-reg: 등록된 브랜치를 바꾼다 (머지 타깃이 바뀜 — 사용자 확인 후)")
    ap.add_argument("--deps", help="선행 작업 slug, 쉼표 구분 (공백 없이)")
    ap.add_argument("--repos", help="add/start: 이 작업이 타깃하는 repo (프로젝트 repo 의 "
                                    "부분집합, 쉼표 구분). 생략하면 프로젝트 전체")
    ap.add_argument("--merged", help="done: 방금 머지된 repo. 타깃 repo 가 여럿일 때 모두 "
                                     "머지되기 전까지는 검토 대기에 남습니다")
    ap.add_argument("--pair", action="append", metavar="REPO:BRANCH",
                    help="proj-reg: repo 와 그 repo 의 프로젝트 브랜치. 여러 repo 를 "
                         "소유하는 보드는 반복해서 지정합니다 (--repo/--branch 대신)")
    ap.add_argument("--status")
    a = ap.parse_args()

    # `--pair` is an input FORM, not a second code path: it is folded into the same comma
    # `repo`/`branch` fields a single-repo board writes, so exactly one registration path exists.
    if a.pair:
        if a.repo or a.branch:
            print("--pair 와 --repo/--branch 는 같이 쓸 수 없습니다", file=sys.stderr)
            return 1
        repos, branches = [], []
        for spec in a.pair:
            if spec.count(":") != 1 or not all(spec.split(":")):
                print(f"--pair 는 repo:branch 형식입니다: {spec!r}", file=sys.stderr)
                return 1
            r, b = spec.split(":")
            if r in repos:
                print(f"--pair 에 같은 repo 가 두 번 나옵니다: {r}", file=sys.stderr)
                return 1
            repos.append(r)
            branches.append(b)
        a.repo, a.branch = ",".join(repos), ",".join(branches)

    sections = PROJ_SECTIONS if a.op.startswith("proj-") else TASK_SECTIONS
    if a.status and a.status not in sections:
        print(f"--status 는 {' / '.join(sections)} 중 하나입니다", file=sys.stderr)
        return 1
    missing = [f"--{f}" for f in NEED[a.op] if not getattr(a, f)]
    if missing:
        print(f"누락된 인자: {' '.join(missing)}", file=sys.stderr)
        return 1

    try:
        return OPS[a.op](a)
    except Fail as e:
        print(str(e), file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
