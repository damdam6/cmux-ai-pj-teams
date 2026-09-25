"""End-to-end: board worktree → board registration → 적재 → 착수 → 머지.

This runs the COMMAND SEQUENCE each SKILL.md prescribes, in order, against real git repos and a
scratch vault (PJ_VAULT). It is not a unit test of any one script — it is the check that the
scripts compose, which is the thing unit tests with stubbed neighbours cannot answer.

cmux runs in TEST mode (PJ_CMUX_TEST=1 + PJ_CMUX_LIVE_HELPERS=1): the transport records cmux
argv instead of spawning real workspaces and agent sessions, while every local helper script
runs for real. Everything else is real — real worktrees, real branches, real merges, a real
task list in a scratch vault.

NOT covered here, and deliberately: spawning real cmux workspaces and the agent sessions inside
them (pj-plan/pj-work/pj-review are agent procedures, not scripts), and DELIVERING a wake-up
into a live pane — that needs a surface to type into. The relay unit tests cover delivery
against a recorded cmux tree.

Covered, in the order a user does it:
  1. pj-board-wt for a ONE-repo project, and for a TWO-repo project where one repo attaches an
     existing branch and the other cuts a new one.
  2. pj-board registration from the created worktree(s).
  3. 적재 of a one-repo task and a two-repo task under the same board.
  4. 착수 of both — worktrees per target repo, one workspace, the list recorded.
  5. pj-done and the merge landing on EACH repo's own project branch.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess

import pytest

SK = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = SK / "_shared" / "scripts"
CREATE_WT = SCRIPTS / "create-worktree.sh"
TASKS = SCRIPTS / "pj-tasks.py"
REPOS = SCRIPTS / "pj-repos.py"
DISPATCH = SCRIPTS / "pj-dispatch.py"
CMUX = SCRIPTS / "pj-cmux.py"
WIRE = SCRIPTS / "wire-session-root.py"
LINK_FAMILY = SCRIPTS / "pj-link-family.sh"

BOARD_SURFACE = "AAAAAAAA-1111-2222-3333-444444444444"
LAUNCHER = "claude"


# --------------------------------------------------------------------------- helpers


def git(repo, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert p.returncode == 0, f"git {' '.join(args)}: {p.stderr}"
    return p.stdout


def sh(*argv, env=None, cwd=None, expect=0) -> subprocess.CompletedProcess:
    p = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                       env=env, cwd=str(cwd) if cwd else None)
    if expect is not None:
        assert p.returncode == expect, (
            f"{argv[0]} exit {p.returncode} (want {expect})\n"
            f"stdout: {p.stdout}\nstderr: {p.stderr}")
    return p


def result_block(out: str) -> dict[str, str]:
    m = re.search(r"---WORKTREE_RESULT---\n(.*?)---END_WORKTREE_RESULT---", out, re.S)
    assert m, out
    return dict(l.split("=", 1) for l in m.group(1).strip().splitlines())


def fields(line: str) -> dict[str, str]:
    return dict(kv.split("=", 1) for kv in line.split() if "=" in kv)


@pytest.fixture
def world(tmp_path):
    """Two repos with a commit, an empty worktree root, and a scratch vault."""
    repos = tmp_path / "github"
    repos.mkdir()
    # Named after the real repos on purpose: the alias file is keyed by folder name, so these
    # exercise the real `linkPaths`/`wtFolder` entries rather than a fixture's idea of them.
    made = {}
    for name in ("example-backend", "example-frontend"):
        r = repos / name
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        (r / "README.md").write_text(f"# {name}\n")
        # The real repos gitignore their runtime directories (verified), so the symlinks
        # --link-paths creates are untracked there. Without this the fixture would commit them
        # and the later squash-merge would collide on them — a fixture artefact, not a defect.
        # No trailing slash: --link-paths creates `.codex` as a SYMLINK, and a `.codex/`
        # pattern matches only a directory. The real repos ignore both forms (verified).
        (r / ".gitignore").write_text(".claude\n.codex\n")
        git(r, "add", "-A")
        git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
        # the runtime wiring a real repo carries — what `linkPaths` points at, and what both
        # linkers work from. Without it the linkers have nothing to link and the flow would
        # pass while the sessions had no skills, which is the failure being guarded against.
        for runtime in (".claude", ".codex"):
            skills = r / runtime / "skills"
            skills.mkdir(parents=True)
            own = skills / f"{name.split('-')[1]}-only"
            own.mkdir()
            (own / "SKILL.md").write_text(f"---\nname: {own.name}\n---\n")
        (r / ".claude" / "settings.local.json").write_text(
            json.dumps({"permissions": {"allow": [f"Bash({name}:*)"]}}))
        made[name] = r
    vault = tmp_path / "vault"
    (vault / "raw" / "tasks").mkdir(parents=True)
    # cmux is stubbed (no real workspaces, no spawned agent sessions); every local helper runs
    # for real, which is what makes this a composition test rather than an argv-format test.
    env = dict(os.environ, PJ_VAULT=str(vault), PJ_CMUX_TEST="1",
               PJ_CMUX_LIVE_HELPERS="1",
               CMUX_SURFACE_ID=BOARD_SURFACE, CMUX_WORKSPACE_ID=BOARD_SURFACE)
    env.pop("CODEX_SANDBOX", None)
    env.pop("CODEX_THREAD_ID", None)
    return {"repos": made, "root": repos / "worktrees", "vault": vault, "env": env,
            "tasks": vault / "raw" / "tasks"}


LINK_PATHS = ".claude/skills,.claude/settings.local.json,.codex"


def make_board_worktree(world, project, repo, folder, *, attach=None, base="main"):
    """Step 1 as pj-board-wt prescribes it — including --link-paths, which is how the worktree
    gets its own runtime wiring (create-worktree.sh delegates it to wire-session-root.py)."""
    argv = [CREATE_WT, "--name", project, "--repo", world["repos"][repo],
            "--root", world["root"], "--wt-folder", folder,
            "--link-paths", LINK_PATHS]
    argv += ["--attach", attach] if attach else ["--prefix", "feat", "--base", base]
    return result_block(sh(*argv, env=world["env"]).stdout)


def register_board(world, project, title, cwd):
    """Step 2 as pj-board prescribes it: resolve the location, then register the pairs."""
    entries = json.loads(sh(REPOS, "--cwd", cwd, env=world["env"]).stdout)
    argv = [TASKS, "proj-reg", "--project", project, "--surface", BOARD_SURFACE,
            "--title", title]
    for e in entries:
        argv += ["--pair", f"{e['repo']}:{e['branch']}"]
    return sh(*argv, env=world["env"], cwd=cwd).stdout.strip(), entries


def start_task(world, slug, board_cwd):
    """Step 4 as pj-task-start/pj-open-ws prescribe it."""
    verdict = fields(sh(DISPATCH, "--slug", slug, env=world["env"],
                        cwd=board_cwd).stdout.strip())
    assert verdict["PJ_DISPATCH"] == "open-ws", verdict
    bases = verdict["base"]
    repos = verdict["repos"].split(",")
    per_repo = ({r: b for r, b in (p.split(":", 1) for p in bases.split(","))}
                if ":" in bases else {repos[0]: bases})

    folders = {"example-backend": "backend", "example-frontend": "frontend"}
    created = []
    for repo in repos:
        res = result_block(sh(CREATE_WT, "--name", slug, "--repo", world["repos"][repo],
                              "--root", world["root"], "--wt-folder", folders[repo],
                              "--prefix", "feat", "--base", per_repo[repo],
                              "--link-paths", LINK_PATHS,
                              env=world["env"]).stdout)
        created.append({"repo": repo, **res})

    # Several worktrees means the session stands in their parent, which has no runtime config
    # of its own — pj-open-ws wires it before opening the workspace.
    if len(created) > 1:
        parent = pathlib.Path(created[0]["WORKTREE_ABS"]).parent
        # No --repo: the script discovers them from the target, so a caller cannot name a repo
        # whose worktree is not actually there.
        sh(WIRE, "--target", parent, env=world["env"], expect=None)
        # package PJ family (pj-ser-up included) — the merge only carries what a child already had
        sh(LINK_FAMILY, "--target", parent, env=world["env"], expect=None)

    argv = [CMUX, "request", "workspace.open", "--slug", slug,
            "--planner", LAUNCHER, "--worker", LAUNCHER, "--reviewer", LAUNCHER,
            "--name", f"WS/{slug}", "--planner-prompt", f"Run /pj-plan {slug}."]
    for c in created:
        argv += ["--repo-wt", f"{c['repo']}:{c['WORKTREE_ABS']}:{c['BRANCH']}"]
    sh(*argv, env=world["env"], cwd=board_cwd)

    start = [TASKS, "start", "--slug", slug,
             "--branch", ",".join(c["BRANCH"] for c in created)]
    if len(created) > 1:
        start += ["--repos", ",".join(c["repo"] for c in created)]
    sh(*start, env=world["env"], cwd=board_cwd)
    return created


def commit_work(worktree, filename="feature.txt"):
    (pathlib.Path(worktree) / filename).write_text("work\n")
    git(worktree, "add", "-A")
    git(worktree, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "-m", f"add {filename}")


def squash_merge(board_worktree, source_branch, message):
    """What standard squash merge does, reduced to its git core: merge the task branch into the branch
    the board worktree is standing on."""
    git(board_worktree, "merge", "--squash", source_branch)
    git(board_worktree, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "-m", message)


# --------------------------------------------------------------------------- 1. board worktrees


def test_one_repo_board_worktree(world):
    res = make_board_worktree(world, "solo-proj", "example-backend", "backend")
    assert res["WORKTREE_ABS"] == str(world["root"] / "solo-proj" / "backend")
    assert res["BRANCH"] == "feat/solo-proj"
    assert res["MODE"] == "create"

    # pj-repos answers with ONE entry, and the session's cwd is that worktree
    entries = json.loads(sh(REPOS, "--cwd", res["WORKTREE_ABS"], env=world["env"]).stdout)
    assert len(entries) == 1
    assert entries[0]["repo"] == "example-backend"
    assert entries[0]["branch"] == "feat/solo-proj"


def test_two_repo_board_worktree_with_a_branch_choice_per_repo(world):
    """The branch is chosen per repo: backend attaches one that already exists, frontend cuts a
    new one. That mix is the normal case — an effort usually starts on one side."""
    git(world["repos"]["example-backend"], "branch", "feat/ctx-be")

    be = make_board_worktree(world, "ctx", "example-backend", "backend",
                             attach="feat/ctx-be")
    fe = make_board_worktree(world, "ctx", "example-frontend", "frontend")

    assert (be["MODE"], be["BRANCH"]) == ("attach", "feat/ctx-be")
    assert (fe["MODE"], fe["BRANCH"]) == ("create", "feat/ctx")

    parent = world["root"] / "ctx"
    assert sorted(c.name for c in parent.iterdir()) == ["backend", "frontend"]

    # the parent is deliberately not a repo; pj-repos answers with both children
    assert subprocess.run(["git", "-C", str(parent), "rev-parse", "--git-dir"],
                          capture_output=True).returncode != 0
    entries = json.loads(sh(REPOS, "--cwd", parent, env=world["env"]).stdout)
    assert [(e["repo"], e["branch"]) for e in entries] == [
        ("example-backend", "feat/ctx-be"), ("example-frontend", "feat/ctx")]


def test_board_worktree_is_registered_as_a_workspace(world):
    """workspace.board records one registry entry per repo, all under one workspace name."""
    git(world["repos"]["example-backend"], "branch", "feat/ctx-be")
    be = make_board_worktree(world, "ctx", "example-backend", "backend", attach="feat/ctx-be")
    fe = make_board_worktree(world, "ctx", "example-frontend", "frontend")
    sh(CMUX, "request", "workspace.board", "--project", "ctx",
       "--repo-wt", f"example-backend:{be['WORKTREE_ABS']}:{be['BRANCH']}",
       "--repo-wt", f"example-frontend:{fe['WORKTREE_ABS']}:{fe['BRANCH']}",
       "--board", LAUNCHER, "--name", "BE+FE/ctx",
       "--board-prompt", "Run /pj-board ctx.",
       env=world["env"], cwd=world["root"] / "ctx")
    for repo, branch in (("example-backend", "feat/ctx-be"),
                         ("example-frontend", "feat/ctx")):
        reg = world["repos"][repo] / ".worktrees" / "registry.json"
        rows = json.loads(reg.read_text())["worktrees"]
        assert [r["branch"] for r in rows] == [branch]
        assert rows[0]["workspaceName"] == "BE+FE/ctx"


# --------------------------------------------------------------------------- 2. registration


def test_registration_from_a_one_repo_board(world):
    res = make_board_worktree(world, "solo-proj", "example-backend", "backend")
    out, entries = register_board(world, "solo-proj", "단일 프로젝트", res["WORKTREE_ABS"])
    assert "created=yes" in out
    line = (world["tasks"] / "index.md").read_text()
    assert "repo=example-backend " in line
    assert "branch=feat/solo-proj " in line
    assert "- `BE` **단일 프로젝트**" in line       # single-repo badge unchanged


def test_registration_from_a_two_repo_board(world):
    git(world["repos"]["example-backend"], "branch", "feat/ctx-be")
    make_board_worktree(world, "ctx", "example-backend", "backend", attach="feat/ctx-be")
    make_board_worktree(world, "ctx", "example-frontend", "frontend")
    out, entries = register_board(world, "ctx", "결합 프로젝트", world["root"] / "ctx")
    assert "created=yes" in out
    index = (world["tasks"] / "index.md").read_text()
    assert "repo=example-backend,example-frontend" in index
    assert "branch=feat/ctx-be,feat/ctx" in index
    assert "- `BE+FE` **결합 프로젝트**" in index
    # and pj-repos can now answer for the project by name
    by_project = json.loads(sh(REPOS, "--project", "ctx", "--repo-hint",
                               str(world["repos"]["example-backend"]),
                               env=world["env"]).stdout)
    assert [(e["repo"], e["branch"]) for e in by_project] == [
        ("example-backend", "feat/ctx-be"), ("example-frontend", "feat/ctx")]


# --------------------------------------------------------------------------- 3-5. full flow


@pytest.fixture
def board(world):
    """A registered two-repo board, standing at the parent of its two worktrees."""
    git(world["repos"]["example-backend"], "branch", "feat/ctx-be")
    be = make_board_worktree(world, "ctx", "example-backend", "backend", attach="feat/ctx-be")
    fe = make_board_worktree(world, "ctx", "example-frontend", "frontend")
    cwd = world["root"] / "ctx"
    register_board(world, "ctx", "결합 프로젝트", cwd)
    return {"cwd": cwd, "be": be, "fe": fe}


def test_files_a_one_repo_task_and_a_two_repo_task(world, board):
    sh(TASKS, "add", "--project", "ctx", "--slug", "fe-label", "--title", "FE 라벨만",
       "--repos", "example-frontend", env=world["env"], cwd=board["cwd"])
    sh(TASKS, "add", "--project", "ctx", "--slug", "both-flow", "--title", "양쪽 흐름",
       env=world["env"], cwd=board["cwd"])
    tasks = (world["tasks"] / "ctx" / "tasks.md").read_text()
    assert "repos=example-frontend" in tasks            # the subset is recorded
    assert "slug=both-flow created=" in tasks          # no subset: covers the project
    fe_line = [l for l in tasks.splitlines() if "slug=fe-label" in l][0]
    assert "`FE`" in fe_line                            # badge says which side


def test_dispatch_reports_the_right_target_set_and_bases(world, board):
    sh(TASKS, "add", "--project", "ctx", "--slug", "fe-label", "--title", "FE",
       "--repos", "example-frontend", env=world["env"], cwd=board["cwd"])
    sh(TASKS, "add", "--project", "ctx", "--slug", "both-flow", "--title", "양쪽",
       env=world["env"], cwd=board["cwd"])

    one = fields(sh(DISPATCH, "--slug", "fe-label", env=world["env"],
                    cwd=board["cwd"]).stdout.strip())
    assert one["repos"] == "example-frontend"
    assert one["base"] == "feat/ctx"                    # bare — one target repo

    two = fields(sh(DISPATCH, "--slug", "both-flow", env=world["env"],
                    cwd=board["cwd"]).stdout.strip())
    assert two["repos"] == "example-backend,example-frontend"
    assert two["base"] == "example-backend:feat/ctx-be,example-frontend:feat/ctx"


def test_full_flow_merges_each_task_into_its_own_repo_branch(world, board):
    """The whole point: two tasks under one board, and each branch lands on the project branch
    OF ITS OWN REPO."""
    sh(TASKS, "add", "--project", "ctx", "--slug", "fe-label", "--title", "FE 라벨만",
       "--repos", "example-frontend", env=world["env"], cwd=board["cwd"])
    sh(TASKS, "add", "--project", "ctx", "--slug", "both-flow", "--title", "양쪽 흐름",
       env=world["env"], cwd=board["cwd"])

    # ---- 착수
    fe_only = start_task(world, "fe-label", board["cwd"])
    combined = start_task(world, "both-flow", board["cwd"])

    assert [c["repo"] for c in fe_only] == ["example-frontend"]
    assert [c["repo"] for c in combined] == ["example-backend", "example-frontend"]
    # the combined task's two worktrees are siblings under one parent
    assert {str(pathlib.Path(c["WORKTREE_ABS"]).parent) for c in combined} == {
        str(world["root"] / "both-flow")}
    # standing in that parent, dispatch says work
    v = fields(sh(DISPATCH, "--slug", "both-flow", env=world["env"],
                  cwd=world["root"] / "both-flow").stdout.strip())
    assert v["PJ_DISPATCH"] == "work"
    assert v["worktree"] == str(world["root"] / "both-flow")

    # and the parent is a usable session root: a combined session discovers config from its own
    # cwd, never from the worktrees below it
    parent = world["root"] / "both-flow"
    assert (parent / ".claude" / "skills").exists(), "부모에 스킬이 없으면 세션은 스킬을 못 씀"
    assert (parent / ".claude" / "settings.local.json").is_file()
    # both repos' skills are in reach from the one session
    for own in ("backend-only", "frontend-only"):
        assert (parent / ".claude" / "skills" / own / "SKILL.md").is_file(), own
    assert (parent / ".claude" / "skills" / "pj-ser-up" / "SKILL.md").is_file()
    assert (parent / ".codex" / "skills" / "pj-ser-up" / "SKILL.md").is_file()
    # both repos' permissions survived the merge
    allow = json.loads((parent / ".claude" / "settings.local.json").read_text())
    assert sorted(allow["permissions"]["allow"]) == [
        "Bash(example-backend:*)", "Bash(example-frontend:*)"]

    # THE BRANCH: a one-worktree task is wired inside the worktree by create-worktree.sh, and
    # gets no parent wiring — the session stands in the worktree, so a `.claude` above it would
    # be config nothing reads.
    assert (pathlib.Path(fe_only[0]["WORKTREE_ABS"]) / ".claude" / "skills").exists()
    assert not (world["root"] / "fe-label" / ".claude").exists()
    # and the combined task keeps BOTH: per-worktree wiring as well as the parent's
    for c in combined:
        assert (pathlib.Path(c["WORKTREE_ABS"]) / ".claude" / "skills").exists(), c["repo"]

    # ---- implement + report
    # A distinct file per (task, repo): same-named files would make the second merge a no-op
    # and the assertions below could not tell which branch carried what.
    for slug, created in (("fe-label", fe_only), ("both-flow", combined)):
        for c in created:
            commit_work(c["WORKTREE_ABS"], f"{slug}--{c['repo']}.txt")

    # The report is BUILT and appended here; DELIVERY needs a live cmux surface to type into,
    # which is stubbed — hence exit 3. What matters for this flow is the recorded payload, and
    # delivery itself is covered by the relay unit tests.
    sh(CMUX, "request", "done.report", "--slug", "fe-label",
       "--commit", "ok", env=world["env"],
       cwd=fe_only[0]["WORKTREE_ABS"], expect=3)
    sh(CMUX, "request", "done.report", "--slug", "both-flow",
       "--result", "example-backend:ok", "--result", "example-frontend:ok",
       env=world["env"], cwd=world["root"] / "both-flow", expect=3)

    reports = {}
    for slug in ("fe-label", "both-flow"):
        stream = world["tasks"] / "ctx" / "exchanges" / slug / "worker.jsonl"
        for line in stream.read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            if ev["type"] == "done_report":
                reports[slug] = ev["payload"]
    assert reports["fe-label"]["results"] == [
        {"repo": "", "commit": "ok", "typecheck": "none"}]
    assert [r["repo"] for r in reports["both-flow"]["results"]] == [
        "example-backend", "example-frontend"]
    assert reports["both-flow"]["typecheck"] == "none"     # worst across repos
    sh(TASKS, "review", "--slug", "fe-label", env=world["env"], cwd=board["cwd"])
    sh(TASKS, "review", "--slug", "both-flow", env=world["env"], cwd=board["cwd"])

    # ---- merge, each in its own repo's board worktree
    squash_merge(board["fe"]["WORKTREE_ABS"], "feat/fe-label", "[FE] 라벨")
    out = sh(TASKS, "done", "--slug", "fe-label", "--merged", "example-frontend",
             env=world["env"], cwd=board["cwd"]).stdout
    assert "status=완료" in out          # its one target landed

    squash_merge(board["be"]["WORKTREE_ABS"], "feat/both-flow", "[BE] 흐름")
    out = sh(TASKS, "done", "--slug", "both-flow", "--merged", "example-backend",
             env=world["env"], cwd=board["cwd"]).stdout
    assert "result=partial" in out and "remaining=example-frontend" in out
    assert "status=검토 대기" in out     # one branch still outstanding

    squash_merge(board["fe"]["WORKTREE_ABS"], "feat/both-flow", "[FE] 흐름")
    out = sh(TASKS, "done", "--slug", "both-flow", "--merged", "example-frontend",
             env=world["env"], cwd=board["cwd"]).stdout
    assert "status=완료" in out

    # ---- the commits are on the right branches, in the right repos
    be_log = git(board["be"]["WORKTREE_ABS"], "log", "--oneline", "-3")
    fe_log = git(board["fe"]["WORKTREE_ABS"], "log", "--oneline", "-3")
    assert "[BE] 흐름" in be_log and "[FE] 흐름" in fe_log
    assert "[FE] 라벨" in fe_log
    assert "[FE] 라벨" not in be_log        # the FE-only task never touched backend
    assert "[BE] 흐름" not in fe_log

    # each repo's project branch carries exactly the work done in THAT repo
    be_wt = pathlib.Path(board["be"]["WORKTREE_ABS"])
    fe_wt = pathlib.Path(board["fe"]["WORKTREE_ABS"])
    assert (be_wt / "both-flow--example-backend.txt").is_file()
    assert not (be_wt / "both-flow--example-frontend.txt").is_file()
    assert not (be_wt / "fe-label--example-frontend.txt").is_file()
    assert (fe_wt / "both-flow--example-frontend.txt").is_file()
    assert (fe_wt / "fe-label--example-frontend.txt").is_file()
    assert not (fe_wt / "both-flow--example-backend.txt").is_file()

    # and the list agrees
    tasks = (world["tasks"] / "ctx" / "tasks.md").read_text()
    done = [l for l in tasks.splitlines() if l.startswith("- ")]
    assert all("closed=" in l for l in done), done


# --------------------------------------------------------------------------- 6. the review round
#
# Everything above stops where a wake-up would have to be TYPED into a session. This section
# drives the task flow past that point — planner handoff, N reviewers, replies, the completion
# gate — with the one thing a delivery needs to validate its target: an agent registry entry the
# transport can find. A test-only launcher supplies the synthetic process lookup;
# production target validation is unchanged and no live agent is started.
#
# What is still NOT exercised: the text typed into a pane (cmux is stubbed) and the agents' own
# judgement. The relay unit tests cover the former; nothing but a live run covers the latter.

import sys

TEST_WS = "TEST-WS-UUID"                 # what create_workspace() records under TEST
PLANNER_SURF = "TEST-PLANNER-SURFACE"    # positional surfaces workspace.open records under TEST
WORKER_SURF = "TEST-WORKER-SURFACE"
REVIEWER_SURF = "TEST-NEW-SURFACE"       # what new_surface_in_pane() returns under TEST


@pytest.fixture
def agents(world, tmp_path, monkeypatch):
    """Provide registry entries and a process lookup only for this test's synthetic PID."""
    registry = tmp_path / "cmux-agents"
    pid = "424242"
    launcher = tmp_path / "test-cmux.py"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('test_cmux', {str(CMUX)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "assert module.TEST\n"
        f"module.pid_command = lambda pid: 'claude test-agent' if pid == {pid!r} else ''\n"
        "raise SystemExit(module.main())\n")
    launcher.chmod(0o755)
    monkeypatch.setitem(globals(), "CMUX", launcher)
    for ws, surf in ((TEST_WS, PLANNER_SURF), (TEST_WS, WORKER_SURF), (TEST_WS, REVIEWER_SURF),
                     (BOARD_SURFACE, BOARD_SURFACE)):
        d = registry / ws
        d.mkdir(parents=True, exist_ok=True)
        (d / f"session-{surf}").write_text(
            f"ts=1786400000\nstate=idle\npid={pid}\nsurface={surf}\n"
            f"cwd={tmp_path}\nkind=claude\n")
    world["env"]["PJ_CMUX_REGISTRY_ROOT"] = str(registry)
    return registry


def as_role(world, ws: str, surf: str) -> dict:
    """The env a command run FROM a given session would carry — the transport records the
    requester from these, and the pill and the reviewer spawn go where they point."""
    return dict(world["env"], CMUX_WORKSPACE_ID=ws, CMUX_SURFACE_ID=surf)


def request(world, env, cwd, action, *args, stdin=None, expect=0) -> tuple[str, str | None]:
    """`pj-cmux.py request …` → (stdout, request id). Non-local actions relay inline here (no
    CODEX_* env), so `PJ_CMUX=delivered` in the output IS the delivery."""
    argv = [CMUX, "request", action, *args]
    if stdin is not None:
        argv.append("--stdin")
    p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, env=env,
                       cwd=str(cwd), input=stdin)
    if expect is not None:
        assert p.returncode == expect, (
            f"{action} exit {p.returncode} (want {expect})\nstdout: {p.stdout}\n"
            f"stderr: {p.stderr}")
    m = re.search(r"request=(req_[0-9a-f]+)", p.stdout)
    return p.stdout + p.stderr, (m.group(1) if m else None)


