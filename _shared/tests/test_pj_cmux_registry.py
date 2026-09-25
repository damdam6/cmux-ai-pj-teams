"""pj-cmux.py worktree registration — the bug this guards is a SILENT one.

The repo used to be carved out of the worktree path (`split("/.worktrees/")[0]`). Once
worktrees moved outside the repo that substring stops matching, so the registration was skipped
with no error: nothing fails until worktree registry reconciliation later cannot find the workspace. These tests run
with TEST off and real git repos, because the whole point is that git — not the path — answers
which repo a worktree belongs to.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-cmux.py"


@pytest.fixture
def M(monkeypatch):
    """A fresh module with TEST off, so main_repo_of really asks git."""
    monkeypatch.delenv("PJ_CMUX_TEST", raising=False)
    spec = importlib.util.spec_from_file_location("pj_cmux_live", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.TEST is False
    return mod


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


def registry(repo: pathlib.Path) -> list[dict]:
    f = repo / ".worktrees" / "registry.json"
    return json.loads(f.read_text())["worktrees"] if f.is_file() else []


# ---------------------------------------------------------------- main_repo_of


def test_main_repo_of_an_external_worktree(M, tmp_path):
    repo = make_repo(tmp_path / "repos", "example-backend")
    wt = tmp_path / "worktrees" / "ctx" / "backend"
    git(repo, "worktree", "add", "-q", "-b", "feat/ctx", str(wt))
    assert pathlib.Path(M.main_repo_of(str(wt))).resolve() == repo.resolve()


def test_main_repo_of_a_nested_worktree(M, tmp_path):
    repo = make_repo(tmp_path / "repos", "example-backend")
    wt = repo / ".worktrees" / "legacy"
    git(repo, "worktree", "add", "-q", "-b", "feat/legacy", str(wt))
    assert pathlib.Path(M.main_repo_of(str(wt))).resolve() == repo.resolve()


def test_main_repo_of_a_non_repo_is_empty(M, tmp_path):
    (tmp_path / "plain").mkdir()
    assert M.main_repo_of(str(tmp_path / "plain")) == ""


# ---------------------------------------------------------------- register_worktrees


def test_external_worktree_is_registered_with_an_absolute_path(M, tmp_path):
    """The regression: this used to record nothing at all."""
    repo = make_repo(tmp_path / "repos", "example-backend")
    wt = tmp_path / "worktrees" / "ctx" / "backend"
    git(repo, "worktree", "add", "-q", "-b", "feat/ctx", str(wt))
    M.register_worktrees({"worktree": str(wt), "branch": "feat/ctx",
                          "base_branch": "main", "name": "BE/ctx"}, "workspace:3")
    rows = registry(repo)
    assert len(rows) == 1
    assert rows[0]["path"] == str(wt)
    assert rows[0]["branch"] == "feat/ctx"
    assert rows[0]["workspaceName"] == "BE/ctx"


def test_nested_worktree_keeps_the_repo_relative_path(M, tmp_path):
    """Unchanged for the legacy layout — the existing 80 worktrees are not being rewritten."""
    repo = make_repo(tmp_path / "repos", "example-backend")
    wt = repo / ".worktrees" / "legacy"
    git(repo, "worktree", "add", "-q", "-b", "feat/legacy", str(wt))
    M.register_worktrees({"worktree": str(wt), "branch": "feat/legacy",
                          "base_branch": "main", "name": "BE/legacy"}, "workspace:1")
    assert registry(repo)[0]["path"] == ".worktrees/legacy"


def test_combined_task_registers_once_in_each_repo(M, tmp_path):
    be = make_repo(tmp_path / "repos", "example-backend")
    fe = make_repo(tmp_path / "repos", "example-frontend")
    parent = tmp_path / "worktrees" / "compact-context"
    git(be, "worktree", "add", "-q", "-b", "feat/compact-context", str(parent / "backend"))
    git(fe, "worktree", "add", "-q", "-b", "feat/compact-context", str(parent / "frontend"))
    M.register_worktrees({
        "worktree": str(parent), "branch": "", "base_branch": "main",
        "name": "BE+FE/ctx",
        "repo_wt": [{"repo": "example-backend", "worktree": str(parent / "backend"),
                     "branch": "feat/compact-context"},
                    {"repo": "example-frontend", "worktree": str(parent / "frontend"),
                     "branch": "feat/compact-context"}]}, "workspace:7")
    for repo, child in ((be, "backend"), (fe, "frontend")):
        rows = registry(repo)
        assert len(rows) == 1, repo
        assert rows[0]["path"] == str(parent / child)
        assert rows[0]["workspaceName"] == "BE+FE/ctx"   # same workspace, two registries


def test_a_pair_without_a_branch_is_skipped(M, tmp_path):
    repo = make_repo(tmp_path / "repos", "example-backend")
    M.register_worktrees({"worktree": str(repo), "branch": "", "name": "BE/x"}, "workspace:1")
    assert registry(repo) == []


def test_an_unresolvable_worktree_is_reported_not_crashed(M, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    M.register_worktrees({"worktree": str(plain), "branch": "feat/x", "name": "X/x"},
                         "workspace:1")
    assert "repo 를 찾지 못했습니다" in capsys.readouterr().err


# ---------------------------------------------------------------- worktree_pairs


def test_worktree_pairs_falls_back_to_the_flat_fields(M):
    """Single-repo payloads keep the shape they always had."""
    assert M.worktree_pairs({"worktree": "/w", "branch": "feat/x"}) == [("/w", "feat/x")]


def test_worktree_pairs_prefers_repo_wt_when_present(M):
    pairs = M.worktree_pairs({"worktree": "/parent", "branch": "",
                              "repo_wt": [{"worktree": "/parent/be", "branch": "feat/x"},
                                          {"worktree": "/parent/fe", "branch": "feat/y"}]})
    assert pairs == [("/parent/be", "feat/x"), ("/parent/fe", "feat/y")]
