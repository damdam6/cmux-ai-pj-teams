#!/usr/bin/env python3
"""Assign or reapply a perceptually distinct color from the current pj board location.

The location is a board worktree for a one-repo project, or the PARENT of the board worktrees
for a project spanning several repos — that parent is not a git repo, so nothing here asks git
directly; `pj-repos.py --cwd` answers what the location is in both cases."""

from __future__ import annotations

import argparse
import colorsys
import contextlib
import fcntl
import json
import math
import os
import pathlib
import random
import re
import subprocess
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "_shared" / "scripts"))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()



# Storage root is configurable; see GUIDE.md.
VAULT = pathlib.Path(os.environ.get("PJ_VAULT")
                     or pathlib.Path.home() / ".local" / "share" / "pj")
TASKS_DIR = VAULT / "raw" / "tasks"
PJ_TASKS = pathlib.Path(__file__).resolve().parents[2] / "_shared" / "scripts" / "pj-tasks.py"
WT_REGISTRY = pathlib.Path(__file__).resolve().parents[2] / "_shared" / "scripts" / "wt-registry.py"
PJ_REPOS = pathlib.Path(__file__).resolve().parents[2] / "_shared" / "scripts" / "pj-repos.py"
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.S)
COLOR_LINE_RE = re.compile(r'^color:\s*["\']?(#[0-9A-Fa-f]{6})["\']?\s*$', re.M)

# OKLab is roughly perceptually uniform. 0.11 is far enough apart to avoid colors that read as
# variants of one another at sidebar-tab size while leaving ample room for dozens of projects.
# It is a floor, not the objective — see choose_distinct_color for why hue decides among the
# candidates that clear it.
MIN_OKLAB_DISTANCE = 0.11
SAMPLE_COUNT = 8192
TOP_POOL = 32


class SyncError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["newc"],
        help="newc: choose a new color even if one is already saved",
    )
    return parser.parse_args()


def run(*args: str) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True)
    except OSError as exc:
        raise SyncError(f"명령을 실행할 수 없습니다: {args[0]} ({exc})") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise SyncError(f"{' '.join(args[:2])} 실패: {detail}")
    return proc.stdout


def current_location() -> list[tuple[pathlib.Path, pathlib.Path, str]]:
    """[(main repo, worktree, branch)] for where this runs — one entry standing in a worktree,
    one per child standing in the parent of several. Never asks git itself: a combined board's
    parent is not a repo, and `pj-repos.py` is the one place that turns a location into repos."""
    if not PJ_REPOS.is_file():
        raise SyncError(f"pj-repos.py를 찾을 수 없습니다: {PJ_REPOS}")
    proc = subprocess.run([sys.executable, str(PJ_REPOS), "--cwd", os.getcwd()],
                          capture_output=True, text=True)
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SyncError(f"pj-repos.py 출력이 JSON이 아닙니다: {exc}") from exc
    out = []
    for r in rows:
        if r.get("main_repo") and r.get("worktree") and r.get("branch"):
            out.append((pathlib.Path(r["main_repo"]), pathlib.Path(r["worktree"]), r["branch"]))
    if not out:
        raise SyncError("현재 위치에 git worktree가 없습니다 — 보드 워크트리, 또는 결합 프로젝트라면 "
                        "그 워크트리들의 부모 폴더에서 실행하세요")
    return out


