#!/usr/bin/env python3
"""Relay behavior: target validation, dedup, failure records, spawn/workspace compilation.

Runs entirely in test mode (MODULE.TEST) — run_cmux/run_helper record argv into
MODULE.EXECUTED and execute nothing.
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
SPEC = importlib.util.spec_from_file_location("pj_cmux", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

TASK = {"project": "pjtest", "repo": "demo-repo", "branch": "feat/t-one",
        "project_branch": "feat/base", "surface": "BOARD-SURF", "slug": "t-one",
        "status": "진행 중"}
WS = "11111111-1111-1111-1111-111111111111"


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


class RelayTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.tasks_dir = root / "raw" / "tasks"
        (self.tasks_dir / "pjtest").mkdir(parents=True)
        self.registry = root / "cmux-agents"
        self.registry.mkdir()
        self.patches = [
            mock.patch.object(MODULE, "TASKS_DIR", self.tasks_dir),
            mock.patch.object(MODULE, "REGISTRY_ROOT", str(self.registry)),
            mock.patch.object(MODULE, "LOCK_ROOT", str(root / "locks")),
            mock.patch.object(MODULE, "TEST", True),
            # request() here must stay pending so relay_request can be exercised explicitly
            mock.patch.object(MODULE, "should_relay_inline", lambda: False),
            mock.patch.object(MODULE, "pj_tasks_json",
                              lambda *a: TASK if a[0] == "get" else {"surface": "BOARD-SURF"}),
            mock.patch.object(MODULE, "pid_command", lambda pid: "claude --resume"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        MODULE.EXECUTED.clear()

    def register_agent(self, surface: str, ws: str = WS, kind: str | None = "claude") -> None:
        d = self.registry / ws
        d.mkdir(exist_ok=True)
        lines = [f"ts=1786400000", "state=idle", "pid=4242", f"surface={surface}",
                 f"cwd={self.tmp.name}"]
        if kind:
            lines.append(f"kind={kind}")
        (d / "session-x").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def request(self, action: str, stdin: str | None = None, **kw) -> str:
        out = io.StringIO()
        ctx = mock.patch("sys.stdin", io.StringIO(stdin)) if stdin is not None \
            else contextlib.nullcontext()
        with ctx, contextlib.redirect_stdout(out):
            MODULE.op_request(ns(action, stdin=stdin is not None, **kw))
        text = out.getvalue()
        for line in text.splitlines():
            if line.startswith("PJ_CMUX_REQUEST="):
                return line.split("=", 1)[1]
        return ""

    def sends(self) -> list[list[str]]:
        return [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "send"]]

    def stream(self, role: str, slug: str | None = "t-one") -> list[dict]:
        return MODULE.read_stream(MODULE.stream_path("pjtest", slug, role))

    def boot_line(self) -> str:
        """The one line a spawned reviewer boots on: sent into a Claude TUI, or the argument of
        a Codex/Grok command line."""
        sent = [a[-1] for a in self.sends() if a[-1].startswith("[pj-review-boot]")]
        buffered = [a[-1] for a in MODULE.EXECUTED if a[:2] == ["cmux", "set-buffer"]]
        return (sent or buffered)[0]

    def boot_prompt(self, role: str) -> str:
        """What `event prompt` hands that reviewer for its newest review_requested event."""
        ev = [e for e in self.stream("worker") if e["type"] == "review_requested"
              and e["payload"]["role"] == role][-1]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MODULE.op_event(argparse.Namespace(slug="t-one", mode="prompt",
                                               event_id=ev["event_id"], role=None,
                                               type=None, round=None))
        return out.getvalue()


class DoneReportRelayTests(RelayTestBase):
    def test_delivered_once_and_duplicate_relay_sends_nothing(self) -> None:
        self.register_agent("BOARD-SURF")
        rid = self.request("done.report", commit="ok", typecheck="ok")
        msg = MODULE.relay_request(rid, "t-one")
        self.assertIn("delivered", msg)
        sends = self.sends()
        self.assertEqual(len(sends), 2)  # wake-up line + \r
        line = sends[0][-1]
        self.assertRegex(line, r"^\[pj-event-ready\] project=pjtest slug=t-one "
                               r"source=worker event=evt_[0-9a-f]{16}$")
        count_before = len(MODULE.EXECUTED)
        msg2 = MODULE.relay_request(rid, "t-one")
        self.assertIn("already", msg2)
        self.assertEqual(len(MODULE.EXECUTED), count_before)
        types = [e["type"] for e in self.stream("worker")]
        self.assertEqual(types.count("delivered"), 1)

    def test_dead_pid_records_delivery_failed_and_sends_nothing(self) -> None:
        self.register_agent("BOARD-SURF")
        rid = self.request("done.report", commit="ok", typecheck="ok")
        with mock.patch.object(MODULE, "pid_command", lambda pid: ""):
            with self.assertRaises(MODULE.Fail) as cm:
                MODULE.relay_request(rid, "t-one")
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(self.sends(), [])
        fails = [e for e in self.stream("worker") if e["type"] == "delivery_failed"]
        self.assertEqual(len(fails), 1)
        self.assertEqual(fails[0]["payload"]["target_role"], "board")

    def test_shell_only_surface_is_refused(self) -> None:
        # no registry entry at all — a bare shell never registers
        rid = self.request("done.report", commit="ok", typecheck="ok")
        with self.assertRaises(MODULE.Fail) as cm:
            MODULE.relay_request(rid, "t-one")
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(self.sends(), [])

    def test_codex_target_needs_codex_pid(self) -> None:
        self.register_agent("BOARD-SURF", kind="codex")
        rid = self.request("done.report", commit="ok", typecheck="ok")
        with mock.patch.object(MODULE, "pid_command", lambda pid: "claude --resume"):
            with self.assertRaises(MODULE.Fail):
                MODULE.relay_request(rid, "t-one")
        with mock.patch.object(MODULE, "pid_command", lambda pid: "codex exec"):
            self.assertIn("delivered", MODULE.relay_request(rid, "t-one"))

    def test_uncertain_crash_retry_is_harmless_and_ack_is_idempotent(self) -> None:
        self.register_agent("BOARD-SURF")
        rid = self.request("done.report", commit="ok", typecheck="ok")
        MODULE.relay_request(rid, "t-one")
        # crash window: delivered record lost → a retried relay sends again (harmless)
        with mock.patch.object(MODULE, "find_terminal", return_value=None):
            MODULE.relay_request(rid, "t-one")
        self.assertEqual(len(self.sends()), 4)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MODULE.op_ack(argparse.Namespace(slug="t-one", request_id=rid,
                                             status="processed"))
        with contextlib.redirect_stdout(out):
            MODULE.op_ack(argparse.Namespace(slug="t-one", request_id=rid,
                                             status="processed"))
        self.assertIn("result=duplicate", out.getvalue())
        with self.assertRaises(MODULE.Fail) as cm:
            MODULE.op_ack(argparse.Namespace(slug="t-one", request_id=rid,
                                             status="rejected"))
        self.assertEqual(cm.exception.code, 4)
        acks = [e for e in self.stream("board", slug=None) if e["type"] == "ack"]
        self.assertEqual(len(acks), 1)  # board acks land in board.jsonl, exactly once


class HandoffAndReviewRelayTests(RelayTestBase):
    def attach_role(self, role: str, surface: str) -> None:
        MODULE.append_event(
            MODULE.stream_path("pjtest", "t-one", role),
            MODULE.make_event("role_attached", role, role, "t-one",
                              {"workspace": WS, "surface": surface, "launcher": "claude"}))

    def test_handoff_targets_recorded_worker_surface(self) -> None:
        self.attach_role("worker", "WORKER-SURF")
        self.register_agent("WORKER-SURF")
        rid = self.request("worker.handoff",
                           stdin=json.dumps({"cautions": [], "references": []}))
        MODULE.relay_request(rid, "t-one")
        self.assertIn("source=planner", self.sends()[0][-1])
        self.assertIn("--surface", self.sends()[0])
        self.assertEqual(self.sends()[0][self.sends()[0].index("--surface") + 1],
                         "WORKER-SURF")

    def test_review_reply_falls_back_to_requester_on_old_topology(self) -> None:
        # pre-separation: no worker role_attached; the group request recorded its session
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        self.register_agent("LEAD-SURF")
        rid = self.request("review.reply", role="code",
                           stdin=json.dumps({"findings": []}))
        MODULE.relay_request(rid, "t-one")
        send = self.sends()[0]
        self.assertEqual(send[send.index("--surface") + 1], "LEAD-SURF")
        self.assertIn("source=reviewer", send[-1])

    def test_planner_gone_records_failure_never_falls_back(self) -> None:
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        rid = self.request("review.request", role="plan")
        with self.assertRaises(MODULE.Fail) as cm:
            MODULE.relay_request(rid, "t-one")
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(self.sends(), [])
        fails = [e for e in self.stream("worker") if e["type"] == "delivery_failed"]
        self.assertEqual(fails[0]["payload"]["target_role"], "planner")

    def test_code_review_spawn_compiles_boot_and_attaches_reviewer(self) -> None:
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "요약입니다", "diff_base": "feat/base",
                 "diff_head": "feat/t-one"}))
            rid = self.request("review.request", role="code")
        MODULE.relay_request(rid, "t-one")
        argv_heads = [a[1] for a in MODULE.EXECUTED if a[0] == "cmux"]
        self.assertIn("new-surface", argv_heads)
        # Nothing multi-line is pasted: a multi-KB paste lost whole sections on the way in.
        self.assertNotIn("set-buffer", argv_heads)
        self.assertNotIn("paste-buffer", argv_heads)
        # The reviewer default is a Claude launcher, so boot_agent sends the launcher first
        # and the one boot line only after the session is ready.
        sent = [a[-1] for a in MODULE.EXECUTED if a[:2] == ["cmux", "send"]]
        self.assertIn(MODULE.launcher_shell_command("claude"), sent)
        line = self.boot_line()
        self.assertNotIn("\n", line)
        self.assertLess(sent.index(MODULE.launcher_shell_command("claude")), sent.index(line))
        ev = [e for e in self.stream("worker") if e["type"] == "review_requested"][-1]
        self.assertIn(f"{MODULE.SCRIPT_DIR / 'pj-cmux.py'} event prompt --slug t-one "
                      f"--event-id {ev['event_id']}", line)

        prompt = self.boot_prompt("code")
        self.assertIn("review.reply", prompt)   # rendered prompt tells the reply
        self.assertIn("persistent code reviewer session", prompt)
        # the prompt is assembled: shared contract, then THIS reviewer's lens
        self.assertIn("Your lens", prompt)
        self.assertIn("senior code reviewer", prompt)
        # and the lens must not carry an output contract of its own
        for banned in ("Verdict:", "Review Summary", "Approval Criteria"):
            self.assertNotIn(banned, prompt, banned)
        self.assertIn("event read", prompt)
        self.assertIn("A prose review printed in chat is NOT a reply", prompt)
        self.assertGreater(prompt.rfind("request review.reply"), prompt.rfind("Later requests"))
        self.assertGreater(prompt.rfind("DO NOT END THIS REVIEW TURN"),
                           prompt.rfind("request review.reply"))
        self.assertNotIn("최초 요청", prompt)
        attached = [e for e in self.stream("reviewer") if e["type"] == "role_attached"]
        self.assertEqual(attached[0]["payload"]["launcher"], "claude")
        self.assertEqual(attached[0]["payload"]["reviewer_mode"], "reuse")
        requested = [e for e in self.stream("worker") if e["type"] == "review_requested"]
        self.assertEqual(attached[0]["reply_to"], requested[0]["event_id"])
        delivered = [e for e in self.stream("worker") if e["type"] == "delivered"]
        self.assertEqual(delivered[0]["payload"]["role"], "code")

    def test_event_prompt_refuses_non_review_requests(self) -> None:
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        group = [e for e in self.stream("worker") if e["type"] == "review_group_opened"][-1]
        with self.assertRaises(MODULE.Fail) as cm:
            MODULE.op_event(argparse.Namespace(slug="t-one", mode="prompt",
                                               event_id=group["event_id"], role=None,
                                               type=None, round=None))
        self.assertEqual(cm.exception.code, 1)

    def test_later_default_review_wakes_the_task_scoped_reviewer(self) -> None:
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "round 1", "diff_base": "feat/base",
                 "diff_head": "feat/t-one"}))
            first = self.request("review.request", role="code")
        MODULE.relay_request(first, "t-one")
        self.register_agent("TEST-NEW-SURFACE", kind="claude")
        MODULE.EXECUTED.clear()

        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "round 2", "diff_base": "feat/base",
                 "diff_head": "feat/t-two"}))
            second = self.request("review.request", role="code")
        with mock.patch.object(MODULE, "pid_command", lambda pid: "claude --resume"):
            MODULE.relay_request(second, "t-one")

        argv_heads = [a[1] for a in MODULE.EXECUTED if a[0] == "cmux"]
        self.assertNotIn("new-surface", argv_heads)
        self.assertEqual(len(self.sends()), 2)
        self.assertIn("source=worker", self.sends()[0][-1])
        self.assertIn("required_action=read-event-then-execute-review.reply-before-ending",
                      self.sends()[0][-1])
        event = [e for e in self.stream("worker") if e["type"] == "review_requested"][-1]
        self.assertIn(f"event prompt --slug t-one --event-id {event['event_id']}",
                      self.sends()[0][-1])
        prompt = self.boot_prompt("code")
        self.assertIn("pj-review-diff.py --base feat/base --head feat/t-two", prompt)
        self.assertNotIn("--head feat/t-one", prompt)
        self.assertIn(str(event["payload"]["requester"]["cwd"]), prompt)
        self.assertEqual(self.sends()[0][self.sends()[0].index("--surface") + 1],
                         "TEST-NEW-SURFACE")
        attached = [e for e in self.stream("reviewer") if e["type"] == "role_attached"]
        self.assertEqual(len(attached), 1)
        delivered = [e for e in self.stream("worker") if e["type"] == "delivered"]
        self.assertTrue(delivered[-1]["payload"]["reused"])

    def test_every_request_reuses_the_one_task_reviewer(self) -> None:
        """One reviewer per task, for every round. The first request spawns it; every later one
        wakes that same surface and must NOT open another — with hard mode gone, a second
        `new-surface` here would mean a reviewer nobody asked for."""
        def round_request(summary: str) -> str:
            with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                              "CMUX_SURFACE_ID": "LEAD-SURF"}):
                self.request("review.group.open", stdin=json.dumps(
                    {"summary": summary, "diff_base": "feat/base",
                     "diff_head": "feat/t-one"}))
                return self.request("review.request", role="code")

        first = round_request("round 1")
        MODULE.relay_request(first, "t-one")
        self.assertIn("new-surface", [a[1] for a in MODULE.EXECUTED if a[0] == "cmux"])
        self.register_agent("TEST-NEW-SURFACE", kind="claude")

        for summary in ("round 2", "round 3"):
            rid = round_request(summary)
            MODULE.EXECUTED.clear()
            with mock.patch.object(MODULE, "pid_command", lambda pid: "claude --resume"):
                MODULE.relay_request(rid, "t-one")
            self.assertNotIn("new-surface",
                             [a[1] for a in MODULE.EXECUTED if a[0] == "cmux"], summary)

        attached = [e for e in self.stream("reviewer") if e["type"] == "role_attached"]
        self.assertEqual(len(attached), 1)
        self.assertEqual(attached[0]["payload"]["reviewer_mode"], "reuse")

    def test_a_legacy_hard_reviewer_record_is_never_reused(self) -> None:
        """The streams still hold `role_attached` records from the removed hard mode. Those
        sessions were built to answer one request and stop, so waking one as this task's
        reviewer would type into a session that is not listening — the recorded
        `reviewer_mode` value is what keeps them out."""
        MODULE.append_event(
            MODULE.stream_path("pjtest", "t-one", "reviewer"),
            MODULE.make_event("role_attached", "reviewer", "reviewer", "t-one",
                              {"workspace": WS, "surface": "OLD-HARD-SURF",
                               "launcher": "codex", "reviewer_mode": "hard"}))
        self.assertIsNone(
            MODULE.latest_reusable_reviewer_attached("pjtest", "t-one", "code"))
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
            rid = self.request("review.request", role="code")
        MODULE.relay_request(rid, "t-one")
        # a fresh reviewer was opened rather than the legacy surface being woken
        self.assertIn("new-surface", [a[1] for a in MODULE.EXECUTED if a[0] == "cmux"])
        sent_to = {a[a.index("--surface") + 1] for a in MODULE.EXECUTED
                   if a[:2] == ["cmux", "send"] and "--surface" in a}
        self.assertNotIn("OLD-HARD-SURF", sent_to)

    def test_missing_stable_reviewer_fails_instead_of_silently_spawning(self) -> None:
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "round 1", "diff_base": "feat/base",
                 "diff_head": "feat/t-one"}))
            first = self.request("review.request", role="code")
        MODULE.relay_request(first, "t-one")
        MODULE.EXECUTED.clear()
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "round 2", "diff_base": "feat/base",
                 "diff_head": "feat/t-one"}))
            second = self.request("review.request", role="code")
        with self.assertRaises(MODULE.Fail) as cm:
            MODULE.relay_request(second, "t-one")
        self.assertEqual(cm.exception.code, 3)
        self.assertNotIn("new-surface", [a[1] for a in MODULE.EXECUTED if a[0] == "cmux"])

    def test_code_review_spawn_compiles_grok_reviewer(self) -> None:
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "요약입니다", "diff_base": "feat/base",
                 "diff_head": "feat/t-one"}))
            rid = self.request("review.request", role="code", launcher="grok")
        MODULE.relay_request(rid, "t-one")
        buffer_call = [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "set-buffer"]][0]
        self.assertIn("--profile grok --", buffer_call[-1])
        self.assertIn("[pj-review-boot]", buffer_call[-1])   # the boot line is its argument
        self.assertIn("event prompt --slug t-one", buffer_call[-1])
        self.assertNotIn("\n", buffer_call[-1])
        self.assertIn("review.reply", self.boot_prompt("code"))


class LocalActionRelayTests(RelayTestBase):
    def test_markdown_open_compiles_viewer_argv(self) -> None:
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "PLANNER-SURF"}):
            rid = self.request("markdown.open")
        MODULE.relay_request(rid, "t-one")
        call = [a for a in MODULE.EXECUTED if a[:3] == ["cmux", "markdown", "open"]][0]
        self.assertTrue(call[3].endswith("pjtest/t-one.md"))
        self.assertIn(WS, call)

    def test_workspace_open_compiles_layout_color_and_attaches_both_roles(self) -> None:
        (self.tasks_dir / "pjtest" / "project.md").write_text(
            '---\nproject: pjtest\nrepo: demo-repo\ncolor: "#1565C0"\n---\n# t\n',
            encoding="utf-8")
        rid = self.request(
            "workspace.open", planner="claude", worker="claude",
            reviewer="codex", name="FE/t-one", branch="feat/t-one",
            worktree="/repos/demo/.worktrees/t-one", description="설명",
            switch=["skip-review"], planner_prompt="Run /pj-plan t-one. 한국어로.")
        MODULE.relay_request(rid, "t-one")
        ws_call = [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "new-workspace"]][0]
        layout = ws_call[ws_call.index("--layout") + 1]
        self.assertIn("--profile claude --", layout)
        boot = self.tasks_dir / "pjtest/exchanges/t-one/boot"
        waiting = next(boot.glob("worker-*.txt")).read_text()
        self.assertIn("/pj-work t-one", waiting)
        self.assertIn("Skip the review", waiting)
        self.assertNotIn("Skip the review", layout)
        self.assertEqual(next(boot.glob("planner-*.txt")).read_text(),
                         "Run /pj-plan t-one. 한국어로.")
        self.assertEqual(layout.count('"pane"'), 2)          # two panes, no color helper
        self.assertNotIn("external color helper", layout)
        color_calls = [a for a in MODULE.EXECUTED
                       if a[:2] == ["cmux", "workspace-action"] and "set-color" in a]
        self.assertEqual(color_calls[0][color_calls[0].index("--color") + 1], "#1565C0")
        reg_calls = [a for a in MODULE.EXECUTED if a[0].endswith("wt-registry.py")]
        self.assertEqual(reg_calls[0][reg_calls[0].index("--repo") + 1], "/repos/demo")
        for role in ("planner", "worker"):
            att = [e for e in self.stream(role) if e["type"] == "role_attached"]
            self.assertEqual(len(att), 1)

    def test_workspace_open_compiles_grok_without_shell_aliases(self) -> None:
        rid = self.request(
            "workspace.open", planner="grok", worker="grok",
            reviewer="grok", name="FE/t-one", branch="feat/t-one",
            worktree="/repos/demo/.worktrees/t-one",
            planner_prompt="Run /pj-plan t-one. 한국어로.")
        MODULE.relay_request(rid, "t-one")
        ws_call = [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "new-workspace"]][0]
        layout = ws_call[ws_call.index("--layout") + 1]
        self.assertIn("--profile grok --", layout)
        waiting = next((self.tasks_dir / "pjtest/exchanges/t-one/boot").glob("worker-*.txt"))
        self.assertIn("/pj-work t-one", waiting.read_text())

    def test_retry_prints_marker_only_while_pending(self) -> None:
        self.register_agent("BOARD-SURF")
        rid = self.request("done.report", commit="ok", typecheck="ok")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MODULE.op_retry(argparse.Namespace(request_id=rid, slug="t-one"))
        self.assertIn(f"PJ_CMUX_REQUEST={rid}", out.getvalue())
        MODULE.relay_request(rid, "t-one")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MODULE.op_retry(argparse.Namespace(request_id=rid, slug="t-one"))
        self.assertIn("already-delivered", out.getvalue())
        self.assertNotIn(f"PJ_CMUX_REQUEST={rid}", out.getvalue())

    def test_inline_relay_delivers_at_request_time(self) -> None:
        # Claude/shell path: hooks are codex-only, so request relays in-process — the caller
        # sees delivered in the same output and no marker is printed
        self.register_agent("BOARD-SURF")
        out = io.StringIO()
        with mock.patch.object(MODULE, "should_relay_inline", lambda: True), \
                contextlib.redirect_stdout(out):
            rc = MODULE.op_request(ns("done.report", commit="ok", typecheck="ok"))
        self.assertEqual(rc, 0)
        self.assertIn("PJ_CMUX=delivered", out.getvalue())
        self.assertNotIn("PJ_CMUX_REQUEST=", out.getvalue())
        self.assertEqual(len(self.sends()), 2)

    def test_inline_relay_failure_is_visible_and_retriable(self) -> None:
        # no registry entry → target validation fails; request exits 3 with the retry command
        out = io.StringIO()
        with mock.patch.object(MODULE, "should_relay_inline", lambda: True), \
                contextlib.redirect_stdout(out):
            rc = MODULE.op_request(ns("done.report", commit="ok", typecheck="ok"))
        self.assertEqual(rc, 3)
        self.assertIn("PJ_CMUX=failed", out.getvalue())
        self.assertIn("retry --request-id", out.getvalue())
        fails = [e for e in self.stream("worker") if e["type"] == "delivery_failed"]
        self.assertEqual(len(fails), 1)

    def test_status_folds_pending_delivered_processed(self) -> None:
        self.register_agent("BOARD-SURF")
        rid = self.request("done.report", commit="ok", typecheck="ok")

        def state() -> str:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                MODULE.op_status(argparse.Namespace(request_id=rid, slug="t-one",
                                                    round=None))
            return json.loads(out.getvalue())["state"]

        self.assertEqual(state(), "pending")
        MODULE.relay_request(rid, "t-one")
        self.assertEqual(state(), "delivered")
        with contextlib.redirect_stdout(io.StringIO()):
            MODULE.op_ack(argparse.Namespace(slug="t-one", request_id=rid,
                                             status="processed"))
        self.assertEqual(state(), "processed")


class SoloRelayTests(RelayTestBase):
    def attach(self, role: str, surface: str, solo: bool) -> None:
        MODULE.append_event(
            MODULE.stream_path("pjtest", "t-one", role),
            MODULE.make_event("role_attached", role, role, "t-one",
                              {"workspace": WS, "surface": surface,
                               "launcher": "claude", "solo": solo}))

    def test_solo_workspace_open_compiles_one_pane_and_attaches_both_roles_to_it(self) -> None:
        (self.tasks_dir / "pjtest" / "project.md").write_text(
            '---\nproject: pjtest\nrepo: demo-repo\ncolor: "#1565C0"\n---\n# t\n',
            encoding="utf-8")
        rid = self.request(
            "workspace.open", solo=True, planner="claude", reviewer="codex",
            name="BE/t-one", branch="feat/t-one", worktree="/repos/demo/.worktrees/t-one",
            planner_prompt="Run /pj-plan t-one. Solo session. 한국어로.")
        MODULE.relay_request(rid, "t-one")
        ws_call = [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "new-workspace"]][0]
        layout = json.loads(ws_call[ws_call.index("--layout") + 1])
        self.assertEqual(list(layout), ["pane"])                # root leaf: one pane
        self.assertIn("claude", layout["pane"]["surfaces"][0]["command"])
        self.assertNotIn("pj-work", json.dumps(layout))         # no waiting prompt anywhere
        color_calls = [a for a in MODULE.EXECUTED
                       if a[:2] == ["cmux", "workspace-action"] and "set-color" in a]
        self.assertEqual(color_calls[0][color_calls[0].index("--color") + 1], "#1565C0")
        att = {role: [e for e in self.stream(role) if e["type"] == "role_attached"]
               for role in ("planner", "worker")}
        self.assertEqual(len(att["planner"]), 1)
        self.assertEqual(len(att["worker"]), 1)
        self.assertEqual(att["planner"][0]["payload"]["surface"],
                         att["worker"][0]["payload"]["surface"])
        self.assertTrue(att["worker"][0]["payload"]["solo"])
        self.assertEqual(att["worker"][0]["payload"]["launcher"], "claude")
        self.assertEqual(MODULE.task_topology("pjtest", "t-one"), "solo")
        # the plan viewer is booted right away, beside the session, and recorded as side pane
        md = [a for a in MODULE.EXECUTED if a[:3] == ["cmux", "markdown", "open"]]
        self.assertEqual(len(md), 1)
        self.assertTrue(md[0][3].endswith("pjtest/t-one.md"))
        self.assertEqual(md[0][md[0].index("--surface") + 1], "TEST-SOLO-SURFACE")
        self.assertEqual(md[0][md[0].index("--direction") + 1], "right")
        for role in ("planner", "worker"):
            self.assertEqual(att[role][0]["payload"]["side_pane"], "TEST-SIDE-PANE")
        self.assertEqual(MODULE.side_pane("pjtest", "t-one"),
                         ("TEST-SIDE-PANE", "TEST-SIDE-SURFACE"))
        self.assertEqual([a for a in MODULE.EXECUTED if a[:2] == ["cmux", "move-surface"]], [])

    def test_split_workspace_open_puts_the_plan_viewer_in_the_worker_pane(self) -> None:
        rid = self.request(
            "workspace.open", planner="claude", worker="claude",
            reviewer="codex", name="FE/t-one", branch="feat/t-one",
            worktree="/repos/demo/.worktrees/t-one",
            planner_prompt="Run /pj-plan t-one. 한국어로.")
        MODULE.relay_request(rid, "t-one")
        md = [a for a in MODULE.EXECUTED if a[:3] == ["cmux", "markdown", "open"]]
        self.assertEqual(len(md), 1)
        self.assertEqual(md[0][md[0].index("--surface") + 1], "TEST-WORKER-SURFACE")  # not the planner
        mv = [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "move-surface"]]
        self.assertEqual(len(mv), 1)
        self.assertEqual(mv[0][mv[0].index("--surface") + 1], "TEST-SIDE-SURFACE")
        self.assertEqual(mv[0][mv[0].index("--pane") + 1], "TEST-WORKER-PANE")
        for role in ("planner", "worker"):
            att = [e for e in self.stream(role) if e["type"] == "role_attached"][0]
            self.assertEqual(att["payload"]["side_pane"], "TEST-WORKER-PANE")
            self.assertEqual(att["payload"]["side_surface"], "TEST-SIDE-SURFACE")
        self.assertEqual(MODULE.side_pane("pjtest", "t-one"),
                         ("TEST-WORKER-PANE", "TEST-SIDE-SURFACE"))

    def test_solo_handoff_is_recorded_as_delivered_without_typing_into_the_session(self) -> None:
        self.attach("planner", "ONE-SURF", solo=True)
        self.attach("worker", "ONE-SURF", solo=True)
        rid = self.request("worker.handoff",
                           stdin=json.dumps({"cautions": ["c"], "references": []}))
        msg = MODULE.relay_request(rid, "t-one")
        self.assertEqual(self.sends(), [])                      # nothing typed anywhere
        self.assertIn("(self)", msg)
        term = [e for e in self.stream("planner") if e["type"] == "delivered"][-1]
        self.assertTrue(term["payload"]["self"])
        self.assertEqual(term["payload"]["target_role"], "worker")
        self.assertEqual(term["payload"]["surface"], "ONE-SURF")
        # a retry is the usual no-op
        self.assertIn("already delivered", MODULE.relay_request(rid, "t-one"))

    def test_solo_inline_request_prints_self_marker(self) -> None:
        self.attach("planner", "ONE-SURF", solo=True)
        self.attach("worker", "ONE-SURF", solo=True)
        out = io.StringIO()
        with mock.patch.object(MODULE, "should_relay_inline", lambda: True), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({"cautions": [], "references": []}))), \
                contextlib.redirect_stdout(out):
            rc = MODULE.op_request(ns("worker.handoff", stdin=True))
        self.assertEqual(rc, 0)
        self.assertIn("PJ_CMUX=delivered request=", out.getvalue())
        self.assertIn(" self=true", out.getvalue())

    def test_code_review_reply_still_reaches_the_solo_session(self) -> None:
        self.attach("planner", "ONE-SURF", solo=True)
        self.attach("worker", "ONE-SURF", solo=True)
        self.register_agent("ONE-SURF")
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        rid = self.request("review.reply", role="code", stdin=json.dumps({"findings": []}))
        MODULE.relay_request(rid, "t-one")
        send = self.sends()[0]
        self.assertEqual(send[send.index("--surface") + 1], "ONE-SURF")
        self.assertIn("source=reviewer", send[-1])


class SidePaneTests(RelayTestBase):
    """The plan viewer's pane is the task's side pane: documents open there as tabs, and a solo
    task's reviewer boots below it instead of hiding as a tab behind the working session."""

    def attach(self, role: str, surface: str, solo: bool) -> None:
        MODULE.append_event(
            MODULE.stream_path("pjtest", "t-one", role),
            MODULE.make_event("role_attached", role, role, "t-one",
                              {"workspace": WS, "surface": surface,
                               "launcher": "claude", "solo": solo}))

    def open_plan(self) -> None:
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "ONE-SURF"}):
            rid = self.request("markdown.open")
        MODULE.relay_request(rid, "t-one")

    def request_code_review(self, **kw) -> None:
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "ONE-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
            rid = self.request("review.request", role="code", **kw)
        MODULE.relay_request(rid, "t-one")

    def heads(self) -> list[str]:
        return [a[1] for a in MODULE.EXECUTED if a[0] == "cmux"]

    def attach_booted_solo(self) -> None:
        for role in ("planner", "worker"):
            MODULE.append_event(
                MODULE.stream_path("pjtest", "t-one", role),
                MODULE.make_event("role_attached", role, role, "t-one",
                                  {"workspace": WS, "surface": "ONE-SURF",
                                   "launcher": "claude", "solo": True,
                                   "side_pane": "BOOT-PANE", "side_surface": "BOOT-VIEWER"}))

    def test_markdown_open_reuses_the_viewer_booted_at_open(self) -> None:
        self.attach_booted_solo()
        self.open_plan()
        self.assertEqual([a for a in MODULE.EXECUTED if a[:2] == ["cmux", "markdown"]], [])
        done = [e for e in self.stream("planner") if e["type"] == "action_completed"][-1]
        self.assertTrue(done["payload"]["reused"])
        self.assertEqual(done["payload"]["side_pane"], "BOOT-PANE")
        self.assertEqual(MODULE.side_pane("pjtest", "t-one"), ("BOOT-PANE", "BOOT-VIEWER"))

    def test_markdown_open_reopens_when_the_booted_viewer_was_closed(self) -> None:
        self.attach_booted_solo()
        with mock.patch.object(MODULE, "surface_alive", lambda ws, s: False):
            self.open_plan()
        md = [a for a in MODULE.EXECUTED if a[:3] == ["cmux", "markdown", "open"]]
        self.assertEqual(len(md), 1)
        done = [e for e in self.stream("planner") if e["type"] == "action_completed"][-1]
        self.assertNotIn("reused", done["payload"])
        self.assertEqual(done["payload"]["side_pane"], "TEST-SIDE-PANE")   # the new viewer wins
        self.assertEqual(MODULE.side_pane("pjtest", "t-one"), ("TEST-SIDE-PANE", "TEST-SIDE-SURFACE"))

    def test_split_markdown_open_reopens_as_a_worker_tab_when_the_viewer_is_gone(self) -> None:
        for role, surf in (("planner", "P-SURF"), ("worker", "W-SURF")):
            MODULE.append_event(
                MODULE.stream_path("pjtest", "t-one", role),
                MODULE.make_event("role_attached", role, role, "t-one",
                                  {"workspace": WS, "surface": surf, "launcher": "claude",
                                   "solo": False, "side_pane": "W-PANE",
                                   "side_surface": "OLD-VIEWER"}))
        with mock.patch.object(MODULE, "surface_alive", lambda ws, s: False):
            self.open_plan()
        md = [a for a in MODULE.EXECUTED if a[:3] == ["cmux", "markdown", "open"]][0]
        self.assertEqual(md[md.index("--surface") + 1], "W-SURF")
        mv = [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "move-surface"]][0]
        self.assertEqual(mv[mv.index("--pane") + 1], "TEST-WORKER-PANE")
        done = [e for e in self.stream("planner") if e["type"] == "action_completed"][-1]
        self.assertEqual(done["payload"]["placement"], "worker-tab")
        self.assertEqual(done["payload"]["side_pane"], "TEST-WORKER-PANE")

    def test_unknown_topology_markdown_open_keeps_the_plain_split(self) -> None:
        self.open_plan()                                        # no role_attached at all
        md = [a for a in MODULE.EXECUTED if a[:3] == ["cmux", "markdown", "open"]][0]
        self.assertEqual(md[md.index("--surface") + 1], "ONE-SURF")   # the requester
        self.assertEqual([a for a in MODULE.EXECUTED if a[:2] == ["cmux", "move-surface"]], [])

    def test_solo_reviewer_opens_in_the_viewer_pane_booted_at_open(self) -> None:
        self.attach_booted_solo()
        self.request_code_review()
        self.assertNotIn("new-split", self.heads())
        surface_call = [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "new-surface"]][0]
        self.assertEqual(surface_call[surface_call.index("--pane") + 1], "BOOT-PANE")

    def test_markdown_open_records_the_viewer_pane(self) -> None:
        self.open_plan()
        done = [e for e in self.stream("planner") if e["type"] == "action_completed"][-1]
        self.assertEqual(done["payload"]["side_pane"], "TEST-SIDE-PANE")
        self.assertEqual(done["payload"]["side_surface"], "TEST-SIDE-SURFACE")
        self.assertEqual(MODULE.side_pane("pjtest", "t-one"),
                         ("TEST-SIDE-PANE", "TEST-SIDE-SURFACE"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MODULE.op_topology(argparse.Namespace(slug="t-one"))
        self.assertIn("side_pane=TEST-SIDE-PANE side_surface=TEST-SIDE-SURFACE", out.getvalue())

    def test_solo_reviewer_opens_as_a_tab_in_the_plan_viewer_pane(self) -> None:
        self.attach("planner", "ONE-SURF", True)
        self.attach("worker", "ONE-SURF", True)
        self.open_plan()
        self.request_code_review()
        self.assertNotIn("new-split", self.heads())             # a tab, never a stacked split
        surface_call = [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "new-surface"]][0]
        self.assertEqual(surface_call[surface_call.index("--pane") + 1], "TEST-SIDE-PANE")
        self.assertEqual(surface_call[surface_call.index("--focus") + 1], "false")
        self.assertNotIn("cd ", self.boot_line())               # new-surface carries the cwd
        att = [e for e in self.stream("reviewer") if e["type"] == "role_attached"][-1]
        self.assertEqual(att["payload"]["pane"], "TEST-SIDE-PANE")
        self.assertEqual(att["payload"]["placement"], "side")
        delivered = [e for e in self.stream("worker") if e["type"] == "delivered"][-1]
        self.assertEqual(delivered["payload"]["placement"], "side")

    def test_solo_without_a_recorded_viewer_falls_back_to_the_requester_pane(self) -> None:
        self.attach("planner", "ONE-SURF", True)
        self.attach("worker", "ONE-SURF", True)
        self.request_code_review()                              # no markdown.open recorded
        self.assertNotIn("new-split", self.heads())
        surface_call = [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "new-surface"]][0]
        self.assertEqual(surface_call[surface_call.index("--pane") + 1], "TEST-PANE")
        att = [e for e in self.stream("reviewer") if e["type"] == "role_attached"][-1]
        self.assertNotIn("placement", att["payload"])

    def test_split_task_ignores_the_side_pane(self) -> None:
        self.attach("planner", "P-SURF", False)
        self.attach("worker", "ONE-SURF", False)
        self.open_plan()
        self.request_code_review()
        self.assertNotIn("new-split", self.heads())
        self.assertIn("new-surface", self.heads())
        self.assertNotIn("cd ", self.boot_line())


