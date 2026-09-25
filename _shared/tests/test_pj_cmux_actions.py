#!/usr/bin/env python3
"""Semantic-action validation, launcher policy, and the request marker contract."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import shlex
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-cmux.py"
SPEC = importlib.util.spec_from_file_location("pj_cmux", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

TASK = {"project": "pjtest", "repo": "demo-repo", "branch": "feat/t-one",
        "project_branch": "feat/base", "surface": "BOARD-SURF", "slug": "t-one",
        "status": "진행 중"}


def ns(action: str, **kw) -> argparse.Namespace:
    base = {"slug": "t-one", "project": None, "stdin": False, "role": None, "round": None,
            "reply_to": None, "launcher": None, "findings": None,
            "commit": None,
            "typecheck": None, "mode": None, "planner": None, "worker": None,
            "reviewer": None, "name": None, "worktree": None, "branch": None,
            "description": None,
            "switch": None, "planner_prompt": None, "solo": False}
    base.update(kw)
    return argparse.Namespace(action=action, **base)


class ActionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tasks_dir = pathlib.Path(self.tmp.name) / "raw" / "tasks"
        self.tasks_dir.mkdir(parents=True)
        self.patches = [
            mock.patch.object(MODULE, "TASKS_DIR", self.tasks_dir),
            mock.patch.object(MODULE, "TEST", True),
            # marker-contract tests exercise the codex path; inline relay is tested separately
            mock.patch.object(MODULE, "should_relay_inline", lambda: False),
            mock.patch.object(MODULE, "pj_tasks_json",
                              lambda *a: TASK if a[0] == "get" else {"surface": "BOARD-SURF"}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        MODULE.EXECUTED.clear()

    def request(self, action: str, stdin: str | None = None, **kw) -> tuple[int, str]:
        out = io.StringIO()
        ctx = mock.patch("sys.stdin", io.StringIO(stdin)) if stdin is not None \
            else contextlib.nullcontext()
        with ctx, contextlib.redirect_stdout(out):
            rc = MODULE.op_request(ns(action, stdin=stdin is not None, **kw))
        return rc, out.getvalue()

    def stream(self, role: str, slug: str | None = "t-one") -> list[dict]:
        return MODULE.read_stream(MODULE.stream_path("pjtest", slug, role))

    def record_completed_workspace_open(self, reviewer: str) -> None:
        path = MODULE.stream_path("pjtest", None, "board")
        opened = MODULE.make_event(
            "workspace_open", "board", "board", "t-one",
            {"launchers": {"plan": "claude", "work": "claude",
                           "review": reviewer}},
            request_id=MODULE.new_id("req"))
        MODULE.append_event(path, opened)
        MODULE.append_event(path, MODULE.make_event(
            "action_completed", "board", "board", "t-one", {},
            reply_to=opened["event_id"]))

    def record_reusable_reviewer(self, launcher: str = "codex") -> None:
        MODULE.append_event(
            MODULE.stream_path("pjtest", "t-one", "reviewer"),
            MODULE.make_event(
                "role_attached", "reviewer", "reviewer", "t-one",
                {"workspace": "WS", "surface": "REVIEWER-SURF", "launcher": launcher,
                 "reviewer_mode": "reuse", "round": 1},
                round_=1, reply_to="evt_" + "a" * 16))


class LauncherPolicyTests(ActionTestBase):
    def test_role_defaults(self) -> None:
        self.assertEqual(MODULE.parse_launcher("plan", "", None), ("claude", "default"))
        self.assertEqual(MODULE.parse_launcher("work", "", None), ("claude", "default"))
        self.assertEqual(MODULE.parse_launcher("review", "", None), ("claude", "default"))

    def test_role_defaults_per_runtime(self) -> None:
        """The same role resolves to a different launcher per runtime column — that pairing is
        the whole reason the table is two-dimensional."""
        self.assertEqual(MODULE.parse_launcher("plan", "", None, "codex")[0], "codex")
        self.assertEqual(MODULE.parse_launcher("work", "", None, "codex")[0], "codex")
        self.assertEqual(MODULE.parse_launcher("review", "", None, "codex")[0], "codex")
        # omitted runtime is the default column, never an error: every pre-existing caller
        # passes three arguments and must keep getting the column it always got.
        self.assertEqual(MODULE.parse_launcher("review", "", None),
                         MODULE.parse_launcher("review", "", None, "claude"))

    def test_reviewers_are_folders_not_a_list_in_this_file(self) -> None:
        """Adding a reviewer is adding a folder. Nothing in pj-cmux.py enumerates them, so this
        walks the directory and holds every definition to its schema — a definition that cannot
        answer one of the transport's questions is the failure this catches, not a KeyError at
        review time."""
        roles = MODULE.reviewer_roles()
        self.assertIn("code", roles)
        self.assertIn("plan", roles)
        for role in roles:
            spec = MODULE.reviewer_def(role)
            for key in MODULE.REVIEWER_KEYS:
                self.assertIn(key, spec, f"{role}.{key}")
            self.assertIn(spec["reply_type"], MODULE.EVENT_TYPES, role)
            self.assertIn(spec["to"], MODULE.ROLES, role)
            self.assertIn(spec["stream"], MODULE.task_streams(), role)
            self.assertIsInstance(spec["default"], bool, role)
            self.assertIsInstance(spec["solo"], bool, role)
            if spec["spawn"]:
                # a spawning reviewer needs a bootable launcher in EVERY runtime column and a
                # lens file in its OWN folder — either missing is a reviewer that cannot start
                self.assertTrue((MODULE.REVIEWER_DIR / role / spec["prompt"]).is_file(), role)
                for runtime in MODULE.RUNTIMES:
                    code = MODULE.reviewer_launcher(role, runtime)
                    self.assertTrue(MODULE.valid_launcher(code), f"{role}/{runtime}: {code}")
            else:
                self.assertIsNone(spec["launchers"], role)

    def test_a_reviewer_is_never_a_session_role(self) -> None:
        for role in MODULE.reviewer_roles():
            if role in MODULE.ROLE_KEYS:
                continue
            with self.assertRaises(MODULE.Fail):
                MODULE.parse_launcher(role, "", None)

    def test_unknown_reviewer_and_path_escape_are_refused(self) -> None:
        for bad in ("no-such-reviewer", "../code", "code/..", ""):
            with self.assertRaises(MODULE.Fail):
                MODULE.reviewer_def(bad)

    def test_review_launcher_is_the_default_reviewers_own_value(self) -> None:
        """`launchers.review` on a workspace must not be a second copy of the code reviewer's
        launcher — it resolves to the definition, so the two can never disagree."""
        for runtime in MODULE.RUNTIMES:
            self.assertEqual(MODULE.default_launcher("review", runtime),
                             MODULE.reviewer_launcher(MODULE.DEFAULT_REVIEWER, runtime))

    def test_every_table_entry_is_a_real_launcher(self) -> None:
        """LAUNCHER_TABLE now holds ONLY the roles a workspace boots. A reviewer id appearing
        here would mean its launcher is stated twice."""
        for key, codes in MODULE.LAUNCHER_TABLE.items():
            self.assertEqual(len(codes), len(MODULE.RUNTIMES), key)
            for code in codes:
                self.assertTrue(MODULE.valid_launcher(code), f"{key}: {code}")
        self.assertEqual(set(MODULE.LAUNCHER_TABLE), {"plan", "work"})
        self.assertNotIn(MODULE.DEFAULT_REVIEWER, MODULE.LAUNCHER_TABLE)
        self.assertIn(MODULE.DEFAULT_RUNTIME, MODULE.RUNTIMES)

    def test_unknown_runtime_and_unknown_key_are_refused(self) -> None:
        with self.assertRaises(MODULE.Fail):
            MODULE.default_launcher("review", "gemini")
        with self.assertRaises(MODULE.Fail):
            MODULE.default_launcher("no-such-role")

    def test_alias_beats_default_and_explicit_beats_alias(self) -> None:
        with mock.patch.object(MODULE, "repo_alias",
                               return_value={"roleLaunchers": {"review": "codex"}}):
            self.assertEqual(MODULE.parse_launcher("review", "", "demo-repo"),
                             ("codex", "alias"))
            self.assertEqual(MODULE.parse_launcher("review", "codex", "demo-repo"),
                             ("codex", "explicit"))

    def test_explicit_profiles_and_no_model_guessing(self) -> None:
        self.assertEqual(MODULE.parse_launcher("work", "codex", None), ("codex", "explicit"))
        self.assertEqual(MODULE.parse_launcher("plan", "grok", None)[0], "grok")
        for hint in ("low", "opus", "model high", "claude; echo wrong"):
            with self.assertRaises(MODULE.Fail):
                MODULE.parse_launcher("work", hint, None)

    def test_launcher_uses_bundled_wrapper(self) -> None:
        import shlex
        argv = shlex.split(MODULE.launcher_shell_command("codex"))
        self.assertTrue(argv[1].endswith("pj-launch.py"))
        self.assertEqual(argv[-3:], ["--profile", "codex", "--"])

    def test_review_option_uses_each_reviewers_configured_profile(self) -> None:
        profiles = dict(MODULE.launcher_profiles())
        profiles["grok"]["reviewers"] = {"database": "grok-database"}
        profiles["grok-database"] = {"runtime": "grok", "argv": ["grok"], "reviewOption": "grok"}
        with mock.patch.object(MODULE, "launcher_profiles", return_value=profiles):
            self.assertEqual(MODULE.parse_launcher("review", "grok", None, reviewer="database"),
                             ("grok-database", "explicit"))
            self.assertEqual(MODULE.parse_launcher("review", "grok", None, reviewer="code"),
                             ("grok", "explicit"))
            with self.assertRaises(MODULE.Fail):
                MODULE.parse_launcher("work", "grok", None, reviewer="database")

    def test_custom_account_group_does_not_depend_on_profile_name(self) -> None:
        profiles = dict(MODULE.launcher_profiles())
        profiles["team-account"] = {"runtime": "codex", "argv": ["codex"],
                                    "reviewOption": "team-account"}
        profiles["precise-code"] = {"runtime": "codex", "argv": ["codex"],
                                   "reviewOption": "team-account"}
        with mock.patch.object(MODULE, "launcher_profiles", return_value=profiles):
            self.assertEqual(MODULE.review_option("precise-code"), "team-account")
            self.assertEqual(MODULE.reviewer_launcher("security", "team-account"), "team-account")
            self.assertEqual(MODULE.reviewer_launcher("grok", "team-account"), "grok")
            self.assertEqual(MODULE.sigil_for("team-account"), "$")

    def test_native_grok_surface_validation(self) -> None:
        tree = ('surface surface:7 AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE '
                '[terminal] "task - grok"\n')
        top = ('0\t1\t1\tprocess\t123\tsurface:7\tgrok-1.0.0-macos-aarch64\n')
        with mock.patch.object(MODULE, "run_cmux", side_effect=[tree, top]):
            MODULE.validate_target("WS", "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
                                   "grok")

    def test_native_grok_board_workspace_resolution(self) -> None:
        tree = (
            'workspace workspace:2 11111111-2222-3333-4444-555555555555 "board"\n'
            '  surface surface:7 AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE '
            '[terminal] "board - grok"\n')
        with mock.patch.object(MODULE, "run_cmux", return_value=tree):
            self.assertEqual(
                MODULE.workspace_for_native_surface(
                    "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"),
                "11111111-2222-3333-4444-555555555555")

    def test_invalid_tokens_and_unsupported_efforts_refused(self) -> None:
        with self.assertRaises(MODULE.Fail):
            MODULE.parse_launcher("work", "banana", None)
        with self.assertRaises(MODULE.Fail):
            MODULE.parse_launcher("review", "luna ultra", None)
        with self.assertRaises(MODULE.Fail):
            MODULE.parse_launcher("work", "missing-profile", None)
        self.assertFalse(MODULE.valid_launcher("missing-profile"))


class RequestTests(ActionTestBase):
    def test_done_report_marker_is_last_stdout_line(self) -> None:
        rc, out = self.request("done.report", commit="ok")
        self.assertEqual(rc, 0)
        last = out.strip().splitlines()[-1]
        self.assertRegex(last, r"^PJ_CMUX_REQUEST=req_[0-9a-f]{16}$")
        events = self.stream("worker")
        self.assertEqual(events[0]["type"], "done_report")
        self.assertEqual(events[0]["payload"]["commit"], "ok")
        self.assertEqual(events[0]["payload"]["typecheck"], "none")

    def test_done_report_rejects_bad_enums(self) -> None:
        for commit, typecheck in (("ok", "maybe"), ("meh", "ok")):
            with self.assertRaises(MODULE.Fail) as cm:
                self.request("done.report", commit=commit, typecheck=typecheck)
            self.assertEqual(cm.exception.code, 1)

    def test_optional_incomplete_typecheck_can_be_reported_honestly(self) -> None:
        self.request("done.report", commit="ok", typecheck="unparsed")
        self.assertEqual(self.stream("worker")[-1]["payload"]["typecheck"], "unparsed")

    def test_handoff_validates_paths_and_bounds(self) -> None:
        ok = json.dumps({"cautions": ["절대 DS 컴포넌트를 직접 수정하지 말 것"],
                         "references": [{"path": "docs/plan.md", "reason": "설계 근거"}]})
        rc, out = self.request("worker.handoff", stdin=ok)
        self.assertEqual(rc, 0)
        ev = self.stream("planner")[0]
        self.assertEqual(ev["type"], "worker_handoff")
        self.assertTrue(ev["payload"]["plan_path"].endswith("pjtest/t-one.md"))
        for bad in (
            {"cautions": [], "references": [{"path": "/abs/path", "reason": "r"}]},
            {"cautions": [], "references": [{"path": "../escape.md", "reason": "r"}]},
            {"cautions": ["x" * 301], "references": []},
            {"cautions": "not-a-list", "references": []},
        ):
            with self.assertRaises(MODULE.Fail):
                self.request("worker.handoff", stdin=json.dumps(bad))

    def test_review_group_is_local_and_unused_round_is_reused(self) -> None:
        body = json.dumps({"summary": "구현 요약", "diff_base": "feat/base",
                           "diff_head": "feat/t-one"})
        rc, out = self.request("review.group.open", stdin=body)
        self.assertEqual(rc, 0)
        self.assertNotIn("PJ_CMUX_REQUEST=", out)   # local: no relay marker
        self.assertIn("PJ_CMUX=ok action=review.group.open", out)
        # reopening before any request is a retry, not a second pass — same round
        self.request("review.group.open", stdin=body)
        rounds = [e["round"] for e in self.stream("worker")
                  if e["type"] == "review_group_opened"]
        self.assertEqual(rounds, [1, 1])
        # once the round has been used, reopening advances it
        self.request("review.request", role="code")
        self.request("review.group.open", stdin=body)
        rounds = [e["round"] for e in self.stream("worker")
                  if e["type"] == "review_group_opened"]
        self.assertEqual(rounds, [1, 1, 2])

    def test_skip_and_disposition_inherit_the_round_in_flight(self) -> None:
        body = json.dumps({"summary": "s", "diff_base": "feat/base",
                           "diff_head": "feat/t-one"})
        self.request("review.group.open", stdin=body)
        self.request("review.request", role="code")
        self.request("review.group.open", stdin=body)          # → round 2
        self.request("review.skip", role="plan",
                     stdin=json.dumps({"reason": "플래너 세션 종료"}))
        self.request("review.disposition", reply_to="evt_" + "a" * 16,
                     stdin=json.dumps({"dispositions": [
                         {"finding_index": 0, "disposition": "accepted", "note": ""}]}))
        by_type = {e["type"]: e["round"] for e in self.stream("worker")}
        self.assertEqual(by_type["review_skipped"], 2)
        self.assertEqual(by_type["review_disposition"], 2)

    def test_review_request_needs_group_and_defaults_reviewer(self) -> None:
        with self.assertRaises(MODULE.Fail):
            self.request("review.request", role="code")
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        rc, out = self.request("review.request", role="code")
        self.assertEqual(rc, 0)
        ev = [e for e in self.stream("worker") if e["type"] == "review_requested"][0]
        self.assertEqual(ev["payload"]["launcher"], "claude")
        # the request carries no session policy: there is only one, and it is reuse
        self.assertNotIn("reviewer_mode", ev["payload"])
        self.assertEqual(ev["to"], "reviewer")

    def test_hard_mode_is_not_requestable(self) -> None:
        """Every review is reuse-based. The isolated-per-request policy is gone, including its
        flag — a caller that still passes it must fail loudly at the CLI rather than have it
        silently ignored, which would look like an honored request for isolation."""
        self.assertFalse(hasattr(MODULE, "REVIEWER_MODES"))
        with mock.patch("sys.argv", ["pj-cmux.py", "request", "review.request",
                                     "--slug", "t-one", "--role", "code",
                                     "--review-mode", "hard"]), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                MODULE.main()

    def test_default_reuses_stable_reviewer_launcher(self) -> None:
        self.record_completed_workspace_open("claude")
        self.record_reusable_reviewer("codex")
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        self.request("review.request", role="code")
        ev = [e for e in self.stream("worker") if e["type"] == "review_requested"][0]
        self.assertEqual(ev["payload"]["launcher"], "codex")

    def test_stable_reviewer_launcher_change_is_refused_outright(self) -> None:
        """The task's reviewer session is fixed once. With hard mode gone there is no escape
        hatch, so the refusal has to say what the user can actually do instead."""
        self.record_reusable_reviewer("codex")
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        with self.assertRaises(MODULE.Fail) as cm:
            self.request("review.request", role="code", launcher="claude")
        self.assertIn("codex", str(cm.exception))
        self.assertIn("닫고", str(cm.exception))
        # the same launcher is not a change and is accepted
        self.request("review.request", role="code", launcher="codex")
        ev = [e for e in self.stream("worker") if e["type"] == "review_requested"][0]
        self.assertEqual(ev["payload"]["launcher"], "codex")

    def test_review_request_uses_reviewer_recorded_at_workspace_open(self) -> None:
        self.record_completed_workspace_open("claude")
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        self.request("review.request", role="code")
        ev = [e for e in self.stream("worker") if e["type"] == "review_requested"][0]
        self.assertEqual(ev["payload"]["launcher"], "claude")

    def test_explicit_review_override_beats_workspace_reviewer(self) -> None:
        self.record_completed_workspace_open("claude")
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        self.request("review.request", role="code", launcher="codex")
        ev = [e for e in self.stream("worker") if e["type"] == "review_requested"][0]
        self.assertEqual(ev["payload"]["launcher"], "codex")

    def test_review_reply_schema_is_role_specific(self) -> None:
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        code = json.dumps({"findings": [{"severity": "blocking", "location": "a.ts:1",
                                         "finding": "f", "evidence": "e",
                                         "recommended_change": "r"}]})
        rc, _ = self.request("review.reply", role="code", stdin=code)
        self.assertEqual(self.stream("reviewer")[0]["type"], "code_review")
        # plan replies need plan_reference, not location
        with self.assertRaises(MODULE.Fail):
            self.request("review.reply", role="plan", stdin=code)
        plan = json.dumps({"findings": [{"severity": "important", "plan_reference": "목표",
                                         "finding": "f", "evidence": "e",
                                         "recommended_change": "r"}]})
        rc, _ = self.request("review.reply", role="plan", stdin=plan)
        self.assertEqual(self.stream("planner")[0]["type"], "plan_review")

    def test_review_reply_prose_fields_are_not_length_capped(self) -> None:
        # Two real reviews were bounced for 1071- and 1593-char evidence; findings only ever
        # reach the worker through `event read`, so the prose is kept whole.
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        long = "x" * 5000
        rc, _ = self.request("review.reply", role="code", stdin=json.dumps(
            {"findings": [{"severity": "important", "location": "a.ts:1", "finding": long,
                           "evidence": long, "recommended_change": long}]}))
        self.assertEqual(rc, 0)
        self.assertEqual(self.stream("reviewer")[0]["payload"]["findings"][0]["evidence"], long)

    def test_disposition_requires_reply_to_and_valid_enum(self) -> None:
        good = json.dumps({"dispositions": [
            {"finding_index": 0, "disposition": "accepted", "note": ""},
            {"finding_index": 1, "disposition": "rejected", "note": "라인이 디프에 없음"}]})
        with self.assertRaises(MODULE.Fail):   # missing reply_to
            self.request("review.disposition", stdin=good)
        rc, out = self.request("review.disposition", stdin=good,
                               reply_to="evt_" + "a" * 16)
        self.assertEqual(rc, 0)
        self.assertNotIn("PJ_CMUX_REQUEST=", out)
        bad = json.dumps({"dispositions": [{"finding_index": 0, "disposition": "shrug"}]})
        with self.assertRaises(MODULE.Fail):
            self.request("review.disposition", stdin=bad, reply_to="evt_" + "a" * 16)

    def test_workspace_open_validates_launchers_switches_prompt(self) -> None:
        kw = dict(planner="claude", worker="claude", reviewer="codex",
                  name="FE/t-one", worktree="/abs/wt", branch="feat/t-one",
                  planner_prompt="Run /pj-plan t-one.")
        rc, out = self.request("workspace.open", **kw)
        self.assertEqual(rc, 0)
        self.assertEqual(self.stream("board", slug=None)[0]["type"], "workspace_open")
        with self.assertRaises(MODULE.Fail):
            self.request("workspace.open", **{**kw, "worker": "sonnet"})  # not normalized
        with self.assertRaises(MODULE.Fail):
            self.request("workspace.open", **{**kw, "switch": ["yolo"]})
        with self.assertRaises(MODULE.Fail):
            self.request("workspace.open", **{**kw, "planner_prompt": ""})

    def test_decision_request_and_reply_validation(self) -> None:
        req = json.dumps({"plan_reference": "작업 단계 3", "question": "A안? B안?",
                          "options": ["A", "B"]})
        rc, _ = self.request("decision.request", stdin=req)
        self.assertEqual(self.stream("worker")[0]["type"], "decision_request")
        with self.assertRaises(MODULE.Fail):   # decision.reply needs reply_to
            self.request("decision.reply", stdin=json.dumps({"decision": "A"}))


class SoloTopologyTests(ActionTestBase):
    """solo: planner and worker are one session — the transport must know, refuse the two
    cross-role requests that have no counterpart, and never make split tasks behave differently."""

    def attach(self, role: str, surface: str, **extra) -> None:
        MODULE.append_event(
            MODULE.stream_path("pjtest", "t-one", role),
            MODULE.make_event("role_attached", role, role, "t-one",
                              {"workspace": "WS", "surface": surface,
                               "launcher": "claude", **extra}))

    def open_group(self) -> None:
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))

    def ws_open(self, **kw) -> tuple[int, str]:
        base = dict(planner="claude", reviewer="codex", name="BE/t-one",
                    branch="feat/t-one", worktree="/repos/demo/.worktrees/t-one",
                    planner_prompt="Run /pj-plan t-one. Solo session. 한국어로.")
        base.update(kw)
        return self.request("workspace.open", **base)

    def test_solo_open_omits_worker_and_records_the_flag(self) -> None:
        rc, _ = self.ws_open(solo=True)
        self.assertEqual(rc, 0)
        ev = self.stream("board", slug=None)[-1]
        self.assertTrue(ev["payload"]["solo"])
        self.assertEqual(ev["payload"]["launchers"]["work"], "claude")  # planner's

    def test_solo_open_refuses_a_different_worker_launcher(self) -> None:
        with self.assertRaises(MODULE.Fail) as cm:
            self.ws_open(solo=True, worker="codex")
        self.assertEqual(cm.exception.code, 1)
        rc, _ = self.ws_open(solo=True, worker="claude")   # same as planner: harmless
        self.assertEqual(rc, 0)

    def test_split_open_still_requires_worker_and_records_solo_false(self) -> None:
        with self.assertRaises(MODULE.Fail):
            self.ws_open()                                      # no --worker, no --solo
        rc, _ = self.ws_open(worker="claude")
        self.assertEqual(rc, 0)
        self.assertFalse(self.stream("board", slug=None)[-1]["payload"]["solo"])

    def test_topology_reads_flag_then_falls_back_to_shared_surface(self) -> None:
        self.assertEqual(MODULE.task_topology("pjtest", "t-one"), "unknown")
        self.attach("planner", "P-SURF")
        self.attach("worker", "W-SURF")
        self.assertEqual(MODULE.task_topology("pjtest", "t-one"), "split")
        self.attach("planner", "ONE-SURF")                      # pre-flag records, one surface
        self.attach("worker", "ONE-SURF")
        self.assertEqual(MODULE.task_topology("pjtest", "t-one"), "solo")
        self.attach("worker", "W2-SURF", solo=False)            # the flag wins over position
        self.assertEqual(MODULE.task_topology("pjtest", "t-one"), "split")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MODULE.op_topology(argparse.Namespace(slug="t-one"))
        self.assertEqual(out.getvalue().strip(), "PJ_TOPOLOGY=split")

    def test_solo_refuses_plan_review_and_decision_request_but_not_code_review(self) -> None:
        self.attach("planner", "ONE-SURF", solo=True)
        self.attach("worker", "ONE-SURF", solo=True)
        self.open_group()
        with self.assertRaises(MODULE.Fail) as cm:
            self.request("review.request", role="plan")
        self.assertIn("review.skip", str(cm.exception))
        with self.assertRaises(MODULE.Fail) as cm:
            self.request("decision.request", stdin=json.dumps(
                {"plan_reference": "step 2", "question": "q", "options": ["a", "b"]}))
        self.assertIn("solo", str(cm.exception))
        rc, _ = self.request("review.request", role="code")     # the independent reviewer stays
        self.assertEqual(rc, 0)
        rc, _ = self.request("review.skip", role="plan", stdin=json.dumps({"reason": "solo"}))
        self.assertEqual(rc, 0)

    def test_split_task_keeps_plan_review_and_decision_request(self) -> None:
        self.attach("planner", "P-SURF", solo=False)
        self.attach("worker", "W-SURF", solo=False)
        self.open_group()
        rc, _ = self.request("review.request", role="plan")
        self.assertEqual(rc, 0)
        rc, _ = self.request("decision.request", stdin=json.dumps(
            {"plan_reference": "step 2", "question": "q", "options": ["a", "b"]}))
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()


class ReviewerSelectionTests(ActionTestBase):
    """Which reviewers a task runs is decided ONCE, by the planner, at the end of planning.

    The worker does not choose. It receives a plan and dispatches the selection recorded on the
    handoff — so these tests pin the selection's path from `worker.handoff` through the round's
    expected roles to what `review.request` will accept.
    """

    def handoff(self, reviewers=None) -> None:
        body = {"cautions": [], "references": []}
        if reviewers is not None:
            body["reviewers"] = reviewers
        self.request("worker.handoff", stdin=json.dumps(body))

    def group(self) -> dict:
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        return [e for e in self.stream("worker")
                if e["type"] == "review_group_opened"][-1]

    def test_defaults_run_without_the_planner_choosing_anything(self) -> None:
        self.handoff()
        self.assertEqual(self.group()["payload"]["roles"], ["code", "plan"])

    def test_planner_selection_joins_the_defaults(self) -> None:
        self.handoff(["security", "react"])
        self.assertEqual(self.group()["payload"]["roles"],
                         ["code", "plan", "react", "security"])

    def test_all_task_reviewers_follow_the_recorded_option(self) -> None:
        self.handoff(["security", "database"])
        self.group()
        profiles = dict(MODULE.launcher_profiles())
        profiles["precise-code"] = {"runtime": "grok", "argv": ["grok"]}
        profiles["grok"]["reviewers"] = {"database": "db-review"}
        profiles["db-review"] = {"runtime": "grok", "argv": ["grok"]}
        with mock.patch.object(MODULE, "launcher_profiles", return_value=profiles), \
             mock.patch.object(MODULE, "recorded_workspace_launcher", return_value="precise-code"):
            for role, expected in (("code", "precise-code"), ("security", "grok"), ("database", "db-review")):
                self.request("review.request", role=role)
                event = [e for e in self.stream("worker") if e["type"] == "review_requested"][-1]
                self.assertEqual(event["payload"]["launcher"], expected)

    def test_grok_is_an_additional_reviewer_with_its_own_default_in_both_runtimes(self) -> None:
        self.handoff(["grok", "security"])
        self.assertEqual(self.group()["payload"]["roles"],
                         ["code", "grok", "plan", "security"])
        for runtime in MODULE.RUNTIMES:
            with self.subTest(runtime=runtime), \
                 mock.patch.object(MODULE, "task_runtime", return_value=runtime):
                self.request("review.request", role="grok")
                ev = [e for e in self.stream("worker") if e["type"] == "review_requested"][-1]
                self.assertEqual(ev["payload"]["launcher"], "grok")
                self.assertEqual(ev["payload"]["role"], "grok")
        self.assertEqual(MODULE.reviewer_def("grok")["stream"], "reviewer-grok")

    def test_grok_reviewer_accepts_configured_profile_override(self) -> None:
        self.handoff(["grok"])
        self.group()
        profiles = dict(MODULE.launcher_profiles())
        profiles["cross-check"] = {"runtime": "grok", "argv": ["grok", "--model", "chosen-model"]}
        with mock.patch.object(MODULE, "launcher_profiles", return_value=profiles):
            launcher, source = MODULE.parse_launcher("review", "cross-check", None)
            self.assertEqual(source, "explicit")
            self.request("review.request", role="grok", launcher=launcher)
            event = [e for e in self.stream("worker") if e["type"] == "review_requested"][-1]
            self.assertEqual(event["payload"]["launcher"], "cross-check")
            self.assertIn("--profile cross-check --", MODULE.launcher_shell_command(launcher))
        with self.assertRaises(MODULE.Fail):
            MODULE.parse_launcher("review", "unconfigured-model high", None)

    def test_a_handoff_without_the_field_is_not_an_empty_selection(self) -> None:
        """An older handoff, or one from a planner that picked nothing, must still run the
        defaults — absence of the field is 'no optional reviewers', never 'no reviewers'."""
        MODULE.append_event(
            MODULE.stream_path("pjtest", "t-one", "planner"),
            MODULE.make_event("worker_handoff", "planner", "worker", "t-one",
                              {"cautions": [], "references": []}, round_=0))
        self.assertEqual(self.group()["payload"]["roles"], ["code", "plan"])

    def test_default_reviewers_cannot_be_selected(self) -> None:
        """Letting the planner pass `code` would imply it could also leave it out."""
        for r in ("code", "plan"):
            with self.assertRaises(MODULE.Fail):
                self.handoff([r])

    def test_unknown_reviewer_in_a_selection_is_refused_at_handoff(self) -> None:
        """Caught where the planner can still fix it, not at review time two hours later."""
        with self.assertRaises(MODULE.Fail):
            self.handoff(["securty"])

    def test_duplicates_collapse_and_the_list_is_bounded(self) -> None:
        self.assertEqual(
            MODULE.validate_reviewer_selection(["security", "security", "react"]),
            ["security", "react"])
        with self.assertRaises(MODULE.Fail):
            MODULE.validate_reviewer_selection(["security"] * 9)
        with self.assertRaises(MODULE.Fail):
            MODULE.validate_reviewer_selection("security")

    def test_requesting_an_unselected_reviewer_is_refused(self) -> None:
        """The worker dispatches the planner's selection; a reviewer nobody chose would be an
        opinion the plan never asked for — and a pane the user did not expect."""
        self.handoff(["security"])
        self.group()
        self.request("review.request", role="security")     # selected → accepted
        with self.assertRaises(MODULE.Fail) as cm:
            self.request("review.request", role="react")
        self.assertIn("계획 종료 시", str(cm.exception))

    def test_each_reviewer_gets_its_own_stream(self) -> None:
        """Two reviewers in one round must not read each other's attachment: with one shared
        file, 'whose session is this' needs payload parsing, and the wrong answer delivers a
        security request into the code reviewer's session."""
        streams = {r: MODULE.reviewer_def(r)["stream"] for r in MODULE.reviewer_roles()
                   if MODULE.reviewer_def(r)["spawn"]}
        self.assertEqual(len(set(streams.values())), len(streams), streams)
        self.assertEqual(streams["code"], "reviewer")       # 기존 기록이 있는 스트림