def resolve_board_project() -> tuple[dict, dict[str, pathlib.Path]]:
    """(project row, {repo name: main repo path}) for the board standing here."""
    if not PJ_TASKS.is_file():
        raise SyncError(f"pj-tasks.py를 찾을 수 없습니다: {PJ_TASKS}")

    here = current_location()
    here_pairs = {(repo.name, branch) for repo, _, branch in here}
    repos = {repo.name: repo for repo, _, _ in here}

    try:
        projects = json.loads(run(str(PJ_TASKS), "proj-list"))
    except json.JSONDecodeError as exc:
        raise SyncError(f"프로젝트 목록 JSON이 잘못됐습니다: {exc}") from exc

    # A board is the worktree on a project's registered branch, not one particular AI session or
    # cmux surface inside that workspace. Git permits a branch to be checked out in only one normal
    # worktree, and repo + branch also keeps identically named branches in different repos apart.
    # A board owning several repos registers them comma-joined (repo=a,b branch=x,y), one
    # pair per position, so match the (repo, branch) pair rather than the whole string.
    # Standing in a parent means every child must belong to the board — a parent holding one
    # project's backend next to another's frontend is not any board's location.
    def owns(row: dict) -> bool:
        repos_ = [r.strip() for r in str(row.get("repo", "")).split(",")]
        branches = [b.strip() for b in str(row.get("branch", "")).split(",")]
        return here_pairs <= set(zip(repos_, branches))

    matches = [row for row in projects if owns(row)]
    where = ", ".join(f"{repo.name}@{branch}" for repo, _, branch in here)
    if not matches:
        raise SyncError(
            f"현재 위치는 등록된 pj board가 아닙니다 ({where}) — "
            "프로젝트 브랜치의 worktree(결합 프로젝트는 그 부모)에서 pj-board를 먼저 실행하세요"
        )
    if len(matches) > 1:
        names = ", ".join(sorted(row.get("project", "?") for row in matches))
        raise SyncError(f"현재 위치에 여러 pj 프로젝트가 등록돼 있습니다: {names} ({where})")
    project = matches[0].get("project", "")
    if not PROJECT_RE.fullmatch(project):
        raise SyncError(f"프로젝트 이름 형식이 잘못됐습니다: {project!r}")
    return matches[0], repos


def project_task_branches(project: str) -> list[tuple[str, str]]:
    """Started tasks' (repo, branch) pairs, read BEFORE taking index.md's color lock — pj-tasks.py
    takes that lock itself. A task targeting several repos records `repo=a,b branch=x,y`, one
    pair per position; a task that records no repo is the project's single repo (repo "")."""
    try:
        tasks = json.loads(run(str(PJ_TASKS), "list", "--project", project))
    except json.JSONDecodeError as exc:
        raise SyncError(f"태스크 목록 JSON이 잘못됐습니다: {exc}") from exc
    pairs: set[tuple[str, str]] = set()
    for row in tasks:
        branches = [b.strip() for b in str(row.get("branch") or "").split(",") if b.strip()]
        repos_ = [r.strip() for r in str(row.get("repo") or "").split(",") if r.strip()]
        if not branches:
            continue
        if repos_ and len(repos_) == len(branches):
            pairs.update(zip(repos_, branches))
        else:
            pairs.update(("", b) for b in branches)
    return sorted(pairs)


