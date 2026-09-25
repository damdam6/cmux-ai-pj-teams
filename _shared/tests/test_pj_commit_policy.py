"""Registration evidence, persistent policy reuse, invalidation and safe alias updates."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "pj-commit-policy.py"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE, text=True).strip()


@pytest.fixture
def consumer(tmp_path):
    repo = tmp_path / "repo ' with spaces"
    repo.mkdir()
    git(repo, "init", "-b", "project")
    git(repo, "config", "user.name", "PJ policy test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "CONTRIBUTING.md").write_text("Commit titles: <type>: <summary>. Use Korean summaries.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "docs: 커밋 규칙")
    return repo, tmp_path / "aliases.json"


def cli(consumer, command, *args, payload=None, trace=False):
    repo, aliases = consumer
    env = dict(os.environ, PJ_ALIASES=str(aliases))
    if trace:
        env["GIT_TRACE"] = str(trace)
    return subprocess.run([sys.executable, str(SCRIPT), command, "--repo", str(repo), *args],
                          input=json.dumps(payload) if payload is not None else None,
                          env=env, capture_output=True, text=True, timeout=10)


def analysis(consumer, *args):
    result = cli(consumer, "inspect", *args)
    assert result.returncode == 0, result.stderr
    path = consumer[1].parent / "analysis.json"
    path.write_text(result.stdout)
    return path, json.loads(result.stdout)


def policy(**changes):
    return dict({"status": "ready", "origin": "instructions", "subject": "<type>: <summary>",
                 "body": "", "language": "Korean summary", "integration": "same subject format",
                 "examples": ["fix: 로그인 오류 수정"], "notes": "",
                 "sourcePaths": ["CONTRIBUTING.md"]}, **changes)


def save(consumer, value=None, refresh=False, snapshot=None):
    if snapshot is None:
        snapshot, _ = analysis(consumer)
    return cli(consumer, "save", "--analysis", str(snapshot),
               *(["--refresh"] if refresh else []), payload=value or policy())


def test_missing_is_read_only_and_inspection_collects_real_evidence(consumer):
    result = cli(consumer, "get")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "missing"
    assert not consumer[1].exists()
    _, snapshot = analysis(consumer)
    assert snapshot["repoKey"] == consumer[0].name
    assert snapshot["samples"][0]["subject"] == "docs: 커밋 규칙"
    assert snapshot["sources"]["files"][0]["path"] == "CONTRIBUTING.md"
    assert len(snapshot["sources"]["files"][0]["sha256"]) == 64


def test_saved_policy_preserves_alias_data_and_history_advancement_does_not_reanalyze(consumer):
    repo, aliases = consumer
    aliases.write_text(json.dumps({"metadata": "keep", "aliases": {
        repo.name: {"short": "APP", "linkPaths": [".codex/skills"]}, "other": "OTH"}}))
    head = git(repo, "rev-parse", "HEAD")
    assert save(consumer).returncode == 0
    (repo / "code.txt").write_text("task implementation\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "feat: 기능 추가")
    trace = repo.parent / "git-trace.txt"
    result = cli(consumer, "get", trace=trace)
    assert result.returncode == 0, result.stderr
    cached = json.loads(result.stdout)
    assert cached["status"] == "ready" and cached["analysisHead"] == head
    assert cached["policy"] == policy()
    assert "git log" not in trace.read_text()
    data = json.loads(aliases.read_text())
    assert data["metadata"] == "keep" and data["aliases"]["other"] == "OTH"
    assert data["aliases"][repo.name]["linkPaths"] == [".codex/skills"]
    assert aliases.stat().st_mode & 0o777 == 0o600


def test_same_repo_worktree_reuses_policy_and_legacy_string_alias(consumer):
    repo, aliases = consumer
    aliases.write_text(json.dumps({"aliases": {repo.name: "APP"}}))
    assert save(consumer).returncode == 0
    worktree = repo.parent / "task checkout"
    git(repo, "worktree", "add", "-b", "task", str(worktree))
    result = cli((worktree, aliases), "get")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ready"
    assert json.loads(aliases.read_text())["aliases"][repo.name]["short"] == "APP"


def test_rule_change_requires_reviewed_refresh_and_old_snapshot_is_refused(consumer):
    snapshot, _ = analysis(consumer)
    assert save(consumer, snapshot=snapshot).returncode == 0
    original = consumer[1].read_bytes()
    (consumer[0] / "CONTRIBUTING.md").write_text("Commit titles: [ticket] description\n")
    assert json.loads(cli(consumer, "get").stdout)["status"] == "stale"
    assert save(consumer, refresh=True, snapshot=snapshot).returncode == 2
    assert consumer[1].read_bytes() == original
    fresh, _ = analysis(consumer)
    assert save(consumer, snapshot=fresh).returncode == 2
    assert save(consumer, policy(subject="[ticket] description"), True, fresh).returncode == 0
    assert json.loads(cli(consumer, "get").stdout)["policy"]["subject"] == "[ticket] description"


def test_head_change_during_analysis_does_not_save_stale_snapshot(consumer):
    snapshot, _ = analysis(consumer)
    git(consumer[0], "commit", "--allow-empty", "-m", "docs: 새 규칙 검토")
    assert save(consumer, snapshot=snapshot).returncode == 2
    assert not consumer[1].exists()


def test_extra_rule_source_is_tracked_and_shell_text_is_only_data(consumer):
    repo, _ = consumer
    (repo / "team").mkdir()
    (repo / "team/style.md").write_text("Use ticket prefix.\n")
    snapshot, _ = analysis(consumer, "--source", "team/style.md")
    value = policy(subject="$(touch injected) is a literal example", sourcePaths=["team/style.md"])
    assert save(consumer, value, snapshot=snapshot).returncode == 0
    assert not (repo / "injected").exists()
    assert json.loads(cli(consumer, "get").stdout)["policy"] == value
    (repo / "team/style.md").write_text("Use a different prefix.\n")
    assert json.loads(cli(consumer, "get").stdout)["status"] == "stale"


def test_ambiguous_history_stays_pending_and_new_sources_invalidate_cache(consumer):
    value = policy(status="needs-confirmation", origin="history", sourcePaths=[],
                   subject="", notes="Too little history; confirm the preferred title format.")
    assert save(consumer, value).returncode == 0
    assert json.loads(cli(consumer, "get").stdout)["status"] == "needs-confirmation"
    (consumer[0] / "commitlint.config.js").write_text("throw new Error('must not execute');\n")
    assert json.loads(cli(consumer, "get").stdout)["status"] == "stale"
    _, snapshot = analysis(consumer)
    assert "commitlint.config.js" in {s["path"] for s in snapshot["sources"]["files"]}


def test_empty_repository_can_register_a_pending_format(tmp_path):
    repo = tmp_path / "new-repo"
    repo.mkdir()
    git(repo, "init", "-b", "project")
    consumer = (repo, tmp_path / "aliases.json")
    value = policy(status="needs-confirmation", origin="history", sourcePaths=[],
                   subject="", notes="No instructions or history; confirm a format.")
    assert save(consumer, value).returncode == 0
    assert json.loads(cli(consumer, "get").stdout)["status"] == "needs-confirmation"
    assert save(consumer, policy(origin="history", sourcePaths=[]), refresh=True).returncode == 2


def test_package_dependencies_do_not_invalidate_inline_commitlint_policy(consumer):
    path = consumer[0] / "package.json"
    data = {"commitlint": {"extends": ["@commitlint/config-conventional"]}, "dependencies": {}}
    path.write_text(json.dumps(data))
    assert save(consumer, policy(sourcePaths=["package.json#commitlint"])).returncode == 0
    data["dependencies"]["example"] = "1.0.0"
    path.write_text(json.dumps(data))
    assert json.loads(cli(consumer, "get").stdout)["status"] == "ready"
    data["commitlint"]["rules"] = {"subject-case": [0]}
    path.write_text(json.dumps(data))
    assert json.loads(cli(consumer, "get").stdout)["status"] == "stale"


def test_removed_extra_rule_source_marks_cache_stale(consumer):
    rule = consumer[0] / "team-style.md"
    rule.write_text("Use ticket prefixes.\n")
    snapshot, _ = analysis(consumer, "--source", rule.name)
    assert save(consumer, policy(sourcePaths=[rule.name]), snapshot=snapshot).returncode == 0
    rule.unlink()
    result = cli(consumer, "get")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "stale"


def test_external_template_is_not_read_or_inferred_as_ready(consumer):
    outside = consumer[0].parent / "private-template"
    outside.write_text("private content must not be returned\n")
    git(consumer[0], "config", "commit.template", str(outside))
    snapshot, data = analysis(consumer)
    assert data["sources"]["externalTemplate"] == str(outside)
    assert "private content" not in snapshot.read_text()
    assert save(consumer, snapshot=snapshot).returncode == 2
    assert save(consumer, policy(origin="user", sourcePaths=[]), snapshot=snapshot).returncode == 0


@pytest.mark.parametrize("name", ["aliases.json", "aliases.json.lock"])
def test_alias_symlinks_are_rejected_without_touching_target(consumer, name):
    outside = consumer[1].parent / "outside.json"
    outside.write_text('{"aliases":{}}')
    (consumer[1].parent / name).symlink_to(outside)
    assert save(consumer).returncode == 2
    assert outside.read_text() == '{"aliases":{}}'


@pytest.mark.parametrize("kind", ["hardlink", "fifo"])
def test_alias_special_files_are_rejected_without_blocking_or_writing(consumer, kind):
    aliases = consumer[1]
    outside = aliases.parent / "outside.json"
    outside.write_text('{"aliases":{}}')
    if kind == "hardlink":
        os.link(outside, aliases)
    else:
        os.mkfifo(aliases)
    assert save(consumer).returncode == 2
    assert outside.read_text() == '{"aliases":{}}'


def test_invalid_json_and_policy_evidence_are_preserved(consumer):
    assert save(consumer, policy(sourcePaths=["../outside.md"])).returncode == 2
    consumer[1].write_text("broken json")
    assert save(consumer).returncode == 2
    assert consumer[1].read_text() == "broken json"


def test_alias_short_update_preserves_policy_during_concurrent_registration(consumer):
    snapshot, _ = analysis(consumer)
    env = dict(os.environ, PJ_ALIASES=str(consumer[1]))
    short = subprocess.Popen([sys.executable, str(SCRIPTS / "save-repo-alias.py"),
                              "--key", consumer[0].name, "--short", "APP"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        result = save(consumer, snapshot=snapshot)
        _, err = short.communicate(timeout=5)
        assert short.returncode == 0, err
        assert result.returncode == 0, result.stderr
        entry = json.loads(consumer[1].read_text())["aliases"][consumer[0].name]
        assert entry["short"] == "APP" and entry["commitPolicy"]["policy"] == policy()
    finally:
        if short.poll() is None:
            short.kill()
            short.wait(timeout=5)
