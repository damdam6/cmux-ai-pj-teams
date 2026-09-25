"""workspace.board — the board's own workspace.

A board workspace is not a task workspace: no roles, no plan document, no handoff. What this
file pins down is (a) the repo:worktree:branch parsing, (b) that the session's cwd is the
worktree when a board owns one repo and their shared parent when it owns several, and (c) that
the action registers NOTHING in the project list — registration is the user's act, run as
pj-board inside the workspace this builds.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-cmux.py"
SPEC = importlib.util.spec_from_file_location("pj_cmux_board", SCRIPT)
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def args(**kw) -> argparse.Namespace:
    base = {"name": None, "board": None, "board_prompt": None, "description": None,
            "repo_wt": None, "slug": None, "project": None}
    base.update(kw)
    return argparse.Namespace(**base)


class ParseRepoWtTests(unittest.TestCase):
    def setUp(self) -> None:
        pt = mock.patch.object(M, "TEST", True)
        pt.start()
        self.addCleanup(pt.stop)

    def test_one_spec(self) -> None:
        got = M.parse_repo_wt(["example-backend:/wt/ctx/backend:feat/ctx-be"])
        self.assertEqual(got, [{"repo": "example-backend", "worktree": "/wt/ctx/backend",
                                "branch": "feat/ctx-be"}])

    def test_two_specs_keep_order(self) -> None:
        got = M.parse_repo_wt(["example-backend:/wt/ctx/backend:feat/ctx-be",
                               "example-frontend:/wt/ctx/frontend:feat/ctx-fe"])
        self.assertEqual([r["repo"] for r in got],
                         ["example-backend", "example-frontend"])

    def test_none_is_empty(self) -> None:
        self.assertEqual(M.parse_repo_wt(None), [])

    def test_wrong_field_count_is_refused(self) -> None:
        for spec in ("be:/wt", "be:/wt:feat/x:extra", "be", "be::feat/x"):
            with self.assertRaises(M.Fail, msg=spec):
                M.parse_repo_wt([spec])

    def test_relative_worktree_is_refused(self) -> None:
        with self.assertRaises(M.Fail):
            M.parse_repo_wt(["be:wt/ctx/backend:feat/x"])

    def test_bad_branch_is_refused(self) -> None:
        with self.assertRaises(M.Fail):
            M.parse_repo_wt(["be:/wt/ctx/backend:-bad branch"])

    def test_duplicate_repo_is_refused(self) -> None:
        with self.assertRaises(M.Fail):
            M.parse_repo_wt(["be:/wt/a:feat/x", "be:/wt/b:feat/y"])


class WorkspaceCwdTests(unittest.TestCase):
    def test_one_worktree_is_its_own_cwd(self) -> None:
        """Unchanged for a single-repo board — the session stands in the git worktree."""
        p = {"worktree": "/wt/ctx/backend", "branch": "feat/x"}
        self.assertEqual(M.workspace_cwd(p), "/wt/ctx/backend")

    def test_several_worktrees_use_their_shared_parent(self) -> None:
        p = {"worktree": "/wt/ctx/backend", "branch": "",
             "repo_wt": [{"repo": "be", "worktree": "/wt/ctx/backend", "branch": "feat/a"},
                         {"repo": "fe", "worktree": "/wt/ctx/frontend", "branch": "feat/b"}]}
        self.assertEqual(M.workspace_cwd(p), "/wt/ctx")

    def test_non_sibling_worktrees_fall_back_to_the_first(self) -> None:
        """A mixed layout must not collapse into some far-up ancestor."""
        p = {"worktree": "/repos/be/.worktrees/t", "branch": "",
             "repo_wt": [{"repo": "be", "worktree": "/repos/be/.worktrees/t",
                          "branch": "feat/a"},
                         {"repo": "fe", "worktree": "/repos/fe/.worktrees/t",
                          "branch": "feat/b"}]}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(M.workspace_cwd(p), "/repos/be/.worktrees/t")


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        for pt in (mock.patch.object(M, "TEST", True),):
            pt.start()
            self.addCleanup(pt.stop)

    def build(self, **kw):
        base = dict(name="BE+FE/ctx", board="claude", board_prompt="Run /pj-board ctx.",
                    repo_wt=["example-backend:/wt/ctx/backend:feat/ctx-be",
                             "example-frontend:/wt/ctx/frontend:feat/ctx-fe"])
        base.update(kw)
        return M.build_workspace_board(args(**base), {}, "ctx", {})

    def test_payload_carries_every_repo(self) -> None:
        ev = self.build()
        self.assertEqual(ev["type"], "workspace_board")
        self.assertEqual([r["repo"] for r in ev["payload"]["repo_wt"]],
                         ["example-backend", "example-frontend"])

    def test_flat_branch_is_blank_for_several_repos(self) -> None:
        """`branch` is a single field; with two repos there is no one value it could hold, and
        guessing one would put the wrong branch in the registry."""
        self.assertEqual(self.build()["payload"]["branch"], "")

    def test_single_repo_keeps_the_flat_shape(self) -> None:
        ev = self.build(repo_wt=["example-backend:/wt/ctx/backend:feat/ctx-be"])
        self.assertEqual(ev["payload"]["worktree"], "/wt/ctx/backend")
        self.assertEqual(ev["payload"]["branch"], "feat/ctx-be")

    def test_no_repo_wt_is_refused(self) -> None:
        with self.assertRaises(M.Fail):
            self.build(repo_wt=None)

    def test_missing_name_is_refused(self) -> None:
        with self.assertRaises(M.Fail):
            self.build(name=None)

    def test_invalid_launcher_is_refused(self) -> None:
        with self.assertRaises(M.Fail):
            self.build(board="not-a-launcher")

    def test_empty_prompt_is_refused(self) -> None:
        with self.assertRaises(M.Fail):
            self.build(board_prompt="   ")

    def test_overlong_prompt_is_refused(self) -> None:
        with self.assertRaises(M.Fail):
            self.build(board_prompt="x" * 4001)

    def test_the_action_records_no_slug(self) -> None:
        """A board workspace belongs to a project, not a task."""
        self.assertIsNone(self.build()["slug"])


class DeliverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        M.EXECUTED.clear()
        for pt in (mock.patch.object(M, "TEST", True),
                   mock.patch.object(M, "TASKS_DIR", root)):
            pt.start()
            self.addCleanup(pt.stop)
        self.addCleanup(self.tmp.cleanup)

    def deliver(self, repo_wt):
        ev = M.build_workspace_board(
            args(name="BE+FE/ctx", board="claude", board_prompt="Run /pj-board ctx.",
                 description="결합 보드", repo_wt=repo_wt), {}, "ctx", {})
        ev["payload"]["requester"] = {}
        with contextlib.redirect_stderr(io.StringIO()):
            return M.deliver_workspace_board("ctx", None, ev)

    def new_ws_call(self):
        return [a for a in M.EXECUTED if a[:2] == ["cmux", "new-workspace"]][0]

    def test_single_pane_layout_boots_the_board_launcher(self) -> None:
        self.deliver(["example-backend:/wt/ctx/backend:feat/ctx-be"])
        call = self.new_ws_call()
        layout = call[call.index("--layout") + 1]
        self.assertEqual(layout.count('"pane"'), 1)      # no roles, no waiting pane
        self.assertNotIn("Run /pj-board ctx.", layout)
        prompt = next(M.TASKS_DIR.rglob("board-*.txt"))
        self.assertEqual(prompt.read_text(), "Run /pj-board ctx.")

    def test_cwd_is_the_worktree_for_one_repo(self) -> None:
        self.deliver(["example-backend:/wt/ctx/backend:feat/ctx-be"])
        call = self.new_ws_call()
        self.assertEqual(call[call.index("--cwd") + 1], "/wt/ctx/backend")

    def test_cwd_is_the_parent_for_two_repos(self) -> None:
        _, result = self.deliver(["example-backend:/wt/ctx/backend:feat/ctx-be",
                                  "example-frontend:/wt/ctx/frontend:feat/ctx-fe"])
        call = self.new_ws_call()
        self.assertEqual(call[call.index("--cwd") + 1], "/wt/ctx")
        self.assertEqual(result["cwd"], "/wt/ctx")
        self.assertEqual(result["repos"], ["example-backend", "example-frontend"])

    def test_every_repo_gets_a_registry_entry(self) -> None:
        self.deliver(["example-backend:/repos/be/.worktrees/ctx:feat/ctx-be",
                      "example-frontend:/repos/fe/.worktrees/ctx:feat/ctx-fe"])
        reg = [a for a in M.EXECUTED if a[0].endswith("wt-registry.py")]
        self.assertEqual(len(reg), 2)
        self.assertEqual({a[a.index("--repo") + 1] for a in reg},
                         {"/repos/be", "/repos/fe"})
        for a in reg:
            self.assertEqual(a[a.index("--workspace-name") + 1], "BE+FE/ctx")

    def test_description_is_applied(self) -> None:
        self.deliver(["example-backend:/wt/ctx/backend:feat/ctx-be"])
        desc = [a for a in M.EXECUTED
                if a[:2] == ["cmux", "workspace-action"] and "set-description" in a]
        self.assertEqual(desc[0][desc[0].index("--description") + 1], "결합 보드")

    def test_nothing_is_written_to_the_project_list(self) -> None:
        """D5: registration is the user's act — pj-board, inside this workspace."""
        self.deliver(["example-backend:/wt/ctx/backend:feat/ctx-be"])
        self.assertEqual([a for a in M.EXECUTED if a[0].endswith("pj-tasks.py")], [])


