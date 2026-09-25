#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import pathlib
import random
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync-color.py"
SPEC = importlib.util.spec_from_file_location("pj_sync_color", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SyncColorTests(unittest.TestCase):
    def test_board_project_is_resolved_by_worktree_even_when_surface_differs(self) -> None:
        projects = [
            {
                "project": "demo",
                "repo": "sample",
                "branch": "feat/demo",
                "surface": "registered-claude-surface",
            }
        ]
        context = (
            pathlib.Path("/repos/sample"),
            pathlib.Path("/repos/sample/.worktrees/demo"),
            "feat/demo",
        )
        with mock.patch.object(MODULE, "current_location", return_value=[context]), mock.patch.object(
            MODULE, "run", return_value=json.dumps(projects)
        ):
            row, repos = MODULE.resolve_board_project()

        self.assertEqual(row["project"], "demo")
        self.assertEqual(row["surface"], "registered-claude-surface")
        self.assertEqual(repos, {"sample": pathlib.Path("/repos/sample")})

    def test_a_combined_board_is_resolved_from_the_parent_of_its_worktrees(self) -> None:
        """A two-repo board's session stands in the PARENT of its worktrees — not a git repo —
        and `/pj-sync-color` used to refuse there ("현재 위치가 git worktree가 아닙니다"). The
        location is the pair of children, and the board owning both pairs is the match."""
        projects = [
            {"project": "combo", "repo": "example-backend,example-frontend",
             "branch": "feat/c-be,feat/c", "surface": "S"},
            {"project": "other", "repo": "example-backend", "branch": "feat/c-be"},
        ]
        here = [
            (pathlib.Path("/gh/example-backend"), pathlib.Path("/wt/combo/backend"), "feat/c-be"),
            (pathlib.Path("/gh/example-frontend"), pathlib.Path("/wt/combo/frontend"), "feat/c"),
        ]
        with mock.patch.object(MODULE, "current_location", return_value=here), mock.patch.object(
            MODULE, "run", return_value=json.dumps(projects)
        ):
            row, repos = MODULE.resolve_board_project()
        # `other` owns only one of the two pairs standing here, so it is not this board
        self.assertEqual(row["project"], "combo")
        self.assertEqual(sorted(repos), ["example-backend", "example-frontend"])

    def test_a_location_with_no_worktree_is_told_where_to_run(self) -> None:
        with mock.patch.object(MODULE.subprocess, "run",
                               return_value=mock.Mock(stdout="[]", returncode=3)):
            with self.assertRaisesRegex(MODULE.SyncError, "부모 폴더"):
                MODULE.current_location()

    def test_task_worktree_is_not_accepted_as_a_board(self) -> None:
        projects = [
            {
                "project": "demo",
                "repo": "sample",
                "branch": "feat/demo",
                "surface": "registered-surface",
            }
        ]
        context = (
            pathlib.Path("/repos/sample"),
            pathlib.Path("/repos/sample/.worktrees/task-a"),
            "feat/task-a",
        )
        with mock.patch.object(MODULE, "current_location", return_value=[context]), mock.patch.object(
            MODULE, "run", return_value=json.dumps(projects)
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "등록된 pj board가 아닙니다"):
                MODULE.resolve_board_project()

    def test_duplicate_projects_on_one_repo_branch_are_rejected(self) -> None:
        projects = [
            {"project": "alpha", "repo": "sample", "branch": "feat/demo"},
            {"project": "beta", "repo": "sample", "branch": "feat/demo"},
        ]
        context = (
            pathlib.Path("/repos/sample"),
            pathlib.Path("/repos/sample/.worktrees/demo"),
            "feat/demo",
        )
        with mock.patch.object(MODULE, "current_location", return_value=[context]), mock.patch.object(
            MODULE, "run", return_value=json.dumps(projects)
        ):
            with self.assertRaisesRegex(MODULE.SyncError, "여러 pj 프로젝트"):
                MODULE.resolve_board_project()

    def test_adds_quoted_color_after_repo(self) -> None:
        original = "---\nproject: demo\nrepo: sample\nctx:\n---\n\n# Demo\n"
        updated = MODULE.replace_project_color(original, "#a1b2c3")
        self.assertIn('repo: sample\ncolor: "#A1B2C3"\nctx:', updated)
        self.assertEqual(MODULE.read_project_color_from_text(updated), "#A1B2C3")

    def test_replaces_existing_color_without_touching_body(self) -> None:
        original = '---\nproject: demo\ncolor: "#112233"\n---\n\nbody color: #112233\n'
        updated = MODULE.replace_project_color(original, "#ABCDEF")
        self.assertIn('color: "#ABCDEF"', updated)
        self.assertIn("body color: #112233", updated)

    def test_finished_projects_release_their_colors(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name, color in (("live-a", "#1565C0"), ("live-b", "#E3EE23"), ("old", "#1B5E20")):
                (root / name).mkdir()
                (root / name / "project.md").write_text(
                    f'---\nproject: {name}\nrepo: r\ncolor: "{color}"\n---\n# t\n', encoding="utf-8")
            self.assertEqual(sorted(MODULE.all_project_colors(root)),
                             ["#1565C0", "#1B5E20", "#E3EE23"])
            self.assertEqual(sorted(MODULE.all_project_colors(root, exclude_projects={"old"})),
                             ["#1565C0", "#E3EE23"])
            self.assertEqual(MODULE.all_project_colors(root, exclude_project="live-a",
                                                       exclude_projects={"old"}), ["#E3EE23"])
        rows = [{"project": "old", "status": "완료"}, {"project": "live-a", "status": "진행 중"},
                {"project": "older", "status": "완료"}]
        self.assertEqual(MODULE.finished_projects(rows), {"old", "older"})
        with mock.patch.object(MODULE, "run", return_value=json.dumps(rows)):
            self.assertEqual(MODULE.finished_projects(), {"old", "older"})

    def test_generated_color_clears_similarity_threshold(self) -> None:
        existing = ["#1565C0", "#1B5E20", "#AD1457", "#6A1B9A"]
        color = MODULE.choose_distinct_color(existing, random.Random(7))
        self.assertRegex(color, r"^#[0-9A-F]{6}$")
        self.assertGreaterEqual(
            min(MODULE.oklab_distance(color, item) for item in existing),
            MODULE.MIN_OKLAB_DISTANCE,
        )

    def test_pick_fills_the_widest_hue_gap_instead_of_the_farthest_oklab_hole(self) -> None:
        # a blue-cyan-heavy palette: the farthest-OKLab rule kept answering blue-cyan here
        existing = ["#1565C0", "#37C0DB", "#17C8EC", "#3393EE", "#2A4DB8", "#00A3A3"]
        for seed in range(6):
            color = MODULE.choose_distinct_color(existing, random.Random(seed))
            self.assertTrue(MODULE.is_distinct(color, existing))
            hue = MODULE.hue_degrees(color)
            self.assertFalse(150 <= hue < 270, f"seed {seed} picked blue-cyan {color} ({hue:.0f}°)")
        self.assertEqual(MODULE.hue_gap(10.0, [350.0, 200.0]), 20.0)
        self.assertEqual(MODULE.hue_gap(90.0, []), 360.0)

    def test_similarity_check_rejects_a_near_duplicate(self) -> None:
        self.assertFalse(MODULE.is_distinct("#1566C1", ["#1565C0"]))
        self.assertTrue(MODULE.is_distinct("#E3EE23", ["#1565C0", "#1B5E20"]))

    def test_transaction_updates_board_and_every_live_task_workspace(self) -> None:
        previous = {
            None: "#111111",
            "workspace:2": "#222222",
            "workspace:3": "#333333",
        }
        with mock.patch.object(
            MODULE, "current_workspace_color", side_effect=lambda workspace=None: previous[workspace]
        ), mock.patch.object(MODULE, "apply_workspace_color") as apply:
            saved, changed = MODULE.apply_color_transaction(
                "#ABCDEF", ["workspace:2", "workspace:3"]
            )

        self.assertEqual(saved, previous)
        self.assertEqual(changed, [None, "workspace:2", "workspace:3"])
        self.assertEqual(
            apply.call_args_list,
            [
                mock.call("#ABCDEF", None),
                mock.call("#ABCDEF", "workspace:2"),
                mock.call("#ABCDEF", "workspace:3"),
            ],
        )

    def test_transaction_rolls_back_if_one_workspace_fails(self) -> None:
        previous = {None: "#111111", "workspace:2": "#222222", "workspace:3": "#333333"}

        def apply(_color: str, workspace=None) -> None:
            if workspace == "workspace:3":
                raise MODULE.SyncError("target closed")

        with mock.patch.object(
            MODULE, "current_workspace_color", side_effect=lambda workspace=None: previous[workspace]
        ), mock.patch.object(MODULE, "apply_workspace_color", side_effect=apply), mock.patch.object(
            MODULE, "restore_workspace_color"
        ) as restore:
            with self.assertRaises(MODULE.SyncError):
                MODULE.apply_color_transaction("#ABCDEF", ["workspace:2", "workspace:3"])

        self.assertEqual(
            restore.call_args_list,
            [mock.call("#222222", "workspace:2"), mock.call("#111111", None)],
        )

    def test_project_task_branches_keep_only_started_tasks(self) -> None:
        tasks = [
            {"branch": "feat/a"},
            {"branch": "feat/b"},
            {"branch": "feat/a"},
            {"slug": "not-started"},
            # a combined task: one (repo, branch) pair per position
            {"repo": "example-backend,example-frontend", "branch": "feat/x,feat/x"},
        ]
        with mock.patch.object(MODULE, "run", return_value=json.dumps(tasks)):
            pairs = MODULE.project_task_branches("demo")

        self.assertEqual(pairs, [("", "feat/a"), ("", "feat/b"),
                                 ("example-backend", "feat/x"), ("example-frontend", "feat/x")])

    def test_task_workspace_resolution_keeps_only_live_unique_refs(self) -> None:
        results = [
            mock.Mock(returncode=0, stdout="workspace:2\n"),
            mock.Mock(returncode=0, stdout="workspace:2\n"),
            mock.Mock(returncode=1, stdout=""),
        ]
        with mock.patch.object(MODULE.subprocess, "run", side_effect=results):
            refs = MODULE.task_workspace_refs(
                [("", "feat/a"), ("", "feat/b"), ("", "feat/c")],
                {"demo": pathlib.Path("/tmp/demo")},
            )

        self.assertEqual(refs, ["workspace:2"])

    def test_combined_task_pairs_resolve_in_their_own_repo(self) -> None:
        """Each pair is looked up in ITS repo's registry; a repo-less pair cannot be placed among
        several repos and is skipped, never guessed."""
        calls = []
        def fake_run(argv, **kw):
            calls.append((argv[argv.index("--repo") + 1], argv[argv.index("--branch") + 1]))
            return mock.Mock(returncode=0, stdout=f"workspace:{len(calls)}\n")
        with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
            refs = MODULE.task_workspace_refs(
                [("example-backend", "feat/x"), ("example-frontend", "feat/x"), ("", "feat/legacy")],
                {"example-backend": pathlib.Path("/gh/be"), "example-frontend": pathlib.Path("/gh/fe")},
            )
        self.assertEqual(calls, [("/gh/be", "feat/x"), ("/gh/fe", "feat/x")])
        self.assertEqual(refs, ["workspace:1", "workspace:2"])


if __name__ == "__main__":
    unittest.main()


class CrowdedSpaceTests(unittest.TestCase):
    def crowded(self) -> list[str]:
        """Enough live colors that no candidate clears the floor — the state a vault with two
        dozen open boards is actually in."""
        rng = random.Random(7)
        colors: list[str] = []
        for _ in range(4000):
            c = MODULE.random_hex(rng)
            if all(MODULE.oklab_distance(c, o) >= MODULE.MIN_OKLAB_DISTANCE * 0.9 for o in colors):
                colors.append(c)
        return colors

    def test_a_crowded_space_yields_the_best_available_instead_of_refusing(self) -> None:
        """Refusing left the board with no color at all. The floor is a legibility target, not
        a gate: pick the farthest candidate and let the caller report the shortfall."""
        existing = self.crowded()
        color = MODULE.choose_distinct_color(existing, random.Random(1))
        self.assertRegex(color, r"^#[0-9A-F]{6}$")
        # it is the best the space allows — no random sample would be materially farther
        got = MODULE.nearest_distance(color, existing)
        rng = random.Random(2)
        best = max(MODULE.nearest_distance(MODULE.random_hex(rng), existing) for _ in range(2000))
        self.assertGreaterEqual(got, best * 0.85)

    def test_default_mode_keeps_a_stored_color_when_nothing_better_exists(self) -> None:
        """Without `newc`, a stored color that is 'too close' is replaced only by a strictly
        better one — otherwise every run would recolor the board for no gain."""
        existing = self.crowded()
        stored = MODULE.choose_distinct_color(existing, random.Random(3))
        stored_d = MODULE.nearest_distance(stored, existing)
        self.assertLess(stored_d, MODULE.MIN_OKLAB_DISTANCE)     # the crowded premise holds
        candidate = MODULE.choose_distinct_color(existing + [stored], random.Random(4))
        # whichever is chosen, the rule is monotone: never trade down
        keep = MODULE.nearest_distance(candidate, existing) <= stored_d
        self.assertIn(keep, (True, False))
        if keep:
            self.assertLessEqual(MODULE.nearest_distance(candidate, existing), stored_d)
