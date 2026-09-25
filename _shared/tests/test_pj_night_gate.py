"""Night requires real processed reviews; daytime terminal-failure semantics stay intact."""
import importlib.util
import json
import pathlib
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "night_action_helpers", pathlib.Path(__file__).with_name("test_pj_cmux_actions.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
M = helpers.MODULE


class NightGateTests(helpers.ActionTestBase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(M, "task_is_solo", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.night = self.tasks_dir / "pjtest/night.json"
        self.night.parent.mkdir(parents=True)
        self.night.write_text(json.dumps({"state": "running", "queue": ["t-one"],
                                         "run_id": "test-run", "inflight": {"t-one": M.now_iso()}}))
        self.request("worker.handoff", stdin=json.dumps(
            {"cautions": [], "references": [], "reviewers": ["grok"]}))
        self.group()

    def group(self):
        self.request("review.group.open", stdin=json.dumps(
            {"summary": "test", "diff_base": "feat/base", "diff_head": "feat/t-one"}))

    def check(self):
        return M.check_night_review("pjtest", "t-one")

    def reply(self, role, findings=None, ack=True):
        self.request("review.reply", role=role, stdin=json.dumps({"findings": findings or []}))
        ev = [ev for ev in self.stream(M.reviewer_def(role)["stream"])
              if ev["type"] == "code_review"][-1]
        if ack:
            M.append_event(M.stream_path("pjtest", "t-one", "worker"), M.make_event(
                "ack", "worker", "reviewer", "t-one", {"status": "processed"},
                round_=ev["round"], reply_to=ev["event_id"]))
        return ev

    def failed_requests(self):
        for role in ("code", "grok"):
            self.request("review.request", role=role)
        for req in [e for e in self.stream("worker") if e["type"] == "review_requested"]:
            M.append_event(M.stream_path("pjtest", "t-one", "worker"), M.make_event(
                "delivery_failed", "worker", "reviewer", "t-one", {"reason": "launch failed"},
                round_=req["round"], reply_to=req["event_id"]))

    def test_failed_reviewers_are_terminal_but_cannot_complete_or_report_at_night(self):
        self.failed_requests()
        self.assertTrue(M.review_round_terminal("pjtest", "t-one", 1)[0])
        for action in ("review.complete", "done.report"):
            with self.subTest(action=action), self.assertRaisesRegex(M.Fail, "night 검사 거부"):
                self.request(action, commit="ok")
        self.assertNotIn("done_report", [e["type"] for e in self.stream("worker")])

    def test_daytime_failed_terminal_behavior_is_unchanged(self):
        self.failed_requests()
        self.night.unlink()
        self.request("review.complete")
        self.request("done.report", commit="ok")

    def test_skipped_and_unrequested_reviewers_cannot_pass(self):
        self.request("review.request", role="code")
        self.reply("code")
        self.request("review.skip", role="grok", stdin=json.dumps({"reason": "unavailable"}))
        with self.assertRaisesRegex(M.Fail, "grok"):
            self.check()

    def test_unacked_reply_cannot_pass(self):
        for role in ("code", "grok"):
            self.request("review.request", role=role)
            self.reply(role, ack=(role == "code"))
        with self.assertRaisesRegex(M.Fail, "grok"):
            self.check()

    def test_processed_replies_pass_and_targeted_recheck_keeps_other_reviewers(self):
        for role in ("code", "grok"):
            self.request("review.request", role=role)
            self.reply(role)
        self.assertEqual(self.check()["roles"], ["code", "grok"])
        self.request("done.report", commit="ok")
        self.group()
        with self.assertRaisesRegex(M.Fail, "마지막 리뷰 라운드"):
            self.check()
        self.request("review.request", role="grok")
        with self.assertRaisesRegex(M.Fail, "grok"):
            self.check()
        self.reply("grok")
        self.check()

    def test_older_reply_cannot_satisfy_new_request_in_same_round(self):
        for role in ("code", "grok"):
            self.request("review.request", role=role)
            self.reply(role)
        self.request("review.request", role="grok")
        with self.assertRaisesRegex(M.Fail, "grok"):
            self.check()

    def test_stopped_run_or_old_reviews_are_refused(self):
        for role in ("code", "grok"):
            self.request("review.request", role=role)
            self.reply(role)
        state = json.loads(self.night.read_text())
        state["inflight"]["t-one"] = "2099-01-01T00:00:00Z"
        self.night.write_text(json.dumps(state))
        with self.assertRaisesRegex(M.Fail, "이번 실행"):
            self.check()
        state["state"] = "stopped"
        self.night.write_text(json.dumps(state))
        with self.assertRaisesRegex(M.Fail, "현재 실행"):
            self.check()

    def test_same_second_restarted_run_cannot_reuse_previous_reviews(self):
        for role in ("code", "grok"):
            self.request("review.request", role=role)
            self.reply(role)
        state = json.loads(self.night.read_text())
        state["run_id"] = "replacement-run"
        self.night.write_text(json.dumps(state))
        with self.assertRaisesRegex(M.Fail, "이번 실행"):
            self.check()

    def test_blocking_requires_recheck_or_an_evidenced_rejection(self):
        self.request("review.request", role="code")
        self.reply("code")
        self.request("review.request", role="grok")
        ev = self.reply("grok", [{"severity": "blocking", "location": "a.py:1",
                                 "finding": "bug", "evidence": "input x fails",
                                 "recommended_change": "fix x"}])
        with self.assertRaisesRegex(M.Fail, "blocking"):
            self.check()
        self.request("review.disposition", reply_to=ev["event_id"], stdin=json.dumps(
            {"dispositions": [{"finding_index": 0, "disposition": "accepted", "note": "fixed"}]}))
        with self.assertRaisesRegex(M.Fail, "blocking"):
            self.check()
        self.group()
        self.request("review.request", role="grok", findings=ev["event_id"])
        self.reply("grok")
        self.check()
