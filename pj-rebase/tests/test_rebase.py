"""Exercise the CLI against isolated Git worktrees, never the user's repositories."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/rebase.py"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE).strip()


def commit(repo, filename, text):
    (repo / filename).write_text(text)
    git(repo, "add", "--", filename)
    git(repo, "commit", "-m", text.strip())


@pytest.fixture
def combo(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("GIT_", "PJ_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PJ_ALIASES", str(tmp_path / "aliases.json"))
    (tmp_path / "aliases.json").write_text('{"aliases": {}}')
    root = tmp_path / "combo with spaces"
    root.mkdir()
    for folder in ("frontend", "backend"):
        main = tmp_path / (folder + "-main")
        main.mkdir()
        git(main, "init", "-b", "develop")
        git(main, "config", "user.name", "Test")
        git(main, "config", "user.email", "test@example.invalid")
        commit(main, "shared.txt", "base\n")
        git(main, "worktree", "add", "-b", "feature/example", str(root / folder))
        commit(main, "shared.txt", "upstream\n")
        commit(root / folder, "feature.txt", "feature\n")
    return root


def run(root, branch="develop"):
    process = subprocess.run([sys.executable, str(SCRIPT), "--", branch], cwd=root,
                             capture_output=True, text=True)
    return process.returncode, json.loads(process.stdout)


def test_success_from_nested_directory_and_repeat(combo):
    nested = combo / "frontend/subdir"
    nested.mkdir()
    code, output = run(nested)
    assert code == 0
    assert [item["repo"] for item in output["results"]] == ["FE", "BE"]
    for item in output["results"]:
        assert item["status"] == "success"
        assert item["conflicts"] == []
        assert item["before"] != item["after"]
        assert git(item["path"], "branch", "--show-current") == "feature/example"
        git(item["path"], "merge-base", "--is-ancestor", "develop", "HEAD")
    assert run(combo)[0] == 0


@pytest.mark.parametrize("folders", [("frontend",), ("backend",), ("frontend", "backend")])
def test_conflicts_preserved_and_other_repo_runs(combo, folders):
    for folder in folders:
        commit(combo / folder, "shared.txt", "conflicting feature\n")
    code, output = run(combo)
    assert code == 1
    for folder, item in zip(("frontend", "backend"), output["results"]):
        if folder in folders:
            assert item["status"] == "conflict"
            assert item["conflicts"] == ["shared.txt"]
            assert "rebase-merge" in item["operation"] or "rebase-apply" in item["operation"]
        else:
            assert item["status"] == "success"
    _, second = run(combo)
    assert second["results"][0 if folders[0] == "frontend" else 1]["status"] == "skipped"


@pytest.mark.parametrize("case", ["dirty", "untracked", "missing", "detached", "hook"])
def test_fe_problem_does_not_stop_be(combo, case):
    fe = combo / "frontend"
    before = git(fe, "rev-parse", "HEAD")
    if case == "dirty":
        (fe / "feature.txt").write_text("unsaved\n")
        git(fe, "config", "rebase.autoStash", "true")
    elif case == "untracked":
        (fe / "untracked.txt").write_text("keep\n")
    elif case == "missing":
        git(combo.parent / "frontend-main", "branch", "-m", "develop", "other-base")
    elif case == "detached":
        git(fe, "checkout", "--detach")
    elif case == "hook":
        hook = Path(git(fe, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-rebase"))
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
    code, output = run(combo)
    assert code == 1
    assert output["results"][0]["status"] == ("failed" if case == "hook" else "skipped")
    assert output["results"][1]["status"] == "success"
    assert git(fe, "rev-parse", "HEAD") == before
    if case == "dirty":
        assert (fe / "feature.txt").read_text() == "unsaved\n"
        assert git(fe, "stash", "list") == ""


def test_remote_tracking_branch(combo):
    for folder in ("frontend", "backend"):
        git(combo / folder, "update-ref", "refs/remotes/origin/develop", "develop")
    code, output = run(combo, "origin/develop")
    assert code == 0
    assert all(item["target_ref"] == "refs/remotes/origin/develop" for item in output["results"])


@pytest.mark.parametrize("branch", ["--abort", "develop;touch injected", "develop..HEAD"])
def test_invalid_input_changes_nothing(combo, branch):
    before = [git(combo / folder, "rev-parse", "HEAD") for folder in ("frontend", "backend")]
    code, output = run(combo, branch)
    assert code == 1 and output["error"] and output["results"] == []
    assert before == [git(combo / folder, "rev-parse", "HEAD") for folder in ("frontend", "backend")]


def test_incomplete_combo_does_not_rebase_fe(combo):
    before = git(combo / "frontend", "rev-parse", "HEAD")
    (combo / "backend").rename(combo.parent / "moved-backend")
    code, output = run(combo)
    assert code == 1 and output["results"] == []
    assert git(combo / "frontend", "rev-parse", "HEAD") == before