def ack(world, env, cwd, slug, request_id, status="processed") -> None:
    sh(CMUX, "ack", "--slug", slug, "--request-id", request_id, "--status", status,
       env=env, cwd=cwd)


def events(world, project, slug, stream) -> list[dict]:
    path = world["tasks"] / project / "exchanges" / slug / f"{stream}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_review_round_runs_every_selected_reviewer_and_gates_completion(world, board, agents):
    """Planner picks two optional reviewers → the round expects four → each is its own session
    in its own stream on its own launcher → completion is refused until the LAST one replies →
    a later round wakes them instead of opening more. Then the task lands as before."""
    sh(TASKS, "add", "--project", "ctx", "--slug", "both-flow", "--title", "양쪽 흐름",
       env=world["env"], cwd=board["cwd"])
    created = start_task(world, "both-flow", board["cwd"])
    task_cwd = world["root"] / "both-flow"
    planner = as_role(world, TEST_WS, PLANNER_SURF)
    worker = as_role(world, TEST_WS, WORKER_SURF)
    reviewer = as_role(world, TEST_WS, REVIEWER_SURF)

    # ---- the planner ends planning: cautions, references, and WHICH optional reviewers
    out, _ = request(world, planner, task_cwd, "worker.handoff", "--slug", "both-flow",
                     stdin=json.dumps({"cautions": ["BE 스키마 변경 금지"], "references": [],
                                       "reviewers": ["security", "database"]}))
    assert "PJ_CMUX=delivered" in out, out
    handoff = [e for e in events(world, "ctx", "both-flow", "planner")
               if e["type"] == "worker_handoff"][-1]
    assert handoff["payload"]["reviewers"] == ["security", "database"]

    # a default reviewer is not selectable; an unknown id fails HERE, not at review time
    for bad in (["code"], ["securty"]):
        out, _ = request(world, planner, task_cwd, "worker.handoff", "--slug", "both-flow",
                         stdin=json.dumps({"cautions": [], "references": [], "reviewers": bad}),
                         expect=1)

    # ---- the worker opens the round: the expected set is the defaults + the planner's picks
    out, _ = request(world, worker, task_cwd, "review.group.open", "--slug", "both-flow",
                     stdin=json.dumps({"summary": "양쪽 구현 완료", "diff_base": "feat/ctx-be",
                                       "diff_head": "feat/both-flow"}))
    group = [e for e in events(world, "ctx", "both-flow", "worker")
             if e["type"] == "review_group_opened"][-1]
    assert group["payload"]["roles"] == ["code", "database", "plan", "security"]

    # ---- one request per expected reviewer; each is delivered
    for role in ("code", "plan", "security", "database"):
        out, _ = request(world, worker, task_cwd, "review.request", "--slug", "both-flow",
                         "--role", role)
        assert "PJ_CMUX=delivered" in out, f"{role}: {out}"

    # a reviewer nobody selected is refused, and says who decides
    out, _ = request(world, worker, task_cwd, "review.request", "--slug", "both-flow",
                     "--role", "react", expect=1)
    assert "계획 종료 시" in out

    # ---- three spawned sessions, three streams, three launchers — none shared
    attached = {}
    for role, stream in (("code", "reviewer"), ("security", "reviewer-security"),
                         ("database", "reviewer-database")):
        att = [e for e in events(world, "ctx", "both-flow", stream) if e["type"] == "role_attached"]
        assert len(att) == 1, f"{stream}: {len(att)} attachments"
        assert att[0]["payload"]["role"] == role
        attached[role] = att[0]["payload"]["launcher"]
    # code keeps the launcher the workspace was opened with (recorded wins over the definition);
    # the optional reviewers come from their own definitions for that launcher's review option
    assert attached["code"] == LAUNCHER
    assert attached["security"] == "claude"
    assert attached["database"] == "claude"
    # plan is a wake-up of the planner, never a spawn
    assert not [e for e in events(world, "ctx", "both-flow", "planner")
                if e["type"] == "role_attached" and e["payload"].get("role") == "plan"]

    # ---- completion is refused while anyone is outstanding
    out, _ = request(world, worker, task_cwd, "review.complete", "--slug", "both-flow", expect=1)
    assert "review.complete 거부" in out

    def reply(role: str, env: dict, findings: list) -> None:
        out, rid = request(world, env, task_cwd, "review.reply", "--slug", "both-flow",
                           "--role", role, stdin=json.dumps({"findings": findings}))
        assert "PJ_CMUX=delivered" in out and rid, f"{role}: {out}"
        ack(world, worker, task_cwd, "both-flow", rid)

    reply("code", reviewer, [{"severity": "suggestion", "location": "a.ts:1",
                               "finding": "f", "evidence": "e", "recommended_change": "r"}])
    reply("plan", planner, [])
    reply("security", reviewer, [])
    # three of four replied → still refused, and it names the one that has not
    out, _ = request(world, worker, task_cwd, "review.complete", "--slug", "both-flow", expect=1)
    assert "database" in out, out

    reply("database", reviewer, [])
    out, _ = request(world, worker, task_cwd, "review.complete", "--slug", "both-flow")
    assert "PJ_CMUX=delivered" in out, out
    assert [e for e in events(world, "ctx", "both-flow", "worker") if e["type"] == "review_complete"]

    # each reply landed in ITS reviewer's stream, not in a shared one
    assert [e["payload"]["role"] for e in events(world, "ctx", "both-flow", "reviewer")
            if e["type"] == "code_review"] == ["code"]
    assert [e["payload"]["role"] for e in events(world, "ctx", "both-flow", "reviewer-security")
            if e["type"] == "code_review"] == ["security"]
    assert [e["payload"]["role"] for e in events(world, "ctx", "both-flow", "reviewer-database")
            if e["type"] == "code_review"] == ["database"]
    assert [e["type"] for e in events(world, "ctx", "both-flow", "planner")
            if e["type"] == "plan_review"] == ["plan_review"]

    # ---- round 2 wakes the same sessions: no new attachment anywhere
    request(world, worker, task_cwd, "review.group.open", "--slug", "both-flow",
            stdin=json.dumps({"summary": "수정 반영", "diff_base": "feat/ctx-be",
                              "diff_head": "feat/both-flow"}))
    for role in ("code", "security", "database"):
        out, _ = request(world, worker, task_cwd, "review.request", "--slug", "both-flow",
                         "--role", role)
        assert "PJ_CMUX=delivered" in out, f"round 2 {role}: {out}"
    for stream in ("reviewer", "reviewer-security", "reviewer-database"):
        assert len([e for e in events(world, "ctx", "both-flow", stream)
                    if e["type"] == "role_attached"]) == 1, f"{stream} spawned again"

    # ---- and the task still lands where it did before
    for c in created:
        commit_work(c["WORKTREE_ABS"], f"both-flow--{c['repo']}.txt")
    out, _ = request(world, worker, task_cwd, "done.report", "--slug", "both-flow",
                     "--result", "example-backend:ok:none", "--result", "example-frontend:ok:none")
    assert "PJ_CMUX=delivered" in out, out          # the board is a registered agent now
    sh(TASKS, "review", "--slug", "both-flow", env=world["env"], cwd=board["cwd"])
    squash_merge(board["be"]["WORKTREE_ABS"], "feat/both-flow", "[BE] 흐름")
    squash_merge(board["fe"]["WORKTREE_ABS"], "feat/both-flow", "[FE] 흐름")
    sh(TASKS, "done", "--slug", "both-flow", "--merged", "example-backend",
       env=world["env"], cwd=board["cwd"])
    out = sh(TASKS, "done", "--slug", "both-flow", "--merged", "example-frontend",
             env=world["env"], cwd=board["cwd"]).stdout
    assert "status=완료" in out


