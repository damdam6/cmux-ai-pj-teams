#!/usr/bin/env python3
"""wire-session-root.py — Make a directory usable as an agent session root.

ONE JOB, TWO TARGETS
--------------------
A session needs the repo's runtime wiring where it STANDS: `.claude/skills`, `.codex`,
`settings.local.json`, and whatever else the repo's alias lists in `linkPaths`. Runtime config
is discovered from the cwd upward, never downward, so it has to be at the cwd itself.

There are two places a pj session stands, and they used to be wired by two different pieces of
code:

    a worktree                     — one repo, wired inside create-worktree.sh
    the parent of several worktrees — several repos, and nothing wired it at all

The second is not a different problem. It is the first with more than one repo, so this script
does both and the repo COUNT decides the behaviour instead of a caller remembering which case
it is in. One repo needs no reconciliation and gets plain symlinks — byte for byte what
create-worktree.sh did before.

HOW IT DECIDES, BY WHAT IS THERE — NOT BY NAME
----------------------------------------------
For each path, across the repos that provide it:

    one repo                     -> symlink target/<path> -> repo/<path>
    all resolve to ONE target    -> symlink once (both repos link the same vault skill: that is
                                    one thing, not two conflicting ones)
    all directories              -> a real directory at the target, recursing with these same
                                    rules — this is what puts BOTH repos' skill sets in reach
    all .json files              -> merged by merge-settings.py (type-driven union; it reports
                                    whatever it cannot decide)
    a directory carrying SKILL.md -> a UNIT, never merged. Two repos with different skills of
                                    one name is a conflict; splicing them file by file would
                                    produce a skill neither repo has
    anything else                -> CONFLICT: nothing is linked, and it is reported

No path is special-cased, so a repo that grows a new runtime directory needs no change here.

COPYING IS FOR A WORKTREE ONLY
------------------------------
`--copy-paths` and `--env-copy` name paths INSIDE the repo's tree (`apps/main-web/.env.local`).
A worktree mirrors that tree, so they land somewhere meaningful; a parent folder does not, and
copying there would create `parent/apps/main-web/...`, a path nothing reads. So copies apply
only when the target is itself a git worktree, and are reported as skipped otherwise. Links are
different: they are runtime config, which belongs wherever the session stands.

UNDOING IT
    A parent folder outlives the worktrees under it: git removes those, and nothing removes the
    config assembled above them. So the wiring is recorded in a manifest at the target and
    `--unwire` removes exactly what is in it, then the target itself if that leaves it empty.

    Precisely what was created, not a guess at what looks safe: almost everything here is a
    symlink, but the merged `settings.local.json` is real content and `.claude/`/`.codex/` are
    real directories, and deciding by appearance would eventually delete something a person put
    there. No manifest means nothing is known to be ours, and `--unwire` refuses.

    The manifest is written only for a NON-worktree target. Inside a worktree it would be an
    untracked file, which is enough to make `git worktree remove` refuse — and a worktree needs
    no undo record anyway, because removing it takes its contents with it.

IDEMPOTENT
    Re-running replaces only what this script owns. A real directory the script did not create
    is reused in place and its children handled individually; anything else that is not a link
    of ours is left alone and reported.

USAGE
    wire-session-root.py --target <dir> [--repo <repo path>...]
        [--link-paths a,b] [--copy-paths a,b] [--env-copy path] [--dry-run]
    wire-session-root.py --target <dir> --unwire [--dry-run]

    --repo omitted  -> discovered from the target with pj-repos.py (the target's own repo when
                       it is a worktree, its children's repos when it is their parent).
    --link-paths omitted -> the union of each repo's alias `linkPaths`.

EXIT CODES
    0  wired, or unwired
    1  bad usage / target missing / no repo to wire from
    2  a repo has no alias linkPaths and none was passed — nothing to wire from
       (--unwire: no manifest, so nothing is known to be ours)
    3  at least one conflict was reported
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ALIASES = _aliases_path()
MERGE = SCRIPT_DIR / "merge-settings.py"
REPOS = SCRIPT_DIR / "pj-repos.py"
MANIFEST = ".pj-wiring.json"


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def csv(value: str | None) -> list[str]:
    return [p.strip().strip("/") for p in (value or "").split(",") if p.strip().strip("/")]


def alias_of(repo: pathlib.Path) -> dict:
    try:
        data = json.loads(ALIASES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entry = (data.get("aliases") or {}).get(repo.name)
    return entry if isinstance(entry, dict) else {}


def discover_repos(target: pathlib.Path) -> list[pathlib.Path]:
    """The repos this target is a session root for, asked of pj-repos.py — the one place that
    knows a location may be a worktree or the parent of several."""
    try:
        p = subprocess.run([sys.executable, str(REPOS), "--cwd", str(target)],
                           capture_output=True, text=True)
        entries = json.loads(p.stdout or "[]")
    except (OSError, ValueError):
        return []
    seen: list[pathlib.Path] = []
    for e in entries:
        mr = pathlib.Path(e["main_repo"])
        if mr not in seen:
            seen.append(mr)
    return seen


def is_worktree(target: pathlib.Path) -> bool:
    try:
        p = subprocess.run(["git", "-C", str(target), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True)
    except OSError:
        return False
    return p.returncode == 0 and os.path.realpath(p.stdout.strip()) == os.path.realpath(target)


def target_of(entry: pathlib.Path) -> str:
    """What an entry should be linked to, fully resolved.

    Resolving matters twice over: the target ends up pointing at the vault rather than at a
    repo's link to it, and two repos' links to the same vault folder compare EQUAL — which is
    how the common case (both repos link the same skill) is recognised as one thing.
    """
    return os.path.realpath(entry)


class Wiring:
    def __init__(self, target: pathlib.Path, dry: bool):
        self.target, self.dry = target, dry
        self.linked: list[str] = []
        self.copied: list[str] = []
        self.merged: list[str] = []
        self.conflicts: list[str] = []
        self.kept: list[str] = []
        # Paths this run created, target-relative — the manifest `--unwire` reads back.
        self.created: list[str] = []

    # ---------------------------------------------------------------- primitives

    def ensure_parent(self, rel: str) -> None:
        """Create the directories `rel` needs, recording each one we actually had to make.

        `mkdir(parents=True)` creates ancestors silently, and an ancestor that is not in the
        manifest is one `--unwire` will leave behind — which is how `.claude/` survived a full
        unwire while everything inside it went.
        """
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            path = self.target / sub
            if path.exists() or path.is_symlink():
                continue
            self.created.append(sub + "/")
            if not self.dry:
                path.mkdir()


    def symlink(self, rel: str, src: str) -> None:
        dest = self.target / rel
        if dest.is_dir() and not dest.is_symlink():
            self.kept.append(f"{rel} (실제 디렉터리라 건드리지 않음)")
            return
        self.linked.append(f"{rel} -> {src}")
        self.created.append(rel)
        self.ensure_parent(rel)
        if self.dry:
            return
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        dest.symlink_to(src)

    def copy(self, rel: str, src: pathlib.Path) -> None:
        dest = self.target / rel
        if os.path.realpath(src) == os.path.realpath(dest):
            # Wiring a repo as its own session root: the source IS the destination, and the
            # clear-then-copy below would delete it. Nothing to do.
            self.kept.append(f"{rel} (원본과 같은 경로)")
            return
        self.copied.append(f"{rel} <- {src}")
        self.ensure_parent(rel)
        if self.dry:
            return
        if dest.exists() or dest.is_symlink():
            (shutil.rmtree(dest) if dest.is_dir() and not dest.is_symlink()
             else dest.unlink())
        # -a semantics: preserve symlinks inside, do not follow them
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=True)
        else:
            shutil.copy2(src, dest, follow_symlinks=False)

    def merge_json(self, rel: str, sources: list[pathlib.Path]) -> None:
        dest = self.target / rel
        self.merged.append(f"{rel} <- {len(sources)} file(s)")
        self.created.append(rel)
        self.ensure_parent(rel)
        if self.dry:
            return
        if dest.is_symlink():
            dest.unlink()
        p = subprocess.run([sys.executable, str(MERGE), "--out", str(dest),
                            *[str(s) for s in sources]], capture_output=True, text=True)
        for line in (p.stderr or "").splitlines():
            if line.startswith("CONFLICT"):
                self.conflicts.append(f"{rel}: {line}")
            elif line.strip():
                log(f"  {line}")
        if p.returncode not in (0, 3):
            self.conflicts.append(f"{rel}: merge-settings.py 실패 — {p.stderr.strip()}")

    # ---------------------------------------------------------------- strategy

    def wire(self, rel: str, sources: list[pathlib.Path]) -> None:
        if len(sources) == 1:
            # Only one repo has it: link it whole, whatever it is. Nothing to reconcile, and
            # linking the directory itself keeps later additions inside it visible.
            self.symlink(rel, target_of(sources[0]))
            return

        targets = {target_of(s) for s in sources}
        if len(targets) == 1:
            # Every repo's copy IS the same thing — the common case, because both repos link
            # their skills from the same vault. Link once and do not look inside.
            self.symlink(rel, targets.pop())
            return

        if all(s.is_dir() for s in sources):
            if any((s / "SKILL.md").exists() for s in sources):
                self.conflicts.append(
                    f"{rel}: 여러 repo 가 같은 이름의 다른 스킬을 갖고 있습니다 — "
                    + " | ".join(sorted(targets)))
                return
            self.wire_dir(rel, sources)
            return

        if all(s.is_file() and s.suffix == ".json" for s in sources):
            self.merge_json(rel, sources)
            return

        kinds = ", ".join(("dir" if s.is_dir() else "file") + f":{s}" for s in sources)
        self.conflicts.append(f"{rel}: 여러 repo 가 제공하지만 병합 규칙이 없습니다 ({kinds})")

    def wire_dir(self, rel: str, sources: list[pathlib.Path]) -> None:
        """Several repos provide genuinely different directories: the target gets a real one
        holding the union of their children, so two repos' skill sets become one set."""
        dest = self.target / rel
        self.ensure_parent(rel)
        if not self.dry:
            if dest.is_symlink():
                dest.unlink()
            dest.mkdir(parents=True, exist_ok=True)
        self.created.append(rel + "/")     # trailing slash: a directory we made
        by_name: dict[str, list[pathlib.Path]] = {}
        for src in sources:
            for child in sorted(src.iterdir()):
                by_name.setdefault(child.name, []).append(child)
        for name, entries in by_name.items():
            # Back through wire(), so each child picks its own strategy and the depth follows
            # the tree instead of a guessed cap: `.codex` needs two levels, `.claude/skills`
            # one, and a skill stops the descent itself.
            self.wire(f"{rel}/{name}", entries)


