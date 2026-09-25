"""wt-registry.py must hold worktrees that live OUTSIDE the repo alongside the nested ones.

`path` used to be repo-relative by construction. The pj family now places worktrees at
`{root}/{name}/{repo-folder}`, so absolute entries appear — and the old relative ones stay,
because existing worktrees are not being moved. Both must prune correctly, which is the one
operation that resolves `path` against the repo.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "wt-registry.py"
SPEC = importlib.util.spec_from_file_location("wt_registry", SCRIPT)
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def git(repo, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repos" / "example-backend"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "init")
    return r


def ns(**kw):
    import argparse
    base = {"repo": None, "branch": None, "base": None, "path": None,
            "workspace_name": None, "workspace_id": None, "ticket": None}
    base.update(kw)
    return argparse.Namespace(**base)


def entries(repo) -> list[dict]:
    return json.loads(M.registry_path(str(repo)).read_text())["worktrees"]


def test_absolute_path_round_trips(repo, tmp_path, capsys):
    wt = tmp_path / "worktrees" / "compact-context" / "backend"
    git(repo, "worktree", "add", "-q", "-b", "feat/compact-context", str(wt))
    M.cmd_add(ns(repo=str(repo), branch="feat/compact-context", base="main",
                 path=str(wt), workspace_name="BE+FE/ctx"))
    capsys.readouterr()
    assert entries(repo)[0]["path"] == str(wt)


def test_prune_keeps_a_live_external_worktree(repo, tmp_path, capsys):
    wt = tmp_path / "worktrees" / "compact-context" / "backend"
    git(repo, "worktree", "add", "-q", "-b", "feat/compact-context", str(wt))
    M.cmd_add(ns(repo=str(repo), branch="feat/compact-context", base="main",
                 path=str(wt), workspace_name="BE+FE/ctx"))
    M.cmd_prune(ns(repo=str(repo)))
    assert capsys.readouterr().out.strip().endswith("kept 1, pruned 0")


def test_prune_drops_a_removed_external_worktree(repo, tmp_path, capsys):
    wt = tmp_path / "worktrees" / "gone" / "backend"
    git(repo, "worktree", "add", "-q", "-b", "feat/gone", str(wt))
    M.cmd_add(ns(repo=str(repo), branch="feat/gone", base="main",
                 path=str(wt), workspace_name="BE/gone"))
    git(repo, "worktree", "remove", str(wt))
    M.cmd_prune(ns(repo=str(repo)))
    assert entries(repo) == []


def test_relative_and_absolute_entries_coexist(repo, tmp_path, capsys):
    """The migration story: old nested entries are not rewritten, so prune must judge both."""
    nested = repo / ".worktrees" / "legacy"
    git(repo, "worktree", "add", "-q", "-b", "feat/legacy", str(nested))
    external = tmp_path / "worktrees" / "modern" / "backend"
    git(repo, "worktree", "add", "-q", "-b", "feat/modern", str(external))
    M.cmd_add(ns(repo=str(repo), branch="feat/legacy", base="main",
                 path=".worktrees/legacy", workspace_name="BE/legacy"))
    M.cmd_add(ns(repo=str(repo), branch="feat/modern", base="main",
                 path=str(external), workspace_name="BE/modern"))
    M.cmd_prune(ns(repo=str(repo)))
    capsys.readouterr()
    assert {e["branch"] for e in entries(repo)} == {"feat/legacy", "feat/modern"}


def test_registry_file_stays_in_the_repo(repo, tmp_path, capsys):
    """Its location is unchanged — worktree registry reconciliation and registry maintenance read it from there."""
    wt = tmp_path / "worktrees" / "x" / "backend"
    git(repo, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    M.cmd_add(ns(repo=str(repo), branch="feat/x", base="main", path=str(wt),
                 workspace_name="BE/x"))
    capsys.readouterr()
    assert M.registry_path(str(repo)) == repo / ".worktrees" / "registry.json"
    assert M.registry_path(str(repo)).is_file()
