"""pj-ctx.py — a project's context documents, resolved for the session that will read them.

Two things make this a script rather than a rule in prose:

  - the prefix depends on WHERE the session stands (a worktree needs none, the parent of
    several needs the repo's folder), and that is exactly the kind of branch an agent forgets;
  - the old `ctx: <domain-name>` spelling is a dead pointer, and resolving it as a path would
    silently produce files that do not exist.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-ctx.py"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / "raw" / "tasks").mkdir(parents=True)
    monkeypatch.setenv("PJ_VAULT", str(v))
    return v


def load(vault):
    """Import with PJ_VAULT already set — the module reads it at import time."""
    spec = importlib.util.spec_from_file_location("pj_ctx", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def write_project(vault, name, frontmatter: str) -> None:
    d = vault / "raw" / "tasks" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.md").write_text(f"---\n{frontmatter}\n---\n\n# {name}\n", encoding="utf-8")


def run(vault, *argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PJ_VAULT=str(vault))
    return subprocess.run(["python3", str(SCRIPT), *argv],
                          capture_output=True, text=True, env=env)


# ---------------------------------------------------------------- frontmatter shapes


def test_flat_list_is_read(vault):
    write_project(vault, "p", "project: p\ndocs:\n  - docs/a.md\n  - docs/b.md")
    m = load(vault)
    value, field = m.read_field("p")
    assert field == "docs"
    assert value == ["docs/a.md", "docs/b.md"]


def test_ctx_wins_over_docs(vault):
    write_project(vault, "p", "project: p\nctx:\n  - docs/new.md\ndocs:\n  - docs/old.md")
    value, field = load(vault).read_field("p")
    assert field == "ctx" and value == ["docs/new.md"]


def test_per_repo_map_is_read(vault):
    write_project(vault, "p", "project: p\nctx:\n  backend:\n    - docs/be.md\n"
                              "  frontend:\n    - docs/fe.md\n    - docs/fe2.md")
    value, field = load(vault).read_field("p")
    assert value == {"backend": ["docs/be.md"],
                     "frontend": ["docs/fe.md", "docs/fe2.md"]}


def test_shared_key_is_read(vault):
    write_project(vault, "p", "project: p\nctx:\n  shared:\n    - raw/docs/x.md")
    value, _ = load(vault).read_field("p")
    assert value == {"shared": ["raw/docs/x.md"]}


def test_an_empty_field_is_allowed(vault):
    write_project(vault, "p", "project: p\nctx:\ndocs:")
    value, _ = load(vault).read_field("p")
    assert value == []


def test_other_frontmatter_keys_do_not_leak_in(vault):
    write_project(vault, "p", "project: p\ncolor: \"#112233\"\nctx:\n  - docs/a.md\n"
                              "created: 2026-09-11")
    value, _ = load(vault).read_field("p")
    assert value == ["docs/a.md"]


def test_a_missing_project_is_an_error(vault):
    p = run(vault, "--project", "ghost")
    assert p.returncode == 2
    assert "프로젝트 문서가 없습니다" in p.stderr


def test_missing_frontmatter_is_an_error(vault):
    d = vault / "raw" / "tasks" / "p"
    d.mkdir(parents=True)
    (d / "project.md").write_text("# no frontmatter\n")
    p = run(vault, "--project", "p")
    assert p.returncode == 2


# ---------------------------------------------------------------- legacy ctx


def test_a_legacy_domain_name_is_not_treated_as_a_path(vault):
    """`ctx: cpq-ai` named a /ctx-domain domain. Resolving it as a path would invent a file."""
    write_project(vault, "p", "project: p\nctx: cpq-ai")
    p = run(vault, "--project", "p")
    assert p.returncode == 0
    assert p.stdout.strip() == ""
    assert "옛 형식" in p.stderr


def test_a_legacy_domain_does_not_shadow_a_live_docs_list(vault):
    """20 of 21 live projects carry a dead `ctx` pointer; most also carry real `docs`."""
    write_project(vault, "p", "project: p\nctx: cpq-ai\ndocs:\n  - docs/real.md")
    p = run(vault, "--project", "p")
    assert p.returncode == 0
    assert "docs/real.md" in p.stdout
    assert "무시하고 docs" in p.stderr


def test_an_inline_path_is_still_a_path(vault):
    write_project(vault, "p", "project: p\nctx: docs/single.md")
    p = run(vault, "--project", "p")
    assert "docs/single.md" in p.stdout
    assert "옛 형식" not in p.stderr


# ---------------------------------------------------------------- resolution


@pytest.fixture
def two_repo_world(tmp_path, vault, monkeypatch):
    """Two repos, a combined parent, and a project owning both."""
    gh = tmp_path / "github"
    made = {}
    for name, folder in (("example-backend", "backend"), ("example-frontend", "frontend")):
        r = gh / name
        r.mkdir(parents=True)
        subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        made[name] = (r, folder)
    parent = gh / "worktrees" / "task"
    parent.mkdir(parents=True)
    for name, (r, folder) in made.items():
        subprocess.run(["git", "-C", str(r), "worktree", "add", "-q", "-b", "feat/x",
                        str(parent / folder)], check=True)
    return {"repos": made, "parent": parent}


def test_one_repo_session_needs_no_prefix(vault, two_repo_world):
    """The session root IS the worktree, so a repo-relative path is already correct — which is
    what keeps every existing single-repo project working untouched."""
    m = load(vault)
    wt = two_repo_world["parent"] / "backend"
    got = m.resolve(["docs/a.md"], str(wt), ["example-backend"])
    assert got[0]["resolved"] == str(wt / "docs" / "a.md")


def test_several_repos_prefix_each_with_its_folder(vault, two_repo_world):
    m = load(vault)
    parent = two_repo_world["parent"]
    got = m.resolve({"backend": ["docs/be.md"], "frontend": ["docs/fe.md"]},
                    str(parent), ["example-backend", "example-frontend"])
    assert {e["resolved"] for e in got} == {
        str(parent / "backend" / "docs" / "be.md"),
        str(parent / "frontend" / "docs" / "fe.md")}


def test_a_repo_key_may_be_the_repo_name_too(vault, two_repo_world):
    m = load(vault)
    parent = two_repo_world["parent"]
    got = m.resolve({"example-backend": ["docs/be.md"]}, str(parent),
                    ["example-backend", "example-frontend"])
    assert got[0]["resolved"] == str(parent / "backend" / "docs" / "be.md")


def test_shared_entries_are_vault_relative(vault, two_repo_world):
    m = load(vault)
    got = m.resolve({"shared": ["raw/docs/x.md"]}, str(two_repo_world["parent"]),
                    ["example-backend"])
    assert got[0]["repo"] == ""
    assert got[0]["resolved"] == str(vault / "raw" / "docs" / "x.md")


def test_absolute_entries_are_used_as_written(vault, two_repo_world):
    m = load(vault)
    got = m.resolve({"backend": ["/etc/hosts"]}, str(two_repo_world["parent"]),
                    ["example-backend"])
    assert got[0]["resolved"] == "/etc/hosts"


def test_without_a_root_the_repo_qualified_form_is_printed(vault):
    """What a person reading the board wants: which repo each document belongs to."""
    m = load(vault)
    got = m.resolve({"backend": ["docs/a.md"]}, None, ["example-backend"])
    assert got[0]["resolved"] == "backend/docs/a.md"


def test_a_flat_list_with_several_repos_warns_instead_of_guessing(vault, two_repo_world,
                                                                  capsys):
    m = load(vault)
    m.resolve(["docs/a.md"], str(two_repo_world["parent"]),
              ["example-backend", "example-frontend"])
    assert "어느 repo 의 경로인지" in capsys.readouterr().err


# ---------------------------------------------------------------- cli


def test_json_format_carries_the_repo_of_each_entry(vault):
    write_project(vault, "p", "project: p\nctx:\n  backend:\n    - docs/a.md")
    p = run(vault, "--project", "p", "--format", "json")
    assert p.returncode == 0
    assert json.loads(p.stdout)[0]["path"] == "docs/a.md"


def test_check_reports_entries_that_do_not_exist(vault):
    write_project(vault, "p", "project: p\nctx:\n  shared:\n    - raw/ghost.md")
    p = run(vault, "--project", "p", "--check")
    assert p.returncode == 3
    assert "MISSING" in p.stdout


def test_check_passes_when_everything_exists(vault):
    (vault / "raw" / "real.md").write_text("x")
    write_project(vault, "p", "project: p\nctx:\n  shared:\n    - raw/real.md")
    p = run(vault, "--project", "p", "--check")
    assert p.returncode == 0
    assert "missing=0" in p.stdout


def test_a_project_with_no_context_is_not_an_error(vault):
    write_project(vault, "p", "project: p")
    p = run(vault, "--project", "p")
    assert p.returncode == 0 and p.stdout.strip() == ""


# ---------------------------------------------------------------- unparseable lines


def test_a_flow_list_is_refused_not_silently_emptied(vault):
    """Found live: `example-backend: [README.md]` returned an empty ctx with no message, so a
    combined project's context vanished. The parser accepts three shapes on purpose; anything
    else must be an error that names the line, never an empty result."""
    write_project(vault, "p", "project: p\nctx:\n  example-backend: [README.md]")
    p = run(vault, "--project", "p")
    assert p.returncode == 2, p.stdout + p.stderr
    assert "해석할 수 없습니다" in p.stderr and "[README.md]" in p.stderr
    assert p.stdout.strip() == ""


def test_a_missing_colon_is_refused(vault):
    write_project(vault, "p", "project: p\nctx:\n  example-backend\n    - README.md")
    p = run(vault, "--project", "p")
    assert p.returncode == 2
    assert "example-backend" in p.stderr


# ---------------------------------------------------------------- a repo that is not in the session


TASKS = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-tasks.py"


def register_project(vault, world) -> None:
    """The project owning both repos on `feat/x` — which is what makes `pj-repos --project`
    able to name each repo's project worktree."""
    env = dict(os.environ, PJ_VAULT=str(vault))
    subprocess.run([sys.executable, str(TASKS), "proj-reg", "--project", "p", "--surface", "S",
                    "--title", "p", "--pair", "example-backend:feat/x",
                    "--pair", "example-frontend:feat/x"],
                   capture_output=True, text=True, env=env, check=True,
                   cwd=str(world["parent"]))