def write_manifest(target: pathlib.Path, created: list[str], dry: bool) -> bool:
    """Record what was created, so --unwire removes that and nothing else.

    Skipped inside a git worktree: there it would be an untracked file, which is enough to make
    `git worktree remove` refuse, and a worktree needs no undo record — removing it takes its
    contents along.
    """
    if not created or is_worktree(target):
        return False
    if dry:
        return True
    # A re-run on an already wired target creates less than the first run did — the ancestors
    # (`.claude/`, `.claude/skills/`) already exist and are not "created" again. Overwriting the
    # manifest would then forget them, and `--unwire` would leave an empty `.claude/` behind
    # that keeps the parent from being removed. Found live. So the record is a UNION over runs:
    # everything this script ever created here, and only that.
    manifest = target / MANIFEST
    previous: list[str] = []
    if manifest.is_file():
        try:
            previous = json.loads(manifest.read_text(encoding="utf-8")).get("created", [])
        except ValueError:
            previous = []
    merged = list(dict.fromkeys([*previous, *created]))
    manifest.write_text(
        json.dumps({"created": merged}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return True


def unwire(target: pathlib.Path, dry: bool) -> int:
    """Remove exactly what a previous run recorded, then the target if that empties it."""
    manifest = target / MANIFEST
    if not manifest.is_file():
        log(f"{target} 에 {MANIFEST} 가 없습니다 — 이 스크립트가 만든 것이 무엇인지 알 수 "
            f"없어 아무것도 지우지 않습니다")
        return 2
    try:
        created = json.loads(manifest.read_text(encoding="utf-8")).get("created", [])
    except ValueError:
        log(f"{manifest} 가 JSON 이 아닙니다 — 아무것도 지우지 않습니다")
        return 2

    head = "[dry-run] " if dry else ""
    removed, left = [], []
    # Deepest first, and at the same depth files before directories — a trailing-slash entry
    # has the same slash count as its own children, so counting alone would try to remove a
    # directory while its contents were still in it.
    for rel in sorted(created, key=lambda r: (-r.rstrip("/").count("/"), r.endswith("/"))):
        path = target / rel.rstrip("/")
        if not path.exists() and not path.is_symlink():
            continue
        if rel.endswith("/"):
            if dry or not any(path.iterdir()):
                removed.append(rel)
                if not dry:
                    path.rmdir()
            else:
                # Something we did not record is inside: leave the directory and say so.
                left.append(f"{rel} ({len(list(path.iterdir()))} 개 항목이 남아 있음)")
            continue
        removed.append(rel)
        if not dry:
            path.unlink()

    if not dry:
        manifest.unlink()
    print(f"{head}unwired target={target} removed={len(removed)} left={len(left)}")
    for rel in left:
        print(f"{head}  left  {rel}")

    remaining = [] if dry else [c.name for c in target.iterdir()]
    if not remaining:
        print(f"{head}removed target={target}")
        if not dry:
            target.rmdir()
    else:
        print(f"{head}kept target={target} ({', '.join(remaining)})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description=__doc__)
    ap.add_argument("--target", required=True, help="세션이 서는 디렉터리 (워크트리 또는 부모)")
    ap.add_argument("--repo", action="append", dest="repos",
                    help="이 세션이 편집할 repo 경로 (반복). 생략하면 --target 에서 탐색")
    ap.add_argument("--link-paths", help="심링크할 repo 상대 경로, 쉼표 구분 "
                                         "(생략하면 alias 의 linkPaths 합집합)")
    ap.add_argument("--copy-paths", help="복사할 repo 상대 경로, 쉼표 구분 (워크트리 타깃만)")
    ap.add_argument("--env-copy", help="복사할 env 파일의 repo 상대 경로 (워크트리 타깃만)")
    ap.add_argument("--unwire", action="store_true",
                    help="이전 실행이 만든 것을 되돌리고, 비면 타깃 자체도 제거")
    ap.add_argument("--dry-run", action="store_true", help="계획만 출력하고 쓰지 않음")
    a = ap.parse_args()

    target = pathlib.Path(a.target).expanduser().resolve()
    if not target.is_dir():
        log(f"타깃 디렉터리가 없습니다: {target}")
        return 1

    if a.unwire:
        if a.repos or a.link_paths or a.copy_paths or a.env_copy:
            log("--unwire 는 다른 인자와 같이 쓰지 않습니다")
            return 1
        return unwire(target, a.dry_run)

    repos = ([pathlib.Path(r).expanduser().resolve() for r in a.repos] if a.repos
             else discover_repos(target))
    if not repos:
        log(f"{target} 에서 repo 를 찾지 못했습니다 — --repo 로 지정하세요")
        return 1

    # Two modes, and the difference is whether the caller SAID what to wire.
    #
    #   any path flag given -> do exactly that, and nothing else. create-worktree.sh passes
    #       through whatever its own caller asked for, so inferring more from the alias would
    #       make a `--copy-paths` request quietly also link a repo's skills.
    #   no path flag at all -> resolve from each repo's alias `linkPaths`. This is the parent
    #       case, where the caller knows the target but not the contents.
    asked = bool(a.link_paths or a.copy_paths or a.env_copy)
    explicit = csv(a.link_paths)
    order: list[str] = []
    providers: dict[str, list[pathlib.Path]] = {}
    for repo in repos:
        if asked:
            paths = explicit
        else:
            paths = [q.strip().strip("/") for q in (alias_of(repo).get("linkPaths") or [])
                     if q.strip().strip("/")]
            if not paths:
                log(f"{repo.name} 에 걸 경로가 없습니다 — alias 의 linkPaths 를 채우거나 "
                    f"--link-paths 를 주세요")
                return 2
        for rel in paths:
            src = repo / rel
            if not src.exists():
                log(f"건너뜀 (repo 에 없음): {repo.name}/{rel}")
                continue
            if rel not in order:
                order.append(rel)
            providers.setdefault(rel, []).append(src)

    w = Wiring(target, a.dry_run)
    for rel in order:
        w.wire(rel, providers[rel])

    # Copies are repo-tree paths; only a worktree mirrors that tree.
    copies = csv(a.copy_paths) + ([a.env_copy.strip().strip("/")] if a.env_copy else [])
    if copies:
        if not is_worktree(target):
            log(f"복사 건너뜀 ({', '.join(copies)}) — 타깃이 워크트리가 아닙니다: "
                f"레포 트리 상대 경로는 부모 폴더에서 의미가 없습니다")
        elif len(repos) > 1:
            log(f"복사 건너뜀 ({', '.join(copies)}) — repo 가 여러 개입니다")
        else:
            for rel in copies:
                src = repos[0] / rel
                if not src.exists():
                    log(f"건너뜀 (repo 에 없음): {repos[0].name}/{rel}")
                    continue
                w.copy(rel, src)

    wrote = write_manifest(target, w.created, a.dry_run)

    head = "[dry-run] " if a.dry_run else ""
    print(f"{head}target={target}")
    if wrote:
        print(f"{head}manifest={MANIFEST} ({len(w.created)} entries)")
    print(f"{head}repos={', '.join(r.name for r in repos)}")
    print(f"{head}linked={len(w.linked)} copied={len(w.copied)} merged={len(w.merged)} "
          f"conflicts={len(w.conflicts)} kept={len(w.kept)}")
    for rel in w.merged:
        print(f"{head}  merged {rel}")
    for rel in w.copied:
        print(f"{head}  copied {rel}")
    for rel in w.kept:
        print(f"{head}  kept   {rel}")
    for c in w.conflicts:
        log(f"CONFLICT {c}")
    return 3 if w.conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
