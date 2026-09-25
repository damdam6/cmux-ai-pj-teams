"""pj-tasks.py multi-repo registration and the task repo subset.

Runs against a TemporaryDirectory patched in as TASKS_DIR/INDEX — never the live vault.

The rule under test is D7: a project owns the repos its board has worktrees in, and a task
targets a SUBSET of them. A one-repo board and a one-repo task are the same code path with a
one-element list, which is what keeps the 24 existing projects working.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import pathlib
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-tasks.py"
SPEC = importlib.util.spec_from_file_location("pj_tasks_multi", SCRIPT)
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def ns(op: str, **kw) -> argparse.Namespace:
    base = {"project": None, "repo": None, "slug": None, "title": None, "branch": None,
            "surface": None, "force_branch": False, "deps": None, "status": None,
            "repos": None, "pair": None, "merged": None}
    base.update(kw)
    return argparse.Namespace(op=op, **base)


def run(op: str, **kw) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = M.OPS[op](ns(op, **kw))
    assert rc == 0
    return out.getvalue().strip()


def cli(*argv: str) -> tuple[int, str, str]:
    """Through main(), which is where --pair is folded into repo/branch."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", ["pj-tasks.py", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = M.main()
    return rc, out.getvalue().strip(), err.getvalue().strip()


class MultiRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        for p in (mock.patch.object(M, "TASKS_DIR", root),
                  mock.patch.object(M, "INDEX", root / "index.md")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    # ------------------------------------------------------------ proj-reg --pair

    def reg_two_repos(self, project="ctx") -> None:
        rc, out, err = cli("proj-reg", "--project", project, "--surface", "SURF1",
                           "--title", "결합 프로젝트",
                           "--pair", "example-backend:feat/ctx-be",
                           "--pair", "example-frontend:feat/ctx-fe")
        assert rc == 0, err

    def test_pair_registers_both_repos_and_branches(self) -> None:
        self.reg_two_repos()
        rec = M.OPS["proj-get"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rec(ns("proj-get", project="ctx"))
        import json
        meta = json.loads(out.getvalue())
        self.assertEqual(meta["repo"], "example-backend,example-frontend")
        self.assertEqual(meta["branch"], "feat/ctx-be,feat/ctx-fe")

    def test_single_pair_is_identical_to_repo_branch(self) -> None:
        cli("proj-reg", "--project", "solo-a", "--surface", "S1", "--title", "A",
            "--pair", "example-backend:feat/a")
        cli("proj-reg", "--project", "solo-b", "--surface", "S2", "--title", "A",
            "--repo", "example-backend", "--branch", "feat/a")
        text = M.INDEX.read_text(encoding="utf-8")
        a = [l for l in text.splitlines() if "project=solo-a" in l][0]
        b = [l for l in text.splitlines() if "project=solo-b" in l][0]
        self.assertEqual(a.replace("solo-a", "X").replace("S1", "S"),
                         b.replace("solo-b", "X").replace("S2", "S"))

    def test_pair_and_repo_together_is_refused(self) -> None:
        rc, _, err = cli("proj-reg", "--project", "x", "--surface", "S", "--title", "t",
                         "--pair", "a:feat/a", "--repo", "b")
        self.assertEqual(rc, 1)
        self.assertIn("같이 쓸 수 없습니다", err)

    def test_malformed_pair_is_refused(self) -> None:
        for spec in ("no-colon", "a:", ":feat/a", "a:b:c"):
            rc, _, err = cli("proj-reg", "--project", "x", "--surface", "S",
                             "--title", "t", "--pair", spec)
            self.assertEqual(rc, 1, spec)
            self.assertIn("repo:branch", err)

    def test_duplicate_repo_in_pairs_is_refused(self) -> None:
        rc, _, err = cli("proj-reg", "--project", "x", "--surface", "S", "--title", "t",
                         "--pair", "example-backend:feat/a",
                         "--pair", "example-backend:feat/b")
        self.assertEqual(rc, 1)
        self.assertIn("두 번", err)

    def test_mismatched_repo_and_branch_counts_are_refused(self) -> None:
        rc, _, err = cli("proj-reg", "--project", "x", "--surface", "S", "--title", "t",
                         "--repo", "example-backend,example-frontend", "--branch", "feat/only")
        self.assertEqual(rc, 1)
        self.assertIn("수가 다릅니다", err)

    # ------------------------------------------------------------ add --repos

    def test_task_subset_is_recorded(self) -> None:
        self.reg_two_repos()
        run("add", project="ctx", slug="fe-only", title="FE 쪽만",
            repos="example-frontend")
        line = [l for l in M.tasks_path("ctx").read_text(encoding="utf-8").splitlines()
                if "slug=fe-only" in l][0]
        self.assertIn("repos=example-frontend", line)
        self.assertIn("`FE`", line)

    def test_task_covering_the_whole_project_records_no_subset(self) -> None:
        """A subset equal to the whole would only repeat the project's own badge."""
        self.reg_two_repos()
        run("add", project="ctx", slug="both", title="둘 다",
            repos="example-backend,example-frontend")
        line = [l for l in M.tasks_path("ctx").read_text(encoding="utf-8").splitlines()
                if "slug=both" in l][0]
        self.assertNotIn("repos=", line)

    def test_task_subset_outside_the_project_is_refused(self) -> None:
        self.reg_two_repos()
        with self.assertRaises(M.Fail) as cm:
            run("add", project="ctx", slug="stray", title="엉뚱", repos="some-other-repo")
        self.assertIn("소유하지 않은 repo", str(cm.exception))

    def test_single_repo_project_still_takes_plain_add(self) -> None:
        cli("proj-reg", "--project", "solo", "--surface", "S", "--title", "단일",
            "--repo", "example-backend", "--branch", "feat/solo")
        run("add", project="solo", slug="plain", title="평범")
        line = [l for l in M.tasks_path("solo").read_text(encoding="utf-8").splitlines()
                if "slug=plain" in l][0]
        self.assertNotIn("repos=", line)
        self.assertEqual(line.count("`"), 0)

    # ------------------------------------------------------------ start

    def test_start_cannot_widen_the_filed_subset(self) -> None:
        """The worktrees and the plan are built around the set recorded at 적재."""
        self.reg_two_repos()
        run("add", project="ctx", slug="fe-only", title="FE", repos="example-frontend")
        with self.assertRaises(M.Fail) as cm:
            run("start", slug="fe-only", branch="feat/a,feat/b",
                repos="example-backend,example-frontend")
        self.assertIn("넓힐", str(cm.exception).replace("넓히려면", "넓힐"))

    def test_start_may_keep_the_filed_subset(self) -> None:
        self.reg_two_repos()
        run("add", project="ctx", slug="fe-only", title="FE", repos="example-frontend")
        out = run("start", slug="fe-only", branch="feat/fe-only")
        self.assertIn("status=진행 중", out)

    def test_start_records_several_branches_with_their_repos(self) -> None:
        self.reg_two_repos()
        run("add", project="ctx", slug="both", title="둘 다")
        run("start", slug="both", branch="feat/both-be,feat/both-fe",
            repos="example-backend,example-frontend")
        line = [l for l in M.tasks_path("ctx").read_text(encoding="utf-8").splitlines()
                if "slug=both" in l][0]
        self.assertIn("branch=feat/both-be,feat/both-fe", line)
        self.assertIn("`BE feat/both-be` · `FE feat/both-fe`", line)

    def test_start_with_several_branches_and_no_repos_is_refused(self) -> None:
        """The pairing is positional — an unlabelled pair of branches is an unreadable record."""
        self.reg_two_repos()
        run("add", project="ctx", slug="both", title="둘 다")
        with self.assertRaises(M.Fail) as cm:
            run("start", slug="both", branch="feat/a,feat/b")
        self.assertIn("--repos 가 없습니다", str(cm.exception))

    def test_start_mismatched_counts_are_refused(self) -> None:
        self.reg_two_repos()
        run("add", project="ctx", slug="both", title="둘 다")
        with self.assertRaises(M.Fail) as cm:
            run("start", slug="both", branch="feat/a,feat/b", repos="example-backend")
        self.assertIn("수가 다릅니다", str(cm.exception))

    def test_single_branch_start_is_unchanged(self) -> None:
        cli("proj-reg", "--project", "solo", "--surface", "S", "--title", "단일",
            "--repo", "example-backend", "--branch", "feat/solo")
        run("add", project="solo", slug="plain", title="평범")
        out = run("start", slug="plain", branch="feat/plain")
        self.assertIn("status=진행 중", out)
        line = [l for l in M.tasks_path("solo").read_text(encoding="utf-8").splitlines()
                if "slug=plain" in l][0]
        self.assertIn("`feat/plain`", line)          # bare backtick, no repo label
        self.assertNotIn("BE feat/plain", line)

    # ------------------------------------------------------------ get fills the project's repos

    def test_get_reports_the_projects_repos_and_the_tasks_subset(self) -> None:
        self.reg_two_repos()
        run("add", project="ctx", slug="fe-only", title="FE", repos="example-frontend")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            M.OPS["get"](ns("get", slug="fe-only"))
        import json
        d = json.loads(out.getvalue())
        self.assertEqual(d["repo"], "example-backend,example-frontend")
        self.assertEqual(d["repos"], "example-frontend")
        self.assertEqual(d["project_branch"], "feat/ctx-be,feat/ctx-fe")


if __name__ == "__main__":
    unittest.main()


class PartialMergeTests(unittest.TestCase):
    """완료 means every target repo's branch landed.

    A two-repo task is merged twice and the second can conflict after the first landed. The
    task must then stay 검토 대기 with the landed repos recorded — otherwise the list says
    finished while a branch is still outstanding.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        for p in (mock.patch.object(M, "TASKS_DIR", root),
                  mock.patch.object(M, "INDEX", root / "index.md")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        cli("proj-reg", "--project", "ctx", "--surface", "S", "--title", "결합",
            "--pair", "example-backend:feat/ctx-be",
            "--pair", "example-frontend:feat/ctx-fe")

    def ready_task(self, slug="both", repos=None) -> None:
        """Filed, started on the right branches for its target set, and review-ready."""
        run("add", project="ctx", slug=slug, title="둘 다",
            **({"repos": repos} if repos else {}))
        targets = repos or "example-backend,example-frontend"
        names = {"example-backend": "be", "example-frontend": "fe"}
        parts = [r for r in targets.split(",")]
        run("start", slug=slug,
            branch=",".join(f"feat/{slug}-{names[r]}" for r in parts),
            repos=targets if len(parts) > 1 else None)
        run("review", slug=slug)

    def line(self, slug="both") -> str:
        return [l for l in M.tasks_path("ctx").read_text(encoding="utf-8").splitlines()
                if f"slug={slug}" in l][0]

    def test_first_merge_keeps_the_task_in_review(self) -> None:
        self.ready_task()
        out = run("done", slug="both", merged="example-backend")
        self.assertIn("result=partial", out)
        self.assertIn("status=검토 대기", out)
        self.assertIn("remaining=example-frontend", out)
        self.assertIn("merged=example-backend", self.line())

    def test_second_merge_completes_it(self) -> None:
        self.ready_task()
        run("done", slug="both", merged="example-backend")
        out = run("done", slug="both", merged="example-frontend")
        self.assertIn("status=완료", out)
        self.assertIn("merged=example-backend,example-frontend", self.line())

    def test_both_at_once_completes_it(self) -> None:
        self.ready_task()
        out = run("done", slug="both", merged="example-backend,example-frontend")
        self.assertIn("status=완료", out)

    def test_repeating_a_merged_repo_does_not_duplicate_it(self) -> None:
        self.ready_task()
        run("done", slug="both", merged="example-backend")
        out = run("done", slug="both", merged="example-backend")
        self.assertIn("result=partial", out)
        self.assertIn("merged=example-backend ", out + " ")
        self.assertNotIn("example-backend,example-backend", self.line())

    def test_a_repo_the_task_does_not_target_is_refused(self) -> None:
        self.ready_task()
        with self.assertRaises(M.Fail) as cm:
            run("done", slug="both", merged="some-other-repo")
        self.assertIn("타깃하지 않는 repo", str(cm.exception))

    def test_a_subset_task_completes_on_its_own_repo_alone(self) -> None:
        """D7: an FE-only task in a two-repo project is done when FE lands."""
        self.ready_task("fe-only", repos="example-frontend")
        out = run("done", slug="fe-only", merged="example-frontend")
        self.assertIn("status=완료", out)

    def test_single_repo_task_needs_no_merged_flag(self) -> None:
        cli("proj-reg", "--project", "solo", "--surface", "S2", "--title", "단일",
            "--repo", "example-backend", "--branch", "feat/solo")
        run("add", project="solo", slug="plain", title="평범")
        run("start", slug="plain", branch="feat/plain")
        run("review", slug="plain")
        out = run("done", slug="plain")
        self.assertIn("status=완료", out)
        self.assertNotIn("merged=", out)