class ReviewCompleteTests(RelayTestBase):
    """review.complete puts the `pj work done` pill on the task workspace — and only once the round's
    roles are all terminal, so the pill never runs ahead of the streams."""

    def attach_role(self, role: str, surface: str) -> None:
        MODULE.append_event(
            MODULE.stream_path("pjtest", "t-one", role),
            MODULE.make_event("role_attached", role, role, "t-one",
                              {"workspace": WS, "surface": surface, "launcher": "claude"}))

    def register(self, surface: str) -> None:
        # one registry file per surface (the base helper's single "session-x" would overwrite)
        d = self.registry / WS
        d.mkdir(exist_ok=True)
        (d / f"session-{surface}").write_text(
            "\n".join(["ts=1786400000", "state=idle", "pid=4242", "kind=claude", f"surface={surface}",
                       f"cwd={self.tmp.name}"]) + "\n", encoding="utf-8")

    def open_round_and_request(self, *roles: str) -> dict[str, str]:
        rids = {}
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "W-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
            for role in roles:
                rids[role] = self.request("review.request", role=role)
        return rids

    def reply_and_ack(self, role: str) -> None:
        rid = self.request("review.reply", role=role, stdin=json.dumps({"findings": []}))
        MODULE.relay_request(rid, "t-one")
        MODULE.op_ack(argparse.Namespace(slug="t-one", request_id=rid, status="processed"))

    def complete(self) -> str:
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "W-SURF"}):
            return self.request("review.complete")

    def test_refused_without_any_review_record(self) -> None:
        with self.assertRaises(MODULE.Fail) as cm:
            self.complete()
        self.assertIn("기록이 없습니다", str(cm.exception))

    def test_refused_while_a_reply_is_delivered_but_not_acked(self) -> None:
        self.attach_role("planner", "P-SURF")
        self.attach_role("worker", "W-SURF")
        self.register_agent("W-SURF")
        rids = self.open_round_and_request("code")
        MODULE.relay_request(rids["code"], "t-one")                    # reviewer spawned
        rid = self.request("review.reply", role="code", stdin=json.dumps({"findings": []}))
        MODULE.relay_request(rid, "t-one")                             # delivered, no ack
        with self.assertRaises(MODULE.Fail) as cm:
            self.complete()
        self.assertIn("code 리뷰가 아직 terminal", str(cm.exception))

    def test_sets_the_pill_once_code_is_acked_and_plan_is_skipped(self) -> None:
        self.attach_role("planner", "P-SURF")
        self.attach_role("worker", "W-SURF")
        self.register_agent("W-SURF")
        rids = self.open_round_and_request("code")
        MODULE.relay_request(rids["code"], "t-one")
        self.reply_and_ack("code")
        self.request("review.skip", role="plan", stdin=json.dumps({"reason": "solo"}))
        rid = self.complete()
        MODULE.relay_request(rid, "t-one")
        call = [a for a in MODULE.EXECUTED if a[:2] == ["cmux", "set-status"]][0]
        self.assertEqual(call[2:4], ["pj", "pj work done"])
        self.assertEqual(call[call.index("--workspace") + 1], WS)
        self.assertEqual(call[call.index("--priority") + 1], "100")   # above the cc.* tier
        self.assertEqual(call[call.index("--icon") + 1], "checkmark.seal.fill")
        self.assertEqual(call[call.index("--color") + 1], "#2E7D32")
        done = [e for e in self.stream("worker") if e["type"] == "action_completed"][-1]
        self.assertEqual(done["payload"]["workspace"], WS)
        # a second call is the usual idempotent no-op on the relay side
        self.assertIn("already action_completed", MODULE.relay_request(rid, "t-one"))

    def test_split_needs_both_roles_terminal(self) -> None:
        self.attach_role("planner", "P-SURF")
        self.attach_role("worker", "W-SURF")
        self.register("W-SURF")
        self.register("P-SURF")
        rids = self.open_round_and_request("code", "plan")
        for r in rids.values():
            MODULE.relay_request(r, "t-one")
        self.reply_and_ack("code")
        with self.assertRaises(MODULE.Fail) as cm:                     # plan still out
            self.complete()
        self.assertIn("plan 리뷰가 아직 terminal", str(cm.exception))
        self.reply_and_ack("plan")
        rid = self.complete()
        MODULE.relay_request(rid, "t-one")
        self.assertEqual(len([a for a in MODULE.EXECUTED if a[:2] == ["cmux", "set-status"]]), 1)


