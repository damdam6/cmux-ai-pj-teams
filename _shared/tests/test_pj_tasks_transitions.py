#!/usr/bin/env python3
"""Monotonic-transition matrix for pj-tasks.py start/review/done.

Runs against a TemporaryDirectory patched in as TASKS_DIR/INDEX — never the live vault.
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
SPEC = importlib.util.spec_from_file_location("pj_tasks", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def ns(op: str, **kw) -> argparse.Namespace:
    base = {"project": None, "repo": None, "slug": None, "title": None, "branch": None,
            "surface": None, "force_branch": False, "deps": None, "status": None,
            "repos": None, "pair": None, "merged": None}
    base.update(kw)
    return argparse.Namespace(op=op, **base)


def run(op: str, **kw) -> str:
    """Invoke one op, return its stdout line(s)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = MODULE.OPS[op](ns(op, **kw))
    assert rc == 0
    return out.getvalue().strip()


class TransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tasks_dir = pathlib.Path(self.tmp.name) / "raw" / "tasks"
        self.patches = [
            mock.patch.object(MODULE, "TASKS_DIR", tasks_dir),
            mock.patch.object(MODULE, "INDEX", tasks_dir / "index.md"),
            mock.patch.object(MODULE, "ALIASES",
                              pathlib.Path(self.tmp.name) / "no-aliases.json"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)
        run("proj-reg", project="pjtest", branch="feat/pj-base",
            surface="AAAA-1111", repo="demo-repo", title="테스트 프로젝트")
        run("add", project="pjtest", slug="t-one", title="작업 하나")

    def tasks_text(self) -> str:
        return (MODULE.TASKS_DIR / "pjtest" / "tasks.md").read_text(encoding="utf-8")

    def transition(self, op: str, **kw):
        fn = {"start": MODULE.op_start, "review": MODULE.op_review, "done": MODULE.op_done}[op]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fn(ns(op, slug="t-one", **kw))
        return rc, out.getvalue().strip()

    def assert_refused(self, op: str, **kw):
        with self.assertRaises(MODULE.Fail) as cm:
            self.transition(op, **kw)
        self.assertEqual(cm.exception.code, 3)

    # --- start ---

    def test_start_moves_from_todo_and_records_branch(self) -> None:
        rc, out = self.transition("start", branch="feat/t-one")
        self.assertEqual(rc, 0)
        self.assertIn("PJ_TASKS=ok op=start", out)
        self.assertIn("status=진행 중", out)
        self.assertNotIn("result=", out)
        self.assertIn("branch=feat/t-one", self.tasks_text())

    def test_start_same_branch_retry_is_noop_with_zero_writes(self) -> None:
        self.transition("start", branch="feat/t-one")
        before = self.tasks_text()
        rc, out = self.transition("start", branch="feat/t-one")
        self.assertEqual(rc, 0)
        self.assertIn("result=noop", out)
        self.assertIn("status=진행 중", out)
        self.assertEqual(self.tasks_text(), before)

    def test_start_different_branch_is_refused_and_branch_kept(self) -> None:
        self.transition("start", branch="feat/t-one")
        self.assert_refused("start", branch="feat/other")
        self.assertIn("branch=feat/t-one", self.tasks_text())
        self.assertNotIn("feat/other", self.tasks_text())

    def test_start_refused_from_review_and_done(self) -> None:
        self.transition("start", branch="feat/t-one")
        self.transition("review")
        self.assert_refused("start", branch="feat/t-one")
        self.transition("done")
        self.assert_refused("start", branch="feat/t-one")

    # --- review ---

    def test_review_refused_from_todo(self) -> None:
        self.assert_refused("review")

    def test_review_moves_then_noop(self) -> None:
        self.transition("start", branch="feat/t-one")
        rc, out = self.transition("review")
        self.assertIn("status=검토 대기", out)
        before = self.tasks_text()
        rc, out = self.transition("review")
        self.assertEqual(rc, 0)
        self.assertIn("result=noop", out)
        self.assertEqual(self.tasks_text(), before)

    def test_review_after_done_is_already_ahead(self) -> None:
        self.transition("start", branch="feat/t-one")
        self.transition("review")
        self.transition("done")
        before = self.tasks_text()
        rc, out = self.transition("review")
        self.assertEqual(rc, 0)
        self.assertIn("result=already-ahead", out)
        self.assertIn("status=완료", out)
        self.assertEqual(self.tasks_text(), before)

    # --- done ---

    def test_done_refused_from_todo_and_in_progress(self) -> None:
        self.assert_refused("done")
        self.transition("start", branch="feat/t-one")
        self.assert_refused("done")

    def test_done_moves_then_noop_without_restamping_closed(self) -> None:
        self.transition("start", branch="feat/t-one")
        self.transition("review")
        rc, out = self.transition("done")
        self.assertIn("status=완료", out)
        before = self.tasks_text()
        rc, out = self.transition("done")
        self.assertEqual(rc, 0)
        self.assertIn("result=noop", out)
        self.assertEqual(self.tasks_text(), before)

    # --- output compatibility ---

    def test_mutation_lines_keep_the_legacy_prefix_shape(self) -> None:
        rc, out = self.transition("start", branch="feat/t-one")
        self.assertTrue(out.startswith("PJ_TASKS=ok op=start project=pjtest slug=t-one"))
        rc, out = self.transition("start", branch="feat/t-one")
        self.assertTrue(out.startswith("PJ_TASKS=ok op=start project=pjtest slug=t-one"))
        # result= is appended after the existing keys so k=v parsers stay compatible
        self.assertTrue(out.endswith("result=noop"))


if __name__ == "__main__":
    unittest.main()