class ActionTableTests(unittest.TestCase):
    def test_registered_as_a_project_level_action(self) -> None:
        spec = M.ACTIONS["workspace.board"]
        self.assertFalse(spec["needs_slug"])   # a board has a project, not a task
        self.assertFalse(spec["local"])
        self.assertEqual(spec["source"], "board")

    def test_event_type_is_known_and_deliverable(self) -> None:
        self.assertIn("workspace_board", M.EVENT_TYPES)
        self.assertIn("workspace_board", M.DELIVER)


if __name__ == "__main__":
    unittest.main()


class WorkspaceOpenMultiRepoTests(unittest.TestCase):
    """workspace.open takes the same two input forms as workspace.board.

    The flat `--worktree`/`--branch` pair is what every existing caller passes, so it must keep
    producing the identical payload shape; `--repo-wt` is what a task targeting several repos
    passes instead.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        M.EXECUTED.clear()
        for pt in (mock.patch.object(M, "TEST", True),
                   mock.patch.object(M, "TASKS_DIR", pathlib.Path(self.tmp.name))):
            pt.start()
            self.addCleanup(pt.stop)
        self.addCleanup(self.tmp.cleanup)

    def build(self, info=None, **kw):
        base = dict(planner="claude", worker="claude", reviewer="codex",
                    name="BE+FE/ctx", planner_prompt="Run /pj-plan ctx-task.",
                    slug="ctx-task", solo=False, switch=None, worktree=None, branch=None,
                    repo_wt=None, description=None, project=None)
        base.update(kw)
        ns = argparse.Namespace(**base)
        return M.build_workspace_open(ns, info or {"project_branch": "feat/proj"},
                                      "ctx", {})

    def test_flat_form_keeps_the_single_repo_payload(self) -> None:
        ev = self.build(worktree="/wt/ctx-task/backend", branch="feat/ctx-task")
        p = ev["payload"]
        self.assertEqual(p["worktree"], "/wt/ctx-task/backend")
        self.assertEqual(p["branch"], "feat/ctx-task")
        self.assertEqual(M.worktree_pairs(p), [("/wt/ctx-task/backend", "feat/ctx-task")])

    def test_repo_wt_form_records_every_repo(self) -> None:
        ev = self.build(repo_wt=["example-backend:/wt/ctx-task/backend:feat/ctx-task",
                                 "example-frontend:/wt/ctx-task/frontend:feat/ctx-task"])
        p = ev["payload"]
        self.assertEqual([r["repo"] for r in p["repo_wt"]],
                         ["example-backend", "example-frontend"])
        self.assertEqual(p["worktree"], "/wt/ctx-task")     # the shared parent
        self.assertEqual(p["branch"], "")                   # no single value to hold

    def test_single_repo_wt_is_identical_to_the_flat_form(self) -> None:
        flat = self.build(worktree="/wt/ctx-task/backend", branch="feat/ctx-task")["payload"]
        pairs = self.build(
            repo_wt=["example-backend:/wt/ctx-task/backend:feat/ctx-task"])["payload"]
        self.assertEqual(flat["worktree"], pairs["worktree"])
        self.assertEqual(flat["branch"], pairs["branch"])
        self.assertEqual(M.worktree_pairs(flat), M.worktree_pairs(pairs))

    def test_both_forms_together_are_refused(self) -> None:
        with self.assertRaises(M.Fail):
            self.build(worktree="/wt/a", branch="feat/a",
                       repo_wt=["be:/wt/ctx-task/backend:feat/x"])

    def test_neither_form_is_refused(self) -> None:
        with self.assertRaises(M.Fail):
            self.build()

    def test_layout_is_unchanged_for_the_flat_form(self) -> None:
        ev = self.build(worktree="/wt/ctx-task/backend", branch="feat/ctx-task")
        ev["payload"]["requester"] = {}
        with contextlib.redirect_stderr(io.StringIO()):
            M.deliver_workspace_open("ctx", "ctx-task", ev)
        call = [a for a in M.EXECUTED if a[:2] == ["cmux", "new-workspace"]][0]
        layout = call[call.index("--layout") + 1]
        self.assertEqual(layout.count('"pane"'), 2)          # planner + waiting worker
        self.assertEqual(call[call.index("--cwd") + 1], "/wt/ctx-task/backend")

    def test_combined_task_session_stands_in_the_parent(self) -> None:
        ev = self.build(repo_wt=["example-backend:/wt/ctx-task/backend:feat/ctx-task",
                                 "example-frontend:/wt/ctx-task/frontend:feat/ctx-task"])
        ev["payload"]["requester"] = {}
        with contextlib.redirect_stderr(io.StringIO()):
            M.deliver_workspace_open("ctx", "ctx-task", ev)
        call = [a for a in M.EXECUTED if a[:2] == ["cmux", "new-workspace"]][0]
        self.assertEqual(call[call.index("--cwd") + 1], "/wt/ctx-task")
        self.assertEqual(call[call.index("--layout") + 1].count('"pane"'), 2)

    def test_each_repo_gets_its_own_registry_entry(self) -> None:
        ev = self.build(repo_wt=["example-backend:/repos/be/.worktrees/t:feat/t",
                                 "example-frontend:/repos/fe/.worktrees/t:feat/t"])
        ev["payload"]["requester"] = {}
        with contextlib.redirect_stderr(io.StringIO()):
            M.deliver_workspace_open("ctx", "ctx-task", ev)
        reg = [a for a in M.EXECUTED if a[0].endswith("wt-registry.py")]
        self.assertEqual({a[a.index("--repo") + 1] for a in reg}, {"/repos/be", "/repos/fe"})


class BaseBranchPairingTests(unittest.TestCase):
    def test_single_base_applies_as_is(self) -> None:
        p = {"base_branch": "feat/proj", "worktree": "/wt/t/be", "branch": "feat/t"}
        self.assertEqual(M.base_branch_for(p, "/wt/t/be"), "feat/proj")

    def test_multi_base_is_paired_by_position(self) -> None:
        p = {"base_branch": "feat/proj-be,feat/proj-fe", "worktree": "/wt/t",
             "repo_wt": [{"repo": "be", "worktree": "/wt/t/be", "branch": "feat/t"},
                         {"repo": "fe", "worktree": "/wt/t/fe", "branch": "feat/t"}]}
        self.assertEqual(M.base_branch_for(p, "/wt/t/be"), "feat/proj-be")
        self.assertEqual(M.base_branch_for(p, "/wt/t/fe"), "feat/proj-fe")

    def test_unknown_worktree_gets_no_base_rather_than_the_wrong_one(self) -> None:
        p = {"base_branch": "feat/proj-be,feat/proj-fe", "worktree": "/wt/t",
             "repo_wt": [{"repo": "be", "worktree": "/wt/t/be", "branch": "feat/t"},
                         {"repo": "fe", "worktree": "/wt/t/fe", "branch": "feat/t"}]}
        self.assertEqual(M.base_branch_for(p, "/wt/other"), "")

    def test_empty_base_stays_empty(self) -> None:
        self.assertEqual(M.base_branch_for({"worktree": "/w", "branch": "b"}, "/w"), "")


class DoneReportResultsTests(unittest.TestCase):
    """One report per task, per-repo verdicts inside it.

    Commit checks are per repo and typecheck results are optional, but the board
    records one task and merges one task — so the report must not split.
    """

    def setUp(self) -> None:
        pt = mock.patch.object(M, "TEST", True)
        pt.start()
        self.addCleanup(pt.stop)

    def build(self, **kw):
        base = dict(slug="t-one", commit=None, typecheck=None, result=None, project=None)
        base.update(kw)
        return M.build_done_report(argparse.Namespace(**base), {"branch": "feat/t-one"},
                                   "pjtest", {})

    def test_flat_form_is_a_one_element_result(self) -> None:
        p = self.build(commit="ok", typecheck="ok")["payload"]
        self.assertEqual(p["commit"], "ok")
        self.assertEqual(p["results"], [{"repo": "", "commit": "ok", "typecheck": "ok"}])

    def test_single_repo_completion_needs_no_typecheck(self) -> None:
        p = self.build(commit="ok")["payload"]
        self.assertEqual(p["results"], [{"repo": "", "commit": "ok", "typecheck": "none"}])

    def test_multi_repo_completion_needs_no_typecheck(self) -> None:
        p = self.build(result=["be:ok", "fe:ok"])["payload"]
        self.assertEqual(p["commit"], "ok")
        self.assertEqual(p["typecheck"], "none")
        self.assertEqual([r["typecheck"] for r in p["results"]], ["none", "none"])

    def test_commit_result_is_still_required(self) -> None:
        with self.assertRaises(M.Fail):
            self.build()
        with self.assertRaises(M.Fail):
            self.build(typecheck="ok")

    def test_per_repo_results_are_kept_verbatim(self) -> None:
        p = self.build(result=["example-backend:ok:ok",
                               "example-frontend:ok:none"])["payload"]
        self.assertEqual([r["repo"] for r in p["results"]],
                         ["example-backend", "example-frontend"])
        self.assertEqual(p["results"][1]["typecheck"], "none")

    def test_flat_fields_carry_the_worst_verdict(self) -> None:
        """A reader that only knows the old shape must not read a partial failure as clean."""
        p = self.build(result=["example-backend:ok:ok",
                               "example-frontend:ok:fail"])["payload"]
        self.assertEqual(p["typecheck"], "fail")

    def test_a_failing_commit_in_one_repo_fails_the_flat_field(self) -> None:
        p = self.build(result=["example-backend:ok:ok",
                               "example-frontend:fail:ok"])["payload"]
        self.assertEqual(p["commit"], "fail")

    def test_all_none_typechecks_stay_none(self) -> None:
        p = self.build(result=["a:ok:none", "b:ok:none"])["payload"]
        self.assertEqual(p["typecheck"], "none")

    def test_mixed_none_and_ok_is_ok(self) -> None:
        p = self.build(result=["a:ok:none", "b:ok:ok"])["payload"]
        self.assertEqual(p["typecheck"], "ok")

    def test_both_forms_together_are_refused(self) -> None:
        with self.assertRaises(M.Fail):
            self.build(commit="ok", typecheck="ok", result=["a:ok:ok"])

    def test_unparsed_is_preserved_as_optional_metadata_in_either_shape(self) -> None:
        for kw in ({"commit": "ok", "typecheck": "unparsed"},
                   {"result": ["example-backend:ok:unparsed"]}):
            self.assertEqual(self.build(**kw)["payload"]["typecheck"], "unparsed")

    def test_unparsed_is_not_aggregated_as_a_pass(self) -> None:
        p = self.build(result=["be:ok:unparsed", "fe:ok:ok"])["payload"]
        self.assertEqual(p["typecheck"], "unparsed")

    def test_malformed_result_is_refused(self) -> None:
        for spec in ("be", "be:ok:", "be:ok:ok:extra", "be::ok", "be:maybe:ok", "be:ok:maybe"):
            with self.assertRaises(M.Fail, msg=spec):
                self.build(result=[spec])

    def test_duplicate_repo_is_refused(self) -> None:
        with self.assertRaises(M.Fail):
            self.build(result=["be:ok:ok", "be:ok:fail"])