if __name__ == "__main__":
    unittest.main()


class MultipleReviewersRelayTests(RelayTestBase):
    """Several reviewers on one round, each its own session — the case the transport was single
    for until now, and the one no other test in this file reaches.

    Every earlier reviewer test exercises `code`. That left the whole per-reviewer split — its
    own stream, its own attachment, its own launcher, its own prompt — asserted only by schema
    checks, which cannot catch a lookup that reads the wrong file.
    """

    def attach_role(self, role: str, surface: str) -> None:
        MODULE.append_event(
            MODULE.stream_path("pjtest", "t-one", role),
            MODULE.make_event("role_attached", role, role, "t-one",
                              {"workspace": WS, "surface": surface, "launcher": "claude"}))

    def register(self, surface: str, kind: str | None = "claude") -> None:
        # one registry file per surface — the base helper's single "session-x" overwrites
        d = self.registry / WS
        d.mkdir(exist_ok=True)
        lines = ["ts=1786400000", "state=idle", "pid=4242", "kind=claude", f"surface={surface}",
                 f"cwd={self.tmp.name}"] + ([f"kind={kind}"] if kind else [])
        (d / f"session-{surface}").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def plan_handoff(self, reviewers) -> None:
        MODULE.append_event(
            MODULE.stream_path("pjtest", "t-one", "planner"),
            MODULE.make_event("worker_handoff", "planner", "worker", "t-one",
                              {"cautions": [], "references": [], "reviewers": reviewers},
                              round_=0))

    def open_group(self) -> dict:
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            self.request("review.group.open", stdin=json.dumps(
                {"summary": "s", "diff_base": "feat/base", "diff_head": "feat/t-one"}))
        return [e for e in self.stream("worker")
                if e["type"] == "review_group_opened"][-1]

    def dispatch(self, role: str) -> str:
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            rid = self.request("review.request", role=role)
        MODULE.relay_request(rid, "t-one")
        return rid

    def test_two_reviewers_get_two_sessions_in_two_streams(self) -> None:
        self.plan_handoff(["security"])
        self.assertEqual(self.open_group()["payload"]["roles"],
                         ["code", "plan", "security"])

        self.dispatch("code")
        MODULE.EXECUTED.clear()
        self.dispatch("security")

        # a SECOND surface was opened. With the old hardcoded `reviewer` stream lookup the
        # security request finds the code reviewer's attachment, wakes THAT session, and opens
        # nothing — so this assertion is the one that fails on the bug. (Surface ids cannot be
        # compared here: TEST mode hands every new surface the same stub id.)
        self.assertIn("new-surface", [a[1] for a in MODULE.EXECUTED if a[0] == "cmux"])

        code_att = [e for e in self.stream("reviewer") if e["type"] == "role_attached"]
        sec_att = [e for e in self.stream("reviewer-security") if e["type"] == "role_attached"]
        self.assertEqual(len(code_att), 1)
        self.assertEqual(len(sec_att), 1)
        self.assertEqual(code_att[0]["payload"]["role"], "code")
        self.assertEqual(sec_att[0]["payload"]["role"], "security")

    def test_solo_grok_cross_check_gates_completion_and_reuses_its_session(self) -> None:
        self.attach_role("worker", "WORKER-SURF")
        self.register_agent("WORKER-SURF")
        self.plan_handoff(["grok"])
        with mock.patch.object(MODULE, "task_is_solo", return_value=True):
            self.assertEqual(self.open_group()["payload"]["roles"], ["code", "grok"])
            self.dispatch("code")
            MODULE.EXECUTED.clear()
            self.dispatch("grok")
            command = [a[-1] for a in MODULE.EXECUTED if a[:2] == ["cmux", "set-buffer"]][0]
            self.assertIn("--profile grok --", command)
            self.assertIn("independent Grok cross-check", self.boot_prompt("grok"))
            self.assertIn("--role grok", self.boot_prompt("grok"))
            for stream, role in (("reviewer", "code"), ("reviewer-grok", "grok")):
                attached = [e for e in self.stream(stream) if e["type"] == "role_attached"]
                self.assertEqual(len(attached), 1)
                self.assertEqual(attached[0]["payload"]["role"], role)

            for role in ("code", "grok"):
                rid = self.request("review.reply", role=role, stdin=json.dumps({"findings": []}))
                MODULE.relay_request(rid, "t-one")
                MODULE.op_ack(ns("ack", request_id=rid, status="processed", slug="t-one"))
                ok, why = MODULE.review_round_terminal("pjtest", "t-one", 1)
                if role == "code":
                    self.assertFalse(ok)
                    self.assertIn("grok", why)
                else:
                    self.assertTrue(ok, why)

            self.open_group()
            MODULE.EXECUTED.clear()
            with mock.patch.object(MODULE, "native_grok_alive", return_value=True) as alive:
                self.dispatch("grok")
                alive.assert_called_once_with(WS, "TEST-NEW-SURFACE")
            self.assertNotIn("new-surface", [a[1] for a in MODULE.EXECUTED if a[0] == "cmux"])
            self.assertEqual(len([e for e in self.stream("reviewer-grok")
                                  if e["type"] == "role_attached"]), 1)
            with self.assertRaises(MODULE.Fail):
                self.request("review.request", role="grok", launcher="codex")

    def test_each_reviewer_boots_on_its_own_launcher_with_its_own_lens(self) -> None:
        self.plan_handoff(["database"])
        self.open_group()
        self.dispatch("code")
        MODULE.EXECUTED.clear()
        self.dispatch("database")

        self.assertIn("independent database reviewer", self.boot_line())
        prompt = self.boot_prompt("database")
        self.assertIn("--role database", prompt)          # its own reply command
        self.assertIn("database specialist", prompt)      # its own lens
        self.assertNotIn("senior code reviewer", prompt)  # not the code reviewer's
        self.assertIn("review.reply", prompt)             # the shared contract came too

        att = [e for e in self.stream("reviewer-database")
               if e["type"] == "role_attached"][0]["payload"]
        self.assertEqual(att["launcher"], MODULE.reviewer_launcher("database"))
        self.assertTrue(MODULE.valid_launcher(att["launcher"]))

    def test_a_later_round_wakes_each_reviewer_without_opening_more(self) -> None:
        self.plan_handoff(["security"])
        self.open_group()
        for role in ("code", "security"):
            self.dispatch(role)
        self.register_agent("TEST-NEW-SURFACE", kind="claude")

        self.open_group()                                  # round 2
        MODULE.EXECUTED.clear()
        with mock.patch.object(MODULE, "pid_command", lambda pid: "claude --resume"):
            for role in ("code", "security"):
                self.dispatch(role)
        self.assertNotIn("new-surface", [a[1] for a in MODULE.EXECUTED if a[0] == "cmux"])
        for stream in ("reviewer", "reviewer-security"):
            self.assertEqual(
                len([e for e in self.stream(stream) if e["type"] == "role_attached"]), 1,
                stream)

    def test_review_complete_refuses_until_every_reviewer_replied(self) -> None:
        """The claim that matters: forgetting one reviewer is a refusal, not a short review."""
        self.attach_role("worker", "WORKER-SURF")
        self.register_agent("WORKER-SURF")
        self.plan_handoff(["security"])
        self.open_group()
        for role in ("code", "security"):
            self.dispatch(role)
        self.request("review.skip", role="plan",
                     stdin=json.dumps({"reason": "solo 아님, 테스트"}))

        def reply_and_ack(role: str) -> None:
            with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                              "CMUX_SURFACE_ID": "LEAD-SURF"}):
                rid = self.request("review.reply", role=role,
                                   stdin=json.dumps({"findings": []}))
            MODULE.relay_request(rid, "t-one")
            MODULE.op_ack(ns("ack", request_id=rid, status="processed", slug="t-one"))

        ok, why = MODULE.review_round_terminal("pjtest", "t-one", 1)
        self.assertFalse(ok, why)

        reply_and_ack("code")
        ok, why = MODULE.review_round_terminal("pjtest", "t-one", 1)
        self.assertFalse(ok, "security 가 아직인데 terminal 로 판정됨")
        self.assertIn("security", why)

        reply_and_ack("security")
        ok, why = MODULE.review_round_terminal("pjtest", "t-one", 1)
        self.assertTrue(ok, why)

    def test_a_selected_reviewer_that_was_never_requested_blocks_completion(self) -> None:
        """A reviewer the planner picked and the worker forgot must NOT read as a clean round.

        `review_round_terminal` walks the REQUESTS, so a reviewer with no request of its own is
        not something that walk can notice — the round would look complete with a lane nobody
        reviewed, and the pill would say 완료.
        """
        self.attach_role("worker", "WORKER-SURF")
        self.register_agent("WORKER-SURF")
        self.plan_handoff(["security"])
        self.assertEqual(self.open_group()["payload"]["roles"],
                         ["code", "plan", "security"])

        self.dispatch("code")                      # security deliberately NOT dispatched
        self.request("review.skip", role="plan",
                     stdin=json.dumps({"reason": "테스트"}))
        with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                          "CMUX_SURFACE_ID": "LEAD-SURF"}):
            rid = self.request("review.reply", role="code",
                               stdin=json.dumps({"findings": []}))
        MODULE.relay_request(rid, "t-one")
        MODULE.op_ack(ns("ack", request_id=rid, status="processed", slug="t-one"))

        ok, why = MODULE.review_round_terminal("pjtest", "t-one", 1)
        self.assertFalse(ok, "요청조차 안 한 security 를 두고 라운드가 완료로 판정됨")
        self.assertIn("security", why)

    def test_a_later_round_is_held_only_to_what_it_requested(self) -> None:
        """Round 1 must run every selected reviewer once. A later round is a targeted recheck:
        the worker re-requests the reviewers whose findings its fix changed, and nobody else is
        owed a turn. Holding round 2 to the full set made a planner give a second plan review it
        had no reason to give (seen live)."""
        self.attach_role("worker", "WORKER-SURF")
        self.register("WORKER-SURF")
        self.plan_handoff(["security"])
        self.open_group()                                       # round 1: code, plan, security

        def reply_and_ack(role: str) -> None:
            with mock.patch.dict(os.environ, {"CMUX_WORKSPACE_ID": WS,
                                              "CMUX_SURFACE_ID": "LEAD-SURF"}):
                rid = self.request("review.reply", role=role,
                                   stdin=json.dumps({"findings": []}))
            MODULE.relay_request(rid, "t-one")
            MODULE.op_ack(ns("ack", request_id=rid, status="processed", slug="t-one"))

        for role in ("code", "security"):
            self.dispatch(role)
        self.request("review.skip", role="plan", stdin=json.dumps({"reason": "테스트"}))
        self.register("TEST-NEW-SURFACE", kind="claude")
        reply_and_ack("code"); reply_and_ack("security")
        ok, why = MODULE.review_round_terminal("pjtest", "t-one", 1)
        self.assertTrue(ok, why)

        # round 2: only security is re-requested — code and plan are NOT owed a turn
        self.open_group()
        ok, why = MODULE.review_round_terminal("pjtest", "t-one", 2)
        self.assertFalse(ok)                                    # nothing requested yet
        self.assertIn("없습니다", why)
        with mock.patch.object(MODULE, "pid_command", lambda pid: "claude --resume"):
            self.dispatch("security")
        ok, why = MODULE.review_round_terminal("pjtest", "t-one", 2)
        self.assertFalse(ok)                                    # requested, not yet replied
        self.assertIn("security", why)
        reply_and_ack("security")
        ok, why = MODULE.review_round_terminal("pjtest", "t-one", 2)
        self.assertTrue(ok, why)                                # code/plan absence is fine here
