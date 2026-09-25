"""Exercise review scope with real Git state in scratch consumer repositories."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


@pytest.fixture
def consumer(tmp_path):
    git(tmp_path, "init", "-b", "project")
    git(tmp_path, "config", "user.name", "PJ scope test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / ".gitignore").write_text(".codex/\nignored.txt\n")
    for name in ("committed.txt", "staged.txt", "unstaged.txt", "deleted.txt"):
        (tmp_path / name).write_text("baseline\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "baseline")
    git(tmp_path, "checkout", "-b", "task")
    # Match the installed consumer layout, including the shared sibling symlink.
    links = tmp_path / ".codex" / "skills"
    links.mkdir(parents=True)
    (links / "_shared").symlink_to(SOURCE, target_is_directory=True)
    (links / "pj-review").symlink_to(SOURCE.parent / "pj-review", target_is_directory=True)
    return tmp_path


def review(root, head="task"):
    helper = root / ".codex/skills/pj-review/../_shared/scripts/pj-review-diff.py"
    before = git(root, "status", "--porcelain=v1", "-z")
    index = (root / ".git/index").read_bytes()
    head_oid = git(root, "rev-parse", "HEAD")
    result = subprocess.run([sys.executable, str(helper), "--base", "project", "--head", head],
                            cwd=root, capture_output=True, text=True)
    assert git(root, "status", "--porcelain=v1", "-z") == before
    assert (root / ".git/index").read_bytes() == index
    assert git(root, "rev-parse", "HEAD") == head_oid
    return result


def test_uncommitted_work_is_reviewed_even_with_zero_commit_diff(consumer):
    root = consumer
    (root / "unstaged.txt").write_text("uncommitted implementation\n")
    (root / "new file.txt").write_text("new implementation\n")
    assert git(root, "diff", "project...task") == b""
    result = review(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "+uncommitted implementation" in result.stdout
    assert "+new implementation" in result.stdout


def test_branch_commits_staged_unstaged_and_new_files_all_appear(consumer):
    root = consumer
    (root / "committed.txt").write_text("branch implementation\n")
    git(root, "add", "committed.txt")
    git(root, "commit", "-m", "task change")
    (root / "staged.txt").write_text("staged implementation\n")
    git(root, "add", "staged.txt")
    (root / "unstaged.txt").write_text("working implementation\n")
    (root / "deleted.txt").unlink()
    (root / "새 파일.txt").write_text("new implementation\n")
    (root / "ignored.txt").write_text("excluded\n")
    (root / "binary.bin").write_bytes(b"\0\xff\0")
    # Project-only work after divergence must not leak into the review.
    git(root, "worktree", "add", str(root.parent / "project-wt"), "project")
    project = root.parent / "project-wt"
    (project / "project-only.txt").write_text("other task\n")
    git(project, "add", ".")
    git(project, "commit", "-m", "unrelated project change")
    result = review(root)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(result.stdout.splitlines()[1])
    assert set(manifest["tracked_files"]) == {
        "committed.txt", "staged.txt", "unstaged.txt", "deleted.txt"}
    assert set(manifest["untracked_files"]) == {"새 파일.txt", "binary.bin"}
    for text in ("+branch implementation", "+staged implementation", "+working implementation",
                 "+new implementation", "deleted file mode", "Binary files"):
        assert text in result.stdout
    assert "project-only.txt" not in result.stdout
    assert "excluded" not in result.stdout


def test_clean_branch_commits_remain_in_scope(consumer):
    (consumer / "committed.txt").write_text("already committed work\n")
    git(consumer, "add", ".")
    git(consumer, "commit", "-m", "implementation")
    result = review(consumer)
    assert result.returncode == 0
    assert "+already committed work" in result.stdout


def test_empty_and_wrong_worktree_are_not_clean_reviews(consumer):
    result = review(consumer)
    assert result.returncode == 3
    assert result.stdout.startswith("PJ_REVIEW_DIFF=empty")
    git(consumer, "checkout", "project")  # Same commit, wrong branch still fails.
    result = review(consumer)
    assert result.returncode == 2
    assert "Wrong worktree" in result.stdout


def test_missing_ref_is_not_an_empty_success(consumer):
    result = review(consumer, head="missing-branch")
    assert result.returncode == 2
    assert result.stdout.startswith("PJ_REVIEW_DIFF=error")


def test_empty_new_file_is_still_reviewable(consumer):
    (consumer / "empty.txt").touch()
    result = review(consumer)
    assert result.returncode == 0
    assert "empty.txt" in json.loads(result.stdout.splitlines()[1])["untracked_files"]


def test_untracked_symlink_and_shell_filename_are_read_as_data(consumer):
    outside = consumer.parent / "outside.txt"
    outside.write_text("outside content must not be read\n")
    (consumer / "external-link").symlink_to(outside)
    literal = "$(touch injected).txt"
    (consumer / literal).write_text("new implementation\n")
    result = review(consumer)
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(json.loads(result.stdout.splitlines()[1])["untracked_files"]) == {
        "external-link", literal}
    assert "outside content must not be read" not in result.stdout
    assert "+new implementation" in result.stdout
    assert not (consumer / "injected").exists()


def test_merge_conflicts_are_a_scope_error(consumer):
    (consumer / "committed.txt").write_text("task version\n")
    git(consumer, "add", ".")
    git(consumer, "commit", "-m", "task change")
    git(consumer, "checkout", "project")
    (consumer / "committed.txt").write_text("project version\n")
    git(consumer, "add", ".")
    git(consumer, "commit", "-m", "project change")
    git(consumer, "checkout", "task")
    merge = subprocess.run(["git", "-C", str(consumer), "merge", "project"],
                           capture_output=True)
    assert merge.returncode == 1
    result = review(consumer)
    assert result.returncode == 2
    assert result.stdout.startswith("PJ_REVIEW_DIFF=error")
    assert "Unresolved merge conflicts" in result.stdout
