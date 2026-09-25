"""wire-session-root.py — make a directory usable as an agent session root.

The bug this exists for is silent: runtime config is discovered from cwd UPWARD, so a session
standing above its worktrees sees none of their `.claude`. Nothing errors — the skills are just
not there.

What the tests pin down is that the strategy per path comes from what is actually on disk, not
from the path's name: same-target entries collapse, different containers merge, different units
(a skill, which says so with SKILL.md) conflict, and JSON files merge through merge-settings.py.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "wire-session-root.py"
ALIASES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "repo-aliases.json"


def run(target, repos, *extra) -> subprocess.CompletedProcess:
    argv = ["python3", str(SCRIPT), "--target", str(target)]
    for r in repos:
        argv += ["--repo", str(r)]
    return subprocess.run([*argv, *extra], capture_output=True, text=True)


@pytest.fixture
def vault_skill(tmp_path):
    """A skill living outside any repo — what both repos link to in real life."""
    s = tmp_path / "vault" / "shared-skill"
    s.mkdir(parents=True)
    (s / "SKILL.md").write_text("---\nname: shared-skill\n---\n")
    return s


@pytest.fixture
def repos(tmp_path, vault_skill):
    """Two repos wired the way the real ones are.

    They are NAMED after the real repos on purpose: the script reads `linkPaths` from the
    shared repo-aliases.json by folder name, so using those names exercises the real alias
    entries — which is the config this depends on and the thing most likely to drift.
    """
    made = {}
    for name in ("example-backend", "example-frontend"):
        r = tmp_path / name
        (r / ".claude" / "skills").mkdir(parents=True)
        (r / ".codex" / "skills").mkdir(parents=True)
        # both repos link the SAME vault skill — the common case
        (r / ".claude" / "skills" / "shared-skill").symlink_to(vault_skill)
        (r / ".codex" / "skills" / "shared-skill").symlink_to(vault_skill)
        (r / ".claude" / "settings.local.json").write_text(json.dumps(
            {"permissions": {"allow": [f"Bash({name})"],
                             "additionalDirectories": [f"/only/{name}"]}}))
        made[name] = r
    # repo-specific skills, one per side
    for name, own in (("example-backend", "be-only"), ("example-frontend", "fe-only")):
        d = made[name] / ".claude" / "skills" / own
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {own}\n---\n")
    return made


@pytest.fixture
def parent(tmp_path):
    p = tmp_path / "worktrees" / "combined"
    p.mkdir(parents=True)
    return p


def report(p: subprocess.CompletedProcess) -> dict[str, int]:
    """The counts line, with the dry-run prefix stripped so both modes compare directly."""
    line = [l.replace("[dry-run] ", "") for l in p.stdout.splitlines()
            if "linked=" in l][0]
    return {k: int(v) for k, v in (kv.split("=") for kv in line.split())}


# ---------------------------------------------------------------- the real alias file


def test_the_real_aliases_carry_link_paths_for_both_repos():
    """The script reads linkPaths as its source of truth for what a session needs; without
    them it has nothing to wire and says so."""
    data = json.loads(ALIASES.read_text(encoding="utf-8"))["aliases"]
    for repo in ("example-frontend", "example-backend"):
        lp = data[repo]["linkPaths"]
        assert any(p.startswith(".claude") for p in lp), repo
        assert any(p.startswith(".codex") for p in lp), repo


# ---------------------------------------------------------------- behaviour


def test_wires_the_parent_from_both_repos(repos, parent):
    p = run(parent, repos.values())
    assert p.returncode == 0, p.stderr
    assert (parent / ".claude" / "skills").is_dir()
    # the union: shared once, plus each repo's own
    names = sorted(c.name for c in (parent / ".claude" / "skills").iterdir())
    assert names == ["be-only", "fe-only", "shared-skill"]


def test_same_target_entries_collapse_to_one_link(repos, parent):
    """Both repos link the same vault skill; the parent points at the vault, not at a repo."""
    run(parent, repos.values())
    link = parent / ".claude" / "skills" / "shared-skill"
    assert link.is_symlink()
    assert "vault" in str(link.resolve())
    assert "example-" not in str(link.resolve())


def test_a_repo_specific_skill_is_reachable(repos, parent):
    run(parent, repos.values())
    for own in ("be-only", "fe-only"):
        assert (parent / ".claude" / "skills" / own / "SKILL.md").is_file()


def test_codex_is_wired_too_even_though_the_alias_names_the_whole_directory(repos, parent):
    """`.codex` is listed as a directory, so the merge has to recurse one level further than
    `.claude/skills` does — the depth follows the tree, not a guessed cap."""
    run(parent, repos.values())
    assert (parent / ".codex" / "skills" / "shared-skill" / "SKILL.md").is_file()


def test_settings_are_merged_not_picked(repos, parent):
    run(parent, repos.values())
    d = json.loads((parent / ".claude" / "settings.local.json").read_text())
    assert sorted(d["permissions"]["allow"]) == ["Bash(example-backend)",
                                                 "Bash(example-frontend)"]
    assert len(d["permissions"]["additionalDirectories"]) == 2


def test_two_different_skills_of_one_name_conflict_rather_than_merge(repos, parent):
    """Splicing two local copies together file by file would produce a skill neither repo has."""
    for name in repos:
        d = repos[name] / ".claude" / "skills" / "same-name"
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: same-name ({name})\n---\n")
    p = run(parent, repos.values())
    assert p.returncode == 3
    assert "같은 이름의 다른 스킬" in p.stderr
    assert not (parent / ".claude" / "skills" / "same-name").exists()


def test_a_conflict_does_not_stop_the_rest_from_being_wired(repos, parent):
    for name in repos:
        d = repos[name] / ".claude" / "skills" / "same-name"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: same-name\n---\n")
    run(parent, repos.values())
    assert (parent / ".claude" / "skills" / "shared-skill").exists()
    assert (parent / ".claude" / "skills" / "be-only" / "SKILL.md").is_file()


def test_a_non_json_file_in_both_repos_conflicts(repos, parent):
    for name in repos:
        (repos[name] / ".claude" / "skills" / "notes.txt").write_text(name)
    p = run(parent, repos.values())
    assert p.returncode == 3
    assert "병합 규칙이 없습니다" in p.stderr


def test_identical_files_in_both_repos_collapse(repos, parent):
    """Same realpath is the test, not same bytes — two links to one file are one file."""
    shared = repos["example-backend"].parent / "shared.txt"
    shared.write_text("x")
    for name in repos:
        (repos[name] / ".claude" / "skills" / "notes.txt").symlink_to(shared)
    p = run(parent, repos.values())
    assert p.returncode == 0, p.stderr
    assert (parent / ".claude" / "skills" / "notes.txt").resolve() == shared.resolve()


# ---------------------------------------------------------------- dry-run / idempotency


def test_dry_run_writes_nothing(repos, parent):
    p = run(parent, repos.values(), "--dry-run")
    assert p.returncode == 0, p.stderr
    assert "[dry-run]" in p.stdout
    assert not (parent / ".claude").exists()


def test_dry_run_reports_the_same_counts_as_the_real_run(repos, parent):
    dry = report(run(parent, repos.values(), "--dry-run"))
    real = report(run(parent, repos.values()))
    assert dry == real


def test_rerunning_is_idempotent(repos, parent):
    run(parent, repos.values())
    before = sorted(str(p.relative_to(parent)) for p in parent.rglob("*"))
    settings = (parent / ".claude" / "settings.local.json").read_text()
    p = run(parent, repos.values())
    assert p.returncode == 0, p.stderr
    assert sorted(str(q.relative_to(parent)) for q in parent.rglob("*")) == before
    assert (parent / ".claude" / "settings.local.json").read_text() == settings


def test_a_single_repo_parent_is_wired_the_same_way(repos, parent):
    """N=1 is not a special case: the same call does the right thing with one repo."""
    p = run(parent, [repos["example-backend"]])
    assert p.returncode == 0, p.stderr
    assert (parent / ".claude" / "skills").is_symlink()      # nothing to merge → link whole
    assert (parent / ".claude" / "skills" / "be-only" / "SKILL.md").is_file()


# ---------------------------------------------------------------- refusals


def test_a_missing_parent_is_refused(tmp_path, repos):
    p = run(tmp_path / "ghost", repos.values())
    assert p.returncode == 1
    assert "타깃 디렉터리가 없습니다" in p.stderr


def test_a_repo_with_no_alias_is_refused_with_the_reason(tmp_path, parent, repos):
    stranger = tmp_path / "unknown-repo"
    (stranger / ".claude").mkdir(parents=True)
    p = run(parent, [repos["example-backend"], stranger])
    assert p.returncode == 2
    assert "linkPaths" in p.stderr


def test_a_path_the_repo_does_not_have_is_skipped_not_failed(repos, parent):
    """A repo need not have every path its alias lists — the alias is shared config."""
    import shutil
    shutil.rmtree(repos["example-backend"] / ".codex")
    p = run(parent, repos.values())
    assert p.returncode == 0, p.stderr
    assert "건너뜀" in p.stderr
    # frontend still provides it, so the parent gets it whole
    assert (parent / ".codex" / "skills" / "shared-skill").exists()


# ---------------------------------------------------------------- asked vs discovered

class TestAskedVsDiscovered:
    """Two modes, and the difference is whether the caller SAID what to wire.

    create-worktree.sh passes through whatever its own caller asked for, so inferring extra
    paths from the alias would make a `--copy-paths` request quietly also link a repo's
    skills. The parent case is the opposite: no path flags, resolve from the alias.
    """

    def test_link_paths_only_wires_exactly_that(self, repos, parent):
        p = run(parent, [repos["example-backend"]], "--link-paths", ".claude/skills")
        assert p.returncode == 0, p.stderr
        assert (parent / ".claude" / "skills").exists()
        assert not (parent / ".codex").exists()        # in the alias, but not asked for

    def test_copy_paths_only_links_nothing(self, repos, parent, tmp_path):
        """The regression this pins: a copy request must not pull the alias's links in."""
        (repos["example-backend"] / "cfg").mkdir()
        (repos["example-backend"] / "cfg" / "f.txt").write_text("x")
        # the target has to be a worktree for copies to apply, so wire the repo itself
        p = run(repos["example-backend"], [repos["example-backend"]], "--copy-paths", "cfg")
        assert p.returncode == 0, p.stderr
        assert "linked=0" in p.stdout

    def test_no_path_flags_resolves_from_the_alias(self, repos, parent):
        p = run(parent, repos.values())
        assert p.returncode == 0, p.stderr
        assert (parent / ".claude" / "skills").exists()
        assert (parent / ".codex").exists()            # both came from the alias