class ReviewGroupDiffRefsTests(ActionTestBase):
    def commands(self, base: str, head: str) -> list[list[str]]:
        prompt = MODULE.diff_ref_lines(base, head)
        return [shlex.split(part) for part in prompt.split("`")[1::2]
                if part.startswith("python3 ")]

    def test_working_tree_helper_is_quoted_in_a_relocated_package(self) -> None:
        scripts = pathlib.Path(self.tmp.name) / "package ' with spaces" / "scripts"
        with mock.patch.object(MODULE, "SCRIPT_DIR", scripts):
            self.assertEqual(self.commands("project", "task"), [
                ["python3", str(scripts / "pj-review-diff.py"),
                 "--base", "project", "--head", "task"]])

    def test_bare_base_expands_to_each_named_head(self) -> None:
        commands = self.commands("project", "be:task-be,fe:task-fe")
        self.assertEqual([cmd[-4:] for cmd in commands], [
            ["--base", "project", "--head", "task-be"],
            ["--base", "project", "--head", "task-fe"]])

    def test_named_refs_pair_by_repo_and_bare_head_applies_to_all(self) -> None:
        commands = self.commands("be:base-be,fe:base-fe", "fe:task-fe,be:task-be")
        self.assertEqual([cmd[-4:] for cmd in commands], [
            ["--base", "base-be", "--head", "task-be"],
            ["--base", "base-fe", "--head", "task-fe"]])
        commands = self.commands("be:base-be,fe:base-fe", "task")
        self.assertEqual([cmd[-4:] for cmd in commands], [
            ["--base", "base-be", "--head", "task"],
            ["--base", "base-fe", "--head", "task"]])

    def test_missing_or_ambiguous_repo_refs_are_not_replaced_by_head(self) -> None:
        for base, head in [("be:base,fe:base", "be:task"),
                           ("be:base", "be:task,fe:task"),
                           ("be:base,fe:base", "be:task,other:task"),
                           ("be:base,be:other", "task"),
                           ("base", "be:task,be:other"),
                           ("base,be:base", "task"),
                           ("base", "task,fe:task")]:
            with self.subTest(base=base, head=head), self.assertRaises(MODULE.Fail):
                self.commands(base, head)

    def test_group_accepts_a_per_repo_base_and_still_a_bare_one(self) -> None:
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "example-backend:feat/x-be,example-frontend:feat/x",
             "diff_head": "feat/t-one"}))
        groups = [e for e in self.stream("worker") if e["type"] == "review_group_opened"]
        self.assertEqual(groups[-1]["payload"]["diff_base"],
                         "example-backend:feat/x-be,example-frontend:feat/x")
        for bad in ("feat/x; rm -rf", "a:b:c", ",feat/x", "feat/x,"):
            with self.assertRaises(MODULE.Fail):
                self.request("review.group.open", stdin=json.dumps(
                    {"summary": "s", "diff_base": bad, "diff_head": "feat/t-one"}))


