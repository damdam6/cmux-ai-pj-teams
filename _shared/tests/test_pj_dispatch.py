"""pj-dispatch.py verdict matrix.

The single-repo cases are regression guards: `branch=`, `worktree=` and `base=` must keep the
exact single-value form the existing skills read verbatim. The multi-repo cases assert the
combined layout — worktrees as siblings under one parent, with that parent being where the
session stands.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-dispatch.py"
SPEC = importlib.util.spec_from_file_location("pj_dispatch", SCRIPT)
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

ROOT = "/root/worktrees"


def wt(repo, branch, worktree, main="/repos/x", linked=True):
    return {"repo": repo, "branch": branch, "worktree": worktree,
            "main_repo": main, "linked": linked}


@pytest.fixture
def stub(monkeypatch):
    """Wire the three things dispatch asks about: location, the task record, project branches."""
    state = {"here": [], "targets": [], "info": None, "pbranches": set()}

    def repos_of(*args):
        return state["here"] if args[0] == "--cwd" else state["targets"]

    monkeypatch.setattr(M, "repos_of", repos_of)
    monkeypatch.setattr(M, "task_info", lambda slug: state["info"])
    monkeypatch.setattr(M, "project_branches", lambda: state["pbranches"])
    return state


def run(stub, *argv: str, cwd: str = "/root/worktrees/compact-context") -> tuple[int, str]:
    out = io.StringIO()
    old, sys.argv = sys.argv, ["pj-dispatch.py", "--cwd", cwd, *argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = M.main()
    finally:
        sys.argv = old
    return rc, out.getvalue().strip()


def fields(line: str) -> dict[str, str]:
    return dict(kv.split("=", 1) for kv in line.split() if "=" in kv)


# ---------------------------------------------------------------- as_list / helpers


@pytest.mark.parametrize("given,want", [
    (None, []), ("feat/x", ["feat/x"]),
    ("feat/x,feat/y", ["feat/x", "feat/y"]), (["a", "b"], ["a", "b"]),
])
def test_as_list(given, want):
    assert M.as_list(given) == want


def test_fmt_pairs_single_repo_is_the_bare_value():
    """The regression guard in one line: one repo must not gain a `repo:` prefix."""
    assert M.fmt_pairs([wt("be", "feat/x", "/w")], "branch") == "feat/x"


def test_fmt_pairs_multi_repo_prefixes_each():
    assert M.fmt_pairs([wt("be", "feat/x", "/w"), wt("fe", "feat/y", "/w2")],
                       "branch") == "be:feat/x,fe:feat/y"


def test_shared_location_of_siblings_is_their_parent():
    assert M.shared_location([f"{ROOT}/t/backend", f"{ROOT}/t/frontend"]) == f"{ROOT}/t"


def test_shared_location_of_one_path_is_itself():
    assert M.shared_location(["/a/b/c"]) == "/a/b/c"


def test_shared_location_refuses_non_siblings():
    """Mixed layouts must not collapse into a meaningless far-up ancestor."""
    assert M.shared_location(["/repos/be/.worktrees/t", "/repos/fe/.worktrees/t"]) is None


# ---------------------------------------------------------------- ask


def test_no_slug_and_no_location_exits_2(stub):
    rc, _ = run(stub, cwd="/tmp/nowhere")
    assert rc == 2


def test_no_slug_but_a_location_on_a_project_branch_asks(stub):
    """A bare call in a board session must not guess a slug out of the project branch."""
    stub["here"] = [wt("be", "feat/design-system", "/repos/be/.worktrees/ds")]
    stub["pbranches"] = {"feat/design-system"}
    rc, line = run(stub)
    assert rc == 0 and line.startswith("PJ_DISPATCH=ask")
    assert "현재 위치에도 없음" in line


def test_slug_not_in_the_task_list_asks(stub):
    stub["here"] = [wt("be", "feat/ghost", "/w")]
    stub["info"] = None
    rc, line = run(stub, "--slug", "ghost")
    assert "작업 목록에 없음" in line


def test_unresolvable_target_repos_asks(stub):
    stub["info"] = {"project": "p", "repo": "be", "project_branch": "feat/p"}
    stub["targets"] = []
    rc, line = run(stub, "--slug", "x")
    assert "대상 repo 를 해석하지 못함" in line


# ---------------------------------------------------------------- single repo (regression)


def test_single_repo_work_keeps_bare_field_values(stub):
    path = "/repos/be/.worktrees/item-search"
    stub["here"] = [wt("example-backend", "feat/item-search", path)]
    stub["targets"] = [wt("example-backend", "feat/item-search", path)]
    stub["info"] = {"project": "search", "repo": "example-backend",
                    "project_branch": "feat/search"}
    rc, line = run(stub, "--slug", "item-search", cwd=path)
    f = fields(line)
    assert f["PJ_DISPATCH"] == "work"
    assert f["branch"] == "feat/item-search"          # no `repo:` prefix
    assert f["worktree"] == path
    assert f["repos"] == "example-backend"


def test_single_repo_goto_when_standing_elsewhere(stub):
    path = "/repos/be/.worktrees/item-search"
    stub["here"] = [wt("example-backend", "feat/other", "/repos/be/.worktrees/other")]
    stub["targets"] = [wt("example-backend", "feat/item-search", path)]
    stub["info"] = {"project": "search", "repo": "example-backend",
                    "project_branch": "feat/search"}
    rc, line = run(stub, "--slug", "item-search", cwd="/repos/be/.worktrees/other")
    f = fields(line)
    assert f["PJ_DISPATCH"] == "goto" and f["worktree"] == path


def test_single_repo_open_ws_base_is_the_project_branch_bare(stub):
    stub["here"] = [wt("example-backend", "feat/search", "/repos/be", linked=False)]
    stub["targets"] = [wt("example-backend", None, None)]
    stub["info"] = {"project": "search", "repo": "example-backend",
                    "project_branch": "feat/search"}
    stub["pbranches"] = {"feat/search"}
    rc, line = run(stub, "--slug", "new-task", cwd="/repos/be")
    f = fields(line)
    assert f["PJ_DISPATCH"] == "open-ws"
    assert f["base"] == "feat/search"                 # bare, as pj-open-ws reads it
    assert "worktree" not in f


def test_project_branch_never_yields_work(stub):
    """Standing on a project branch is a board, even when a task shares the slug."""
    stub["here"] = [wt("example-backend", "feat/design-system", "/repos/be/.worktrees/ds")]
    stub["pbranches"] = {"feat/design-system"}
    stub["targets"] = [wt("example-backend", None, None)]
    stub["info"] = {"project": "ds", "repo": "example-backend",
                    "project_branch": "feat/design-system"}
    rc, line = run(stub, "--slug", "design-system", cwd="/repos/be/.worktrees/ds")
    assert fields(line)["PJ_DISPATCH"] == "open-ws"


def test_missing_project_branch_asks_not_open_ws(stub):
    stub["targets"] = [wt("example-backend", None, None)]
    stub["info"] = {"project": "p", "repo": "example-backend", "project_branch": ""}
    rc, line = run(stub, "--slug", "x")
    assert "브랜치가 없음" in line


# ---------------------------------------------------------------- combined repos


def test_combined_work_from_the_shared_parent(stub):
    parent = f"{ROOT}/compact-context"
    children = [wt("example-backend", "feat/compact-context", f"{parent}/backend"),
                wt("example-frontend", "feat/compact-context", f"{parent}/frontend")]
    stub["here"] = children
    stub["targets"] = children
    stub["info"] = {"project": "ctx", "repo": "example-backend,example-frontend",
                    "project_branch": "feat/ctx-be,feat/ctx-fe"}
    rc, line = run(stub, "--slug", "compact-context", cwd=parent)
    f = fields(line)
    assert f["PJ_DISPATCH"] == "work"
    assert f["repos"] == "example-backend,example-frontend"
    assert f["branch"] == "example-backend:feat/compact-context,example-frontend:feat/compact-context"
    assert f["worktree"] == parent          # the parent, not one child


def test_combined_work_from_inside_one_child_still_reports_the_parent(stub):
    """The session belongs at the parent; standing in a child is not a different task."""
    parent = f"{ROOT}/compact-context"
    children = [wt("example-backend", "feat/compact-context", f"{parent}/backend"),
                wt("example-frontend", "feat/compact-context", f"{parent}/frontend")]
    stub["here"] = [children[0]]
    stub["targets"] = children
    stub["info"] = {"project": "ctx", "repo": "example-backend,example-frontend",
                    "project_branch": "feat/ctx-be,feat/ctx-fe"}
    rc, line = run(stub, "--slug", "compact-context", cwd=f"{parent}/backend")
    f = fields(line)
    assert f["PJ_DISPATCH"] == "work" and f["worktree"] == parent


def test_combined_goto_from_an_unrelated_location(stub):
    parent = f"{ROOT}/compact-context"
    stub["here"] = [wt("example-backend", "feat/other", f"{ROOT}/other/backend")]
    stub["targets"] = [wt("example-backend", "feat/compact-context", f"{parent}/backend"),
                       wt("example-frontend", "feat/compact-context", f"{parent}/frontend")]
    stub["info"] = {"project": "ctx", "repo": "example-backend,example-frontend",
                    "project_branch": "feat/ctx-be,feat/ctx-fe"}
    rc, line = run(stub, "--slug", "compact-context", cwd=f"{ROOT}/other/backend")
    f = fields(line)
    assert f["PJ_DISPATCH"] == "goto" and f["worktree"] == parent


def test_combined_open_ws_bases_are_per_repo(stub):
    stub["targets"] = [wt("example-backend", None, None), wt("example-frontend", None, None)]
    stub["info"] = {"project": "ctx", "repo": "example-backend,example-frontend",
                    "project_branch": "feat/ctx-be,feat/ctx-fe"}
    rc, line = run(stub, "--slug", "new-combined", cwd="/repos/be")
    f = fields(line)
    assert f["PJ_DISPATCH"] == "open-ws"
    assert f["base"] == "example-backend:feat/ctx-be,example-frontend:feat/ctx-fe"


def test_subset_task_in_a_two_repo_project_is_a_single_repo_verdict(stub):
    """D7: a task may target one repo of a two-repo project — and then it must look exactly
    like today's single-repo flow, prefixes included."""
    stub["targets"] = [wt("example-frontend", None, None)]
    stub["info"] = {"project": "ctx", "repo": "example-backend,example-frontend",
                    "project_branch": "feat/ctx-be,feat/ctx-fe", "repos": "example-frontend"}
    rc, line = run(stub, "--slug", "fe-only", cwd="/repos/fe")
    f = fields(line)
    assert f["repos"] == "example-frontend"
    assert f["base"] == "feat/ctx-fe"        # bare — one target repo