# ---------------------------------------------------------------- copy semantics

class TestCopySemantics:
    """Copies name paths inside the repo's TREE, so they only mean something in a worktree."""

    def test_copies_apply_to_a_worktree_target(self, tmp_path, repos):
        repo = repos["example-backend"]
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        (repo / "envdir").mkdir()
        (repo / "envdir" / ".env.local").write_text("SECRET=1")
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        wt = tmp_path / "worktrees" / "task" / "backend"
        wt.parent.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feat/x",
                        str(wt)], check=True)
        p = run(wt, [repo], "--copy-paths", "envdir")
        assert p.returncode == 0, p.stderr
        assert "copied=1" in p.stdout
        assert (wt / "envdir" / ".env.local").read_text() == "SECRET=1"

    def test_copying_onto_the_source_itself_is_a_no_op_not_a_deletion(self, repos):
        """Wiring a repo as its own session root must not destroy what it was asked to copy."""
        repo = repos["example-backend"]
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        (repo / "envdir").mkdir()
        (repo / "envdir" / ".env.local").write_text("SECRET=1")
        p = run(repo, [repo], "--copy-paths", "envdir")
        assert p.returncode == 0, p.stderr
        assert "copied=0" in p.stdout
        assert (repo / "envdir" / ".env.local").read_text() == "SECRET=1"

    def test_copies_are_skipped_for_a_non_worktree_target(self, repos, parent):
        (repos["example-backend"] / "envdir").mkdir()
        (repos["example-backend"] / "envdir" / ".env").write_text("x")
        p = run(parent, [repos["example-backend"]], "--copy-paths", "envdir")
        assert p.returncode == 0, p.stderr
        assert "copied=0" in p.stdout
        assert "워크트리가 아닙니다" in p.stderr
        assert not (parent / "envdir").exists()

    def test_a_missing_copy_source_is_skipped_not_failed(self, repos):
        repo = repos["example-backend"]
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        p = run(repo, [repo], "--copy-paths", "ghost")
        assert p.returncode == 0, p.stderr
        assert "건너뜀" in p.stderr


