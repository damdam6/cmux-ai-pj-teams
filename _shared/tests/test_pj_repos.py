"""pj-repos.py — the one place that answers "which repos does this refer to?".

Two halves, tested differently on purpose:
  - `--cwd` is pure git, so it runs against real temporary repos.
  - `--project` / `--slug` read the task list, so `tasks_json` is stubbed with record shapes.
    Both the OLD single-value shape and the multi-repo shape are exercised, because this script
    ships before the task-list schema widens and must not be sequenced behind it.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-repos.py"
SPEC = importlib.util.spec_from_file_location("pj_repos", SCRIPT)
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def git(repo, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


def make_repo(root: pathlib.Path, name: str) -> pathlib.Path:
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "init")
    return repo


@pytest.fixture
def repos(tmp_path):
    """Two repos plus a combined worktree parent, the real target layout."""
    be = make_repo(tmp_path / "repos", "example-backend")
    fe = make_repo(tmp_path / "repos", "example-frontend")
    parent = tmp_path / "worktrees" / "compact-context"
    parent.mkdir(parents=True)
    git(be, "worktree", "add", "-q", "-b", "feat/compact-context", str(parent / "backend"))
    git(fe, "worktree", "add", "-q", "-b", "feat/compact-context", str(parent / "frontend"))
    return {"be": be, "fe": fe, "parent": parent}


# ---------------------------------------------------------------- as_list


@pytest.mark.parametrize("given,want", [
    (None, []),
    ("", []),
    ("example-backend", ["example-backend"]),
    ("example-backend,example-frontend", ["example-backend", "example-frontend"]),
    (" be , fe ", ["be", "fe"]),
    (["be", "fe"], ["be", "fe"]),
])
def test_as_list_accepts_both_shapes(given, want):
    assert M.as_list(given) == want


# ---------------------------------------------------------------- --cwd


def test_cwd_in_a_worktree_answers_with_itself(repos):
    found, reason = M.from_cwd(str(repos["parent"] / "backend"))
    assert reason == ""
    assert len(found) == 1
    assert found[0]["repo"] == "example-backend"
    assert found[0]["branch"] == "feat/compact-context"
    assert found[0]["linked"] is True


def test_cwd_in_the_main_checkout_is_not_linked(repos):
    found, _ = M.from_cwd(str(repos["be"]))
    assert len(found) == 1
    assert found[0]["branch"] == "main"
    assert found[0]["linked"] is False


def test_cwd_in_a_subdirectory_of_a_worktree_still_resolves(repos):
    sub = repos["parent"] / "backend" / "src" / "deep"
    sub.mkdir(parents=True)
    found, _ = M.from_cwd(str(sub))
    assert len(found) == 1
    # the worktree ROOT, not the subdirectory we asked from
    assert found[0]["worktree"] == str(repos["parent"] / "backend")


def test_cwd_in_the_combined_parent_finds_every_child(repos):
    found, reason = M.from_cwd(str(repos["parent"]))
    assert reason == ""
    assert [e["repo"] for e in found] == ["example-backend", "example-frontend"]
    assert {e["branch"] for e in found} == {"feat/compact-context"}
    # each child points at its OWN main repo
    assert {e["main_repo"] for e in found} == {str(repos["be"]), str(repos["fe"])}


def test_cwd_parent_with_one_child_gives_one_entry(tmp_path):
    """A single-repo task is the same layout with one child — N=1, not a special case."""
    be = make_repo(tmp_path / "repos", "example-backend")
    parent = tmp_path / "worktrees" / "solo"
    parent.mkdir(parents=True)
    git(be, "worktree", "add", "-q", "-b", "feat/solo", str(parent / "backend"))
    found, _ = M.from_cwd(str(parent))
    assert len(found) == 1 and found[0]["repo"] == "example-backend"


def test_cwd_ignores_non_repo_and_hidden_children(repos):
    (repos["parent"] / "notes").mkdir()
    (repos["parent"] / ".cache").mkdir()
    found, _ = M.from_cwd(str(repos["parent"]))
    assert len(found) == 2


def test_cwd_does_not_scan_deeper_than_one_level(tmp_path):
    """A grandchild repo must not be reported — otherwise any directory above a checkout
    would answer with repos it has nothing to do with."""
    outer = tmp_path / "outer"
    (outer / "mid").mkdir(parents=True)
    make_repo(outer / "mid", "buried")
    found, reason = M.from_cwd(str(outer))
    assert found == []
    assert "워크트리" in reason


def test_cwd_on_a_plain_directory_is_empty_with_a_reason(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    found, reason = M.from_cwd(str(empty))
    assert found == [] and reason


def test_cwd_on_a_missing_path_is_empty_with_a_reason(tmp_path):
    found, reason = M.from_cwd(str(tmp_path / "ghost"))
    assert found == [] and "디렉터리가 아닙니다" in reason


# ---------------------------------------------------------------- --project


@pytest.fixture
def roots(repos, monkeypatch):
    monkeypatch.setattr(M, "known_repo_roots",
                        lambda hint=None: {"example-backend": str(repos["be"]),
                                           "example-frontend": str(repos["fe"])})
    return repos


def test_project_old_single_repo_shape(roots, monkeypatch):
    monkeypatch.setattr(M, "tasks_json", lambda *a: {
        "meta": {"repo": "example-backend", "branch": "feat/compact-context"}})
    found, reason = M.from_project("ctx-compact", None)
    assert reason == ""
    assert len(found) == 1
    assert found[0]["worktree"] == str(roots["parent"] / "backend")


def test_project_multi_repo_shape_pairs_positionally(roots, monkeypatch):
    monkeypatch.setattr(M, "tasks_json", lambda *a: {"meta": {
        "repo": "example-backend,example-frontend",
        "branch": "feat/compact-context,feat/compact-context"}})
    found, _ = M.from_project("ctx-compact", None)
    assert [(e["repo"], e["worktree"]) for e in found] == [
        ("example-backend", str(roots["parent"] / "backend")),
        ("example-frontend", str(roots["parent"] / "frontend")),
    ]


def test_project_branch_with_no_worktree_resolves_to_none(roots, monkeypatch):
    monkeypatch.setattr(M, "tasks_json", lambda *a: {
        "meta": {"repo": "example-backend", "branch": "feat/never-checked-out"}})
    found, _ = M.from_project("x", None)
    assert found[0]["worktree"] is None
    assert found[0]["branch"] == "feat/never-checked-out"


def test_project_missing_repo_on_disk_is_reported_not_dropped_silently(repos, monkeypatch, capsys):
    monkeypatch.setattr(M, "known_repo_roots",
                        lambda hint=None: {"example-backend": str(repos["be"])})
    monkeypatch.setattr(M, "tasks_json", lambda *a: {"meta": {
        "repo": "example-backend,example-ghost", "branch": "feat/compact-context,feat/x"}})
    found, reason = M.from_project("x", None)
    assert [e["repo"] for e in found] == ["example-backend"]
    assert "example-ghost" in capsys.readouterr().err


def test_project_unknown_is_empty_with_a_reason(monkeypatch):
    monkeypatch.setattr(M, "tasks_json", lambda *a: None)
    found, reason = M.from_project("ghost", None)
    assert found == [] and "찾을 수 없습니다" in reason


def test_project_with_no_repo_recorded_is_empty(roots, monkeypatch):
    monkeypatch.setattr(M, "tasks_json", lambda *a: {"meta": {"branch": "feat/x"}})
    found, reason = M.from_project("x", None)
    assert found == [] and "repo" in reason


# ---------------------------------------------------------------- --slug


def test_slug_defaults_to_every_repo_of_its_project(roots, monkeypatch):
    monkeypatch.setattr(M, "tasks_json", lambda *a: {
        "slug": "compact-context", "repo": "example-backend,example-frontend"})
    found, reason = M.from_slug("compact-context", None)
    assert reason == ""
    assert [e["repo"] for e in found] == ["example-backend", "example-frontend"]
    # branch/worktree recovered from the {prefix}/{slug} convention even before `start`
    assert {e["branch"] for e in found} == {"feat/compact-context"}
    assert all(e["worktree"] for e in found)


def test_slug_repos_subset_narrows_the_targets(roots, monkeypatch):
    monkeypatch.setattr(M, "tasks_json", lambda *a: {
        "slug": "compact-context", "repo": "example-backend,example-frontend",
        "repos": "example-frontend"})
    found, _ = M.from_slug("compact-context", None)
    assert [e["repo"] for e in found] == ["example-frontend"]


def test_slug_subset_is_intersected_not_trusted(roots, monkeypatch):
    """A stale `repos` entry must not add a repo the project does not own."""
    monkeypatch.setattr(M, "tasks_json", lambda *a: {
        "slug": "compact-context", "repo": "example-backend",
        "repos": "example-backend,example-frontend"})
    found, _ = M.from_slug("compact-context", None)
    assert [e["repo"] for e in found] == ["example-backend"]


def test_slug_subset_disjoint_from_project_is_refused(roots, monkeypatch):
    monkeypatch.setattr(M, "tasks_json", lambda *a: {
        "slug": "x", "repo": "example-backend", "repos": "example-frontend"})
    found, reason = M.from_slug("x", None)
    assert found == [] and "겹치지" in reason


def test_slug_single_branch_applies_to_its_one_repo(roots, monkeypatch):
    monkeypatch.setattr(M, "tasks_json", lambda *a: {
        "slug": "compact-context", "repo": "example-backend",
        "branch": "feat/compact-context"})
    found, _ = M.from_slug("compact-context", None)
    assert found[0]["worktree"] == str(roots["parent"] / "backend")


def test_slug_not_in_the_list_is_empty_with_a_reason(monkeypatch):
    monkeypatch.setattr(M, "tasks_json", lambda *a: None)
    found, reason = M.from_slug("ghost", None)
    assert found == [] and "slug" in reason


# ---------------------------------------------------------------- cli contract


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", str(SCRIPT), *args], capture_output=True, text=True)


def test_cli_prints_json_and_exits_0(repos):
    p = run_cli("--cwd", str(repos["parent"]))
    assert p.returncode == 0
    assert p.stdout.lstrip().startswith("[")


def test_cli_tsv_is_one_line_per_repo(repos):
    p = run_cli("--cwd", str(repos["parent"]), "--format", "tsv")
    assert p.returncode == 0
    lines = p.stdout.strip().splitlines()
    assert len(lines) == 2
    assert all(len(l.split("\t")) == 5 for l in lines)


def test_cli_empty_exits_3_with_empty_json_on_stdout(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    p = run_cli("--cwd", str(empty))
    assert p.returncode == 3
    assert p.stdout.strip() == "[]"
    assert p.stderr.strip()


def test_cli_requires_one_mode():
    assert run_cli().returncode == 2


def test_cli_modes_are_mutually_exclusive(repos):
    p = run_cli("--cwd", str(repos["parent"]), "--project", "x")
    assert p.returncode == 2