class TaskRuntimeTests(ActionTestBase):
    """`task_runtime` decides which LAUNCHER_TABLE column a task's reviewers come from. The rule
    is deliberately plain: `--runtime` as given, else claude — never inferred from launchers."""

    def open_ws(self, slug: str, runtime: str | None) -> None:
        payload = {"launchers": {"plan": "codex", "work": "codex", "review": "codex"}}
        if runtime:
            payload["runtime"] = runtime
        MODULE.append_event(MODULE.stream_path("pjtest", None, "board"),
                            MODULE.make_event("workspace_open", "board", "board", slug, payload,
                                              request_id=MODULE.new_id("req")))

    def test_recorded_runtime_is_used_as_given(self) -> None:
        self.open_ws("t-one", "codex")
        self.assertEqual(MODULE.task_runtime("pjtest", "t-one"), "codex")

    def test_absent_runtime_is_claude_even_with_codex_launchers(self) -> None:
        """Codex planner and worker, no --runtime: still claude. The column is a decision made at
        kickoff, and the transport does not second-guess it from the launchers."""
        self.open_ws("t-one", None)
        self.assertEqual(MODULE.task_runtime("pjtest", "t-one"), "claude")

    def test_another_tasks_open_does_not_leak_its_runtime(self) -> None:
        """The board stream is project-wide. Found by review: the newest workspace_open in it
        belonged to whichever task was opened LAST, and this task inherited that runtime."""
        self.open_ws("t-one", "claude")
        self.open_ws("t-two", "codex")                     # opened later, different task
        self.assertEqual(MODULE.task_runtime("pjtest", "t-one"), "claude")
        self.assertEqual(MODULE.task_runtime("pjtest", "t-two"), "codex")
        self.assertEqual(MODULE.task_runtime("pjtest", "t-never-opened"), "claude")

    def test_reviewer_launcher_follows_the_tasks_column(self) -> None:
        self.open_ws("t-one", "codex")
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        self.request("review.request", role="code")
        ev = [e for e in self.stream("worker") if e["type"] == "review_requested"][0]
        self.assertEqual(ev["payload"]["launcher"], "codex")