def test_solo_task_excludes_the_plan_reviewer_from_the_round(world, board, agents):
    """One session is planner and worker. There is nobody independent to review the plan, so
    `plan` is not in the expected set at all — not requested-then-skipped, absent — and a
    request for it is refused rather than recorded as a self-review."""
    sh(TASKS, "add", "--project", "ctx", "--slug", "fe-solo", "--title", "FE 단독",
       "--repos", "example-frontend", env=world["env"], cwd=board["cwd"])
    verdict = fields(sh(DISPATCH, "--slug", "fe-solo", env=world["env"],
                        cwd=board["cwd"]).stdout.strip())
    res = result_block(sh(CREATE_WT, "--name", "fe-solo", "--repo",
                          world["repos"]["example-frontend"], "--root", world["root"],
                          "--wt-folder", "frontend", "--prefix", "feat", "--base", verdict["base"],
                          "--link-paths", LINK_PATHS, env=world["env"]).stdout)
    sh(CMUX, "request", "workspace.open", "--slug", "fe-solo", "--solo",
       "--planner", LAUNCHER, "--reviewer", LAUNCHER, "--name", "WS/fe-solo",
       "--planner-prompt", "Solo session: plan and implement here.",
       "--worktree", res["WORKTREE_ABS"], "--branch", res["BRANCH"],
       env=world["env"], cwd=board["cwd"])
    sh(TASKS, "start", "--slug", "fe-solo", "--branch", res["BRANCH"],
       env=world["env"], cwd=board["cwd"])
    solo = as_role(world, TEST_WS, "TEST-SOLO-SURFACE")
    wt = res["WORKTREE_ABS"]

    # the handoff is recorded but delivered to nobody — the session that asked is the worker
    out, _ = request(world, solo, wt, "worker.handoff", "--slug", "fe-solo",
                     stdin=json.dumps({"cautions": [], "references": [], "reviewers": ["react"]}))
    assert "self=true" in out, out

    request(world, solo, wt, "review.group.open", "--slug", "fe-solo",
            stdin=json.dumps({"summary": "s", "diff_base": verdict["base"],
                              "diff_head": res["BRANCH"]}))
    group = [e for e in events(world, "ctx", "fe-solo", "worker")
             if e["type"] == "review_group_opened"][-1]
    assert group["payload"]["roles"] == ["code", "react"]        # no `plan` on a solo task
    out, _ = request(world, solo, wt, "review.request", "--slug", "fe-solo", "--role", "plan",
                     expect=1)
    assert "solo" in out