def task_workspace_refs(pairs: list[tuple[str, str]],
                        repos: dict[str, pathlib.Path]) -> list[str]:
    """Resolve currently live task workspaces through the worktree registry, per (repo, branch).
    A pair naming no repo belongs to the board's only repo; with several repos it cannot be
    placed and is skipped rather than guessed."""
    if not WT_REGISTRY.is_file():
        raise SyncError(f"wt-registry.py를 찾을 수 없습니다: {WT_REGISTRY}")
    refs: list[str] = []
    for repo_name, branch in pairs:
        repo = repos.get(repo_name) if repo_name else (
            next(iter(repos.values())) if len(repos) == 1 else None)
        if repo is None:
            continue
        try:
            proc = subprocess.run(
                [
                    str(WT_REGISTRY),
                    "resolve",
                    "--repo",
                    str(repo),
                    "--branch",
                    branch,
                    "--heal",
                ],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise SyncError(f"workspace registry 조회 실패: {branch} ({exc})") from exc
        if proc.returncode != 0:
            # A completed/closed task commonly keeps its branch in the task record but has no live
            # workspace. It is not a connected target and should not block the remaining sync.
            continue
        ref = proc.stdout.strip()
        if re.fullmatch(r"workspace:[0-9]+", ref) and ref not in refs:
            refs.append(ref)
    return refs


def read_frontmatter(path: pathlib.Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SyncError(f"project.md를 읽을 수 없습니다: {path} ({exc})") from exc
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise SyncError(f"project.md frontmatter가 없거나 잘못됐습니다: {path}")
    return text, match.group(1)


def color_from_frontmatter(frontmatter: str) -> str | None:
    match = COLOR_LINE_RE.search(frontmatter)
    return match.group(1).upper() if match else None


def read_project_color_from_text(text: str) -> str | None:
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise SyncError("project.md frontmatter가 없거나 잘못됐습니다")
    return color_from_frontmatter(match.group(1))


def read_project_color(path: pathlib.Path) -> str | None:
    text, _ = read_frontmatter(path)
    return read_project_color_from_text(text)


def finished_projects(projects: list[dict] | None = None) -> set[str]:
    """Projects recorded 완료 in index.md. Their colors are released: a finished board is on its
    way out, and holding every past color forever is what fills the color space until no new
    project can be told apart. project.md itself is left alone — the color stays as history."""
    if projects is None:
        try:
            projects = json.loads(run(str(PJ_TASKS), "proj-list"))
        except json.JSONDecodeError as exc:
            raise SyncError(f"프로젝트 목록 JSON이 잘못됐습니다: {exc}") from exc
    return {row.get("project", "") for row in projects if row.get("status") == "완료"}


def all_project_colors(
    tasks_dir: pathlib.Path = TASKS_DIR,
    exclude_project: str | None = None,
    exclude_projects: set[str] | None = None,
) -> list[str]:
    """Colors currently taken: every project.md color except this project's and those of
    finished projects."""
    skip = set(exclude_projects or ())
    if exclude_project:
        skip.add(exclude_project)
    colors: list[str] = []
    for path in sorted(tasks_dir.glob("*/project.md")):
        if path.parent.name in skip:
            continue
        try:
            color = read_project_color(path)
        except SyncError:
            continue
        if color:
            colors.append(color)
    return colors


@contextlib.contextmanager
def project_color_lock(tasks_dir: pathlib.Path = TASKS_DIR):
    """Serialize selection + persistence so two new boards cannot pick look-alike colors."""
    lock_target = tasks_dir / "index.md"
    try:
        with lock_target.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
    except OSError as exc:
        raise SyncError(f"프로젝트 색상 잠금 실패: {lock_target} ({exc})") from exc


def srgb_channel(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def hex_to_oklab(color: str) -> tuple[float, float, float]:
    if not COLOR_RE.fullmatch(color):
        raise ValueError(f"invalid color: {color}")
    rgb = [int(color[i : i + 2], 16) / 255.0 for i in (1, 3, 5)]
    r, g, b = (srgb_channel(value) for value in rgb)

    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    # The LMS values are non-negative for sRGB, so exponentiation is a Python 3.9-compatible
    # cube root (math.cbrt only arrived in 3.11).
    l_, m_, s_ = l ** (1.0 / 3.0), m ** (1.0 / 3.0), s ** (1.0 / 3.0)

    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def oklab_distance(left: str, right: str) -> float:
    a = hex_to_oklab(left)
    b = hex_to_oklab(right)
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def random_hex(rng: random.Random) -> str:
    # Keep saturation and lightness in a sidebar-friendly band: neither grey/muddy nor so close to
    # black/white that the colored tab loses its identity.
    hue = rng.random()
    saturation = rng.uniform(0.58, 0.88)
    lightness = rng.uniform(0.34, 0.61)
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"


def hue_degrees(color: str) -> float:
    rgb = [int(color[i : i + 2], 16) / 255.0 for i in (1, 3, 5)]
    return colorsys.rgb_to_hls(*rgb)[0] * 360.0


def hue_gap(hue: float, others: list[float]) -> float:
    """Smallest circular hue distance from `hue` to any of `others` (360 when there are none)."""
    if not others:
        return 360.0
    return min(min(abs(hue - other), 360.0 - abs(hue - other)) for other in others)


def choose_distinct_color(existing: list[str], rng: random.Random | None = None) -> str:
    """Pick a color at least MIN_OKLAB_DISTANCE from every existing color, preferring the hue
    region the existing colors leave emptiest.

    Distance alone is the wrong tiebreaker: the candidate farthest from every existing color in
    OKLab kept landing in the same blue-cyan band (the band is wide in lightness and chroma, so it
    stays "farthest" even after several cyans exist). What the eye separates tabs by is hue, so
    among the distinct-enough candidates the widest hue gap wins, with a small random pool so a
    re-roll (`newc`) still varies."""
    rng = rng or random.SystemRandom()
    normalized = sorted({color.upper() for color in existing if COLOR_RE.fullmatch(color)})
    existing_hues = [hue_degrees(color) for color in normalized]
    scored: list[tuple[float, str]] = []

    for _ in range(SAMPLE_COUNT):
        candidate = random_hex(rng)
        if candidate in normalized:
            continue
        nearest = min((oklab_distance(candidate, color) for color in normalized), default=1.0)
        scored.append((nearest, candidate))

    eligible = [item for item in scored if item[0] >= MIN_OKLAB_DISTANCE]
    if not eligible:
        # The live color space is crowded: with two dozen boards open, no candidate clears the
        # floor. Refusing here left a board with no color at all — which is worse than the best
        # available one, and the floor is a target for legibility, not a gate. Take the farthest
        # candidates instead and let the hue rule pick among them; the caller reports the
        # achieved distance so the shortfall is visible, never silent.
        scored.sort(key=lambda item: item[0], reverse=True)
        eligible = scored[: max(TOP_POOL, 1)]

    by_hue_gap = sorted(eligible, key=lambda item: hue_gap(hue_degrees(item[1]), existing_hues),
                        reverse=True)
    pool = by_hue_gap[: min(TOP_POOL, len(by_hue_gap))]
    return rng.choice(pool)[1]


def nearest_distance(color: str, others: list[str]) -> float:
    return min((oklab_distance(color, other) for other in others), default=1.0)


def is_distinct(color: str, others: list[str]) -> bool:
    return all(oklab_distance(color, other) >= MIN_OKLAB_DISTANCE for other in others)


def replace_project_color(text: str, color: str) -> str:
    if not COLOR_RE.fullmatch(color):
        raise SyncError(f"색상 형식이 잘못됐습니다: {color!r}")
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise SyncError("project.md frontmatter가 없거나 잘못됐습니다")

    frontmatter = match.group(1)
    line = f'color: "{color.upper()}"'
    if re.search(r"^color:.*$", frontmatter, re.M):
        updated = re.sub(r"^color:.*$", line, frontmatter, count=1, flags=re.M)
    else:
        lines = frontmatter.splitlines()
        insert_at = 0
        for key in ("repo:", "project:"):
            for index, current in enumerate(lines):
                if current.startswith(key):
                    insert_at = index + 1
                    break
            if insert_at:
                break
        lines.insert(insert_at, line)
        updated = "\n".join(lines)
    return text[: match.start(1)] + updated + text[match.end(1) :]


def write_project_color(path: pathlib.Path, color: str) -> None:
    try:
        with path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            text = handle.read()
            updated = replace_project_color(text, color)
            handle.seek(0)
            handle.write(updated)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, SyncError) as exc:
        if isinstance(exc, SyncError):
            raise
        raise SyncError(f"project.md 색상 저장 실패: {path} ({exc})") from exc


def current_workspace_color(workspace: str | None = None) -> str | None:
    args = ["cmux", "sidebar-state"]
    if workspace:
        args.extend(["--workspace", workspace])
    state = run(*args)
    for line in state.splitlines():
        if line.startswith("color="):
            value = line.removeprefix("color=").strip()
            # cmux emits the literal "none" sentinel for unset fields (pr, ports, progress, color),
            # not an empty string — a workspace that never had a color reads back as "none".
            if not value or value == "none":
                return None
            if not COLOR_RE.fullmatch(value):
                raise SyncError(f"cmux가 예상하지 못한 색상 값을 반환했습니다: {value!r}")
            return value.upper()
    raise SyncError("cmux sidebar-state에서 color를 찾지 못했습니다")


def apply_workspace_color(color: str, workspace: str | None = None) -> None:
    args = ["cmux", "workspace-action", "--action", "set-color", "--color", color]
    if workspace:
        args.extend(["--workspace", workspace])
    run(*args)


def restore_workspace_color(color: str | None, workspace: str | None = None) -> None:
    if color:
        apply_workspace_color(color, workspace)
    else:
        args = ["cmux", "workspace-action", "--action", "clear-color"]
        if workspace:
            args.extend(["--workspace", workspace])
        run(*args)


def rollback_workspace_colors(
    changed: list[str | None], previous: dict[str | None, str | None]
) -> None:
    failures: list[str] = []
    for workspace in reversed(changed):
        try:
            restore_workspace_color(previous[workspace], workspace)
        except SyncError as exc:
            failures.append(f"{workspace or 'board'}: {exc}")
    if failures:
        raise SyncError("workspace 색상 복원 실패: " + "; ".join(failures))


def apply_color_transaction(
    color: str, task_workspaces: list[str]
) -> tuple[dict[str | None, str | None], list[str | None]]:
    """Apply to board + every live task workspace, rolling back if any target fails."""
    targets: list[str | None] = [None, *task_workspaces]
    previous: dict[str | None, str | None] = {}
    changed: list[str | None] = []
    try:
        for workspace in targets:
            old_color = current_workspace_color(workspace)
            previous[workspace] = old_color
            if old_color == color:
                continue
            apply_workspace_color(color, workspace)
            changed.append(workspace)
    except SyncError as apply_error:
        try:
            rollback_workspace_colors(changed, previous)
        except SyncError as rollback_error:
            raise SyncError(f"색상 적용과 복원 모두 실패했습니다: {apply_error}; {rollback_error}")
        raise
    return previous, changed


def main() -> int:
    args = parse_args()
    try:
        project_row, repos = resolve_board_project()
        project = project_row["project"]
        project_file = TASKS_DIR / project / "project.md"
        task_branches = project_task_branches(project)
        # Anything that shells out to pj-tasks.py must run BEFORE the color lock: pj-tasks.py takes
        # index.md's flock itself, and the color lock is that same file — calling it inside is a
        # self-deadlock that also stalls every other pj session waiting on the lock.
        finished = finished_projects()
        with project_color_lock():
            stored = read_project_color(project_file)
            others = all_project_colors(exclude_project=project, exclude_projects=finished)
            forbidden = others + ([stored] if stored else [])
            if args.mode == "newc" or not stored:
                color, generated = choose_distinct_color(forbidden, random.SystemRandom()), True
            elif is_distinct(stored, others):
                color, generated = stored, False
            else:
                # The stored color is too close to a neighbour. Replace it only if a strictly
                # better one exists: in a crowded space every color is "too close", and
                # regenerating on each run would make the board's color flap for no gain.
                candidate = choose_distinct_color(forbidden, random.SystemRandom())
                if nearest_distance(candidate, others) > nearest_distance(stored, others):
                    color, generated = candidate, True
                else:
                    color, generated = stored, False
            assert color is not None
            achieved = nearest_distance(color, others)

            task_workspaces = task_workspace_refs(task_branches, repos)
            previous, changed = apply_color_transaction(color, task_workspaces)
            try:
                write_project_color(project_file, color)
            except SyncError:
                try:
                    rollback_workspace_colors(changed, previous)
                except SyncError as rollback_error:
                    raise SyncError(
                        f"project.md 저장과 workspace 색상 복원 모두 실패했습니다: {rollback_error}"
                    )
                raise

        mode = "generated" if generated else "reused"
        crowded = achieved < MIN_OKLAB_DISTANCE
        print(
            f"PJ_COLOR=ok project={project} color={color} mode={mode} "
            f"task_workspaces={len(task_workspaces)} distance={achieved:.3f}"
            + (f" floor={MIN_OKLAB_DISTANCE:.3f} crowded=yes" if crowded else "")
        )
        return 0
    except SyncError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