def be_only_worktree(world) -> pathlib.Path:
    """A backend-only task's worktree: a checkout of the backend repo alone, off the project
    branch. The frontend is not part of this session in any way."""
    be_repo = world["repos"]["example-backend"][0]
    wt = world["parent"].parent / "be-only" / "backend"
    subprocess.run(["git", "-C", str(be_repo), "worktree", "add", "-q", "-b", "feat/be-only",
                    str(wt), "feat/x"], check=True)
    return wt


def test_a_repo_not_in_the_session_resolves_to_its_project_worktree(vault, two_repo_world):
    """Reported from a live board: a backend-only task under a backend+frontend project was
    told to read the frontend docs at `<backend worktree>/<path>` — and since the same file
    names exist in the backend, --check said nothing. The frontend docs belong to the frontend
    project worktree, which is exactly what the board was doing by hand."""
    register_project(vault, two_repo_world)
    wt = be_only_worktree(two_repo_world)
    (two_repo_world["parent"] / "frontend" / "README.md").write_text("fe\n")
    (wt / "README.md").write_text("be\n")
    write_project(vault, "p", "project: p\nctx:\n  example-backend:\n    - README.md\n"
                              "  example-frontend:\n    - README.md")
    p = run(vault, "--project", "p", "--root", str(wt), "--format", "json")
    assert p.returncode == 0, p.stderr
    got = {e["repo"]: e for e in json.loads(p.stdout)}
    assert got["example-backend"]["source"] == "session"
    assert got["example-backend"]["resolved"] == str(wt / "README.md")
    assert got["example-frontend"]["source"] == "project"
    assert got["example-frontend"]["resolved"] == \
        str(two_repo_world["parent"] / "frontend" / "README.md")
    # and the planner is told to read the frontend's file, not the backend's twin
    assert pathlib.Path(got["example-frontend"]["resolved"]).read_text() == "fe\n"


