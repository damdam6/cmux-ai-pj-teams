"""create-worktree.sh — the legacy nested layout must not move, and the external layout
({root}/{name}/{wt-folder}) must place several repos side by side under one parent.

The legacy assertions are the point of this file: four skill families document
`{repo}/.worktrees/{name}`, so the external layout is an opt-in that must not change the
default by accident.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "create-worktree.sh"


def git(repo: pathlib.Path, *args: str) -> str:
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


def run(*args: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          cwd=str(cwd) if cwd else None)


def result_of(out: str) -> dict[str, str]:
    body = re.search(r"---WORKTREE_RESULT---\n(.*?)---END_WORKTREE_RESULT---", out, re.S)
    assert body, out
    return dict(line.split("=", 1) for line in body.group(1).strip().splitlines())


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repos", "example-backend")


# ---------------------------------------------------------------- legacy layout


def test_legacy_layout_unchanged(repo):
    p = run("--name", "item-search", "--base", "main", cwd=repo)
    assert p.returncode == 0, p.stderr
    r = result_of(p.stdout)
    assert r["WORKTREE_ABS"] == str(repo / ".worktrees" / "item-search")
    assert r["BRANCH"] == "feature/item-search"
    assert r["LAYOUT"] == "nested"
    assert r["MODE"] == "create"
    assert (repo / ".worktrees" / "item-search" / ".git").exists()


def test_legacy_ticket_and_prefix_unchanged(repo):
    p = run("--name", "item-search", "--ticket", "TASK-1", "--prefix", "fix",
            "--base", "main", cwd=repo)
    assert p.returncode == 0, p.stderr
    assert result_of(p.stdout)["BRANCH"] == "fix/TASK-1-item-search"


def test_existing_branch_still_exits_4_and_now_names_the_way_out(repo):
    git(repo, "branch", "feature/taken")
    p = run("--name", "taken", "--base", "main", cwd=repo)
    assert p.returncode == 4
    assert "--attach" in p.stderr


# ---------------------------------------------------------------- external layout


def test_external_layout_nests_under_root_name_folder(tmp_path, repo):
    root = tmp_path / "worktrees"
    p = run("--name", "compact-context", "--base", "main", "--root", str(root),
            "--wt-folder", "backend", cwd=repo)
    assert p.returncode == 0, p.stderr
    r = result_of(p.stdout)
    assert r["WORKTREE_ABS"] == str(root / "compact-context" / "backend")
    assert r["LAYOUT"] == "external"
    assert r["REPO"] == "example-backend"
    # outside the repo, and the repo keeps only the admin metadata
    assert not (repo / ".worktrees").exists()


def test_two_repos_share_one_parent(tmp_path, repo):
    fe = make_repo(tmp_path / "repos", "example-frontend")
    root = tmp_path / "worktrees"
    for target, folder in ((repo, "backend"), (fe, "frontend")):
        p = run("--name", "compact-context", "--base", "main", "--root", str(root),
                "--wt-folder", folder, "--repo", str(target))
        assert p.returncode == 0, p.stderr
    parent = root / "compact-context"
    assert sorted(c.name for c in parent.iterdir()) == ["backend", "frontend"]
    # each child resolves to its OWN repo
    for folder, want in (("backend", repo), ("frontend", fe)):
        common = git(parent / folder, "rev-parse", "--path-format=absolute",
                     "--git-common-dir").strip()
        assert pathlib.Path(common).resolve() == (want / ".git").resolve()
    # the shared parent is deliberately NOT a git repo
    bare = subprocess.run(["git", "-C", str(parent), "rev-parse", "--git-dir"],
                          capture_output=True, text=True)
    assert bare.returncode != 0


def test_wt_folder_defaults_to_repo_folder_name(tmp_path, repo):
    root = tmp_path / "worktrees"
    p = run("--name", "solo-task", "--base", "main", "--root", str(root), cwd=repo)
    assert p.returncode == 0, p.stderr
    assert result_of(p.stdout)["WORKTREE_ABS"] == str(
        root / "solo-task" / "example-backend")


def test_repo_flag_works_from_outside_any_repo(tmp_path, repo):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    root = tmp_path / "worktrees"
    p = run("--name", "from-outside", "--base", "main", "--root", str(root),
            "--wt-folder", "backend", "--repo", str(repo), cwd=outside)
    assert p.returncode == 0, p.stderr
    assert result_of(p.stdout)["MAIN_REPO"] == str(repo)


def test_without_repo_flag_outside_any_repo_exits_3(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    p = run("--name", "nope", cwd=outside)
    assert p.returncode == 3


# ---------------------------------------------------------------- attach mode


def test_attach_checks_out_existing_branch(tmp_path, repo):
    git(repo, "branch", "feat/context-compaction")
    root = tmp_path / "worktrees"
    p = run("--name", "ctx-compact", "--root", str(root), "--wt-folder", "backend",
            "--attach", "feat/context-compaction", cwd=repo)
    assert p.returncode == 0, p.stderr
    r = result_of(p.stdout)
    assert r["BRANCH"] == "feat/context-compaction"
    assert r["MODE"] == "attach"
    wt = root / "ctx-compact" / "backend"
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feat/context-compaction"


def test_attach_ignores_prefix_and_ticket(tmp_path, repo):
    git(repo, "branch", "feature/cpq")
    root = tmp_path / "worktrees"
    p = run("--name", "board", "--root", str(root), "--attach", "feature/cpq",
            "--prefix", "fix", "--ticket", "TASK-9", cwd=repo)
    assert p.returncode == 0, p.stderr
    assert result_of(p.stdout)["BRANCH"] == "feature/cpq"


def test_attach_missing_branch_exits_5(tmp_path, repo):
    p = run("--name", "board", "--root", str(tmp_path / "w"), "--attach", "feat/ghost",
            cwd=repo)
    assert p.returncode == 5
    assert "does not exist" in p.stderr


def test_attach_already_checked_out_exits_5_and_names_the_holder(tmp_path, repo):
    git(repo, "branch", "feat/taken")
    root = tmp_path / "worktrees"
    first = run("--name", "one", "--root", str(root), "--attach", "feat/taken", cwd=repo)
    assert first.returncode == 0, first.stderr
    second = run("--name", "two", "--root", str(root), "--attach", "feat/taken", cwd=repo)
    assert second.returncode == 5
    assert str(root / "one") in second.stderr


def test_attach_refuses_the_branch_the_main_checkout_holds(tmp_path, repo):
    p = run("--name", "board", "--root", str(tmp_path / "w"), "--attach", "main", cwd=repo)
    assert p.returncode == 5
    assert str(repo) in p.stderr


# ---------------------------------------------------------------- link/copy still work


def test_link_paths_apply_in_external_layout(tmp_path, repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "skills").mkdir()
    (repo / ".claude" / "skills" / "marker").write_text("x")
    root = tmp_path / "worktrees"
    p = run("--name", "linked", "--base", "main", "--root", str(root),
            "--wt-folder", "backend", "--link-paths", ".claude/skills", cwd=repo)
    assert p.returncode == 0, p.stderr
    link = root / "linked" / "backend" / ".claude" / "skills"
    assert link.is_symlink()
    assert (link / "marker").read_text() == "x"


def test_existing_worktree_dir_exits_4_in_external_layout(tmp_path, repo):
    root = tmp_path / "worktrees"
    (root / "dup" / "backend").mkdir(parents=True)
    p = run("--name", "dup", "--base", "main", "--root", str(root),
            "--wt-folder", "backend", cwd=repo)
    assert p.returncode == 4