# ---------------------------------------------------------------- repo discovery

class TestDiscovery:
    """With no --repo the script asks pj-repos.py, so a caller cannot name a repo whose
    worktree is not actually there."""

    def test_discovers_the_repos_from_a_combined_parent(self, tmp_path):
        repos_dir = tmp_path / "github"
        made = {}
        for name in ("example-backend", "example-frontend"):
            r = repos_dir / name
            (r / ".claude" / "skills").mkdir(parents=True)
            own = r / ".claude" / "skills" / f"{name}-skill"
            own.mkdir()
            (own / "SKILL.md").write_text("---\n")
            subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"], check=True)
            subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-q", "--allow-empty", "-m", "init"], check=True)
            made[name] = r
        parent = tmp_path / "worktrees" / "combined"
        parent.mkdir(parents=True)
        for name, folder in (("example-backend", "backend"), ("example-frontend", "frontend")):
            subprocess.run(["git", "-C", str(made[name]), "worktree", "add", "-q",
                            "-b", "feat/x", str(parent / folder)], check=True)
        p = subprocess.run(["python3", str(SCRIPT), "--target", str(parent)],
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        assert "example-backend" in p.stdout and "example-frontend" in p.stdout
        for name in made:
            assert (parent / ".claude" / "skills" / f"{name}-skill" / "SKILL.md").is_file()

    def test_a_target_with_nothing_to_discover_is_refused(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        p = subprocess.run(["python3", str(SCRIPT), "--target", str(plain)],
                           capture_output=True, text=True)
        assert p.returncode == 1
        assert "repo 를 찾지 못했습니다" in p.stderr


# ---------------------------------------------------------------- re-wiring and undo


def test_rewiring_a_parent_keeps_the_first_runs_ancestry_so_unwire_removes_everything(repos, parent):
    """Found live. The second run on an already wired parent creates less than the first — the
    ancestors (`.claude/`, `.claude/skills/`) already exist — and a manifest that only records
    the second run forgets them. `--unwire` then removes what it knows, leaves an empty
    `.claude/`, and the parent survives teardown as an orphan with 'human files' in it that no
    human ever made."""
    assert run(parent, repos.values()).returncode in (0, 3)
    # the way a caller re-wires: drop the merged file and wire again
    (parent / ".claude" / "settings.local.json").unlink()
    assert run(parent, repos.values()).returncode in (0, 3)
    manifest = json.loads((parent / ".pj-wiring.json").read_text())["created"]
    # ancestry not forgotten (directories are recorded with a trailing slash)
    assert ".claude/" in manifest and ".claude/skills/" in manifest

    p = subprocess.run(["python3", str(SCRIPT), "--target", str(parent), "--unwire"],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    # nothing of ours survives — and with nothing left, the parent itself is removed
    assert not (parent / ".claude").exists(), "an empty .claude/ was left behind"
    assert not parent.exists(), "an emptied parent should be removed, not kept"
    assert "removed target=" in p.stdout