def test_a_repo_with_no_checkout_anywhere_is_reported_not_guessed(vault, two_repo_world):
    """No session checkout and no project worktree: the path cannot be resolved, and --check
    must say so instead of pointing at whatever happens to be under the session root."""
    wt = be_only_worktree(two_repo_world)          # project NOT registered → no project worktrees
    write_project(vault, "p", "project: p\nctx:\n  example-frontend:\n    - README.md")
    # the project record is needed for the repo list; register it with the frontend on a branch
    # that has no worktree, so pj-repos cannot name a checkout for it
    env = dict(os.environ, PJ_VAULT=str(vault))
    subprocess.run([sys.executable, str(TASKS), "proj-reg", "--project", "p", "--surface", "S",
                    "--title", "p", "--pair", "example-backend:feat/be-only",
                    "--pair", "example-frontend:feat/nowhere"],
                   capture_output=True, text=True, env=env, check=True, cwd=str(wt))
    p = run(vault, "--project", "p", "--root", str(wt), "--check")
    assert p.returncode == 3, p.stdout + p.stderr
    assert "MISSING example-frontend" in p.stdout and "no checkout" in p.stdout
    # and never resolved into the backend worktree
    assert str(wt) not in [l for l in p.stdout.splitlines() if "example-frontend" in l][0]


def test_session_repos_still_resolve_in_place_under_a_parent(vault, two_repo_world):
    """The parent case is unchanged in outcome: each repo's docs under its own child folder —
    now taken from what pj-repos reports rather than from the alias's wtFolder."""
    register_project(vault, two_repo_world)
    parent = two_repo_world["parent"]
    write_project(vault, "p", "project: p\nctx:\n  backend:\n    - docs/be.md\n"
                              "  frontend:\n    - docs/fe.md")
    p = run(vault, "--project", "p", "--root", str(parent), "--format", "json")
    got = {e["repo"]: e for e in json.loads(p.stdout)}
    assert got["example-backend"]["resolved"] == str(parent / "backend" / "docs" / "be.md")
    assert got["example-frontend"]["resolved"] == str(parent / "frontend" / "docs" / "fe.md")
    assert {e["source"] for e in got.values()} == {"session"}
