#!/usr/bin/env python3
"""Queue and watch behaviour for night-queue.py.

Runs against a TemporaryDirectory patched in as TASKS_DIR and a stubbed run_json — never the
live vault, never a real pj-tasks.py / pj-cmux.py call.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import os
import pathlib
import tempfile
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "night-queue.py"
SPEC = importlib.util.spec_from_file_location("night_queue", SCRIPT)
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

PROJECT = "ds-migration"


def ns(op: str, **kw) -> argparse.Namespace:
    base = {"project": PROJECT, "slug": None, "max": None, "attempts": 3,
            "parallel": M.PARALLEL_DEFAULT, "outcome": None, "reason": None, "since": None,
            "quiet": M.QUIET_DEFAULT, "timeout": M.TIMEOUT_DEFAULT}
    base.update(kw)
    return argparse.Namespace(op=op, **base)


def task(slug: str, status: str, deps: list[str] | None = None) -> dict:
    return {"slug": slug, "status": status, "title": f"{slug} 작업", "deps": deps or [],
            "project": PROJECT}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        (root / PROJECT).mkdir()
        p = mock.patch.object(M, "TASKS_DIR", root)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.tasks: dict[str, dict] = {}
        self.events: list[dict] = []               # default stream for any slug
        self.per_slug: dict[str, list[dict]] = {}  # overrides, so one slug can report alone
        self.night_failure = None

    def stub(self):
        """run_json dispatcher standing in for pj-tasks.py and pj-cmux.py."""
        def fake(script, *args):
            if args[0] == "night-check":
                if self.night_failure:
                    raise M.Fail(3, self.night_failure)
                return {"project": PROJECT, "roles": ["code", "grok"]}
            if args[0] == "list":
                return list(self.tasks.values())
            if args[0] == "get":
                slug = args[args.index("--slug") + 1]
                if slug not in self.tasks:
                    raise M.Fail(1, f"없음: {slug}")
                return self.tasks[slug]
            if args[0] == "event":
                slug = args[args.index("--slug") + 1]
                return self.per_slug.get(slug, self.events)
            raise AssertionError(f"unexpected call: {args}")
        return mock.patch.object(M, "run_json", side_effect=fake)

    def run_op(self, a) -> tuple[int, str]:
        buf = io.StringIO()
        with self.stub(), contextlib.redirect_stdout(buf):
            rc = M.OPS[a.op](a)
        return rc, buf.getvalue()


class Plan(Base):
    def test_queue_defaults_to_todo_in_list_order(self) -> None:
        self.tasks = {"a": task("a", M.TODO), "b": task("b", "진행 중"),
                      "c": task("c", M.TODO)}
        rc, out = self.run_op(ns("plan"))
        self.assertEqual(rc, 0)
        self.assertIn("count=2", out)
        self.assertEqual(M.load_state(PROJECT)["queue"], ["a", "c"])

    def test_named_slugs_keep_their_order_and_must_be_todo(self) -> None:
        self.tasks = {"a": task("a", M.TODO), "c": task("c", M.TODO)}
        self.run_op(ns("plan", slug=["c", "a"]))
        self.assertEqual(M.load_state(PROJECT)["queue"], ["c", "a"])

        self.run_op(ns("stop"))
        self.tasks["c"] = task("c", "검토 대기")
        with self.assertRaises(M.Fail):
            self.run_op(ns("plan", slug=["c"]))

    def test_running_plan_refuses_and_preserves_state(self) -> None:
        self.tasks = {s: task(s, M.TODO) for s in ("a", "b")}
        self.run_op(ns("plan"))
        self.run_op(ns("next"))
        self.run_op(ns("next"))
        self.run_op(ns("record", slug="a", outcome="failed", reason="착수 실패"))
        before = M.state_path(PROJECT).read_bytes()
        with self.assertRaisesRegex(M.Fail, "이미 running"):
            self.run_op(ns("plan"))
        self.assertEqual(M.state_path(PROJECT).read_bytes(), before)

    def test_stop_allows_a_new_plan(self) -> None:
        self.tasks = {"a": task("a", M.TODO)}
        self.run_op(ns("plan"))
        self.run_op(ns("next"))
        self.assertIn("still-running=a", self.run_op(ns("stop"))[1])
        self.assertIn("PJ_NIGHT=stopped", self.run_op(ns("next"))[1])
        self.run_op(ns("plan"))
        self.assertIn("PJ_NIGHT=next slug=a", self.run_op(ns("next"))[1])

    def test_max_truncates(self) -> None:
        self.tasks = {s: task(s, M.TODO) for s in ("a", "b", "c")}
        self.run_op(ns("plan", max=2))
        self.assertEqual(M.load_state(PROJECT)["queue"], ["a", "b"])

    def test_dep_inside_the_queue_only_orders_the_run_and_is_not_warned(self) -> None:
        self.tasks = {"a": task("a", M.TODO, deps=["b"]), "b": task("b", M.TODO)}
        _, out = self.run_op(ns("plan"))
        self.assertNotIn("WARN", out)

    def test_dep_outside_the_queue_is_warned_before_the_user_sleeps(self) -> None:
        self.tasks = {"a": task("a", M.TODO, deps=["x"]), "x": task("x", "진행 중")}
        _, out = self.run_op(ns("plan", slug=["a"]))
        self.assertIn("WARN", out)
        self.assertIn("건너뛰게", out)

    def test_empty_todo_refuses(self) -> None:
        self.tasks = {"a": task("a", "완료")}
        with self.assertRaises(M.Fail):
            self.run_op(ns("plan"))


class Next(Base):
    def plan(self, *slugs: str) -> None:
        self.run_op(ns("plan", slug=list(slugs)))

    def test_hands_out_the_first_unrun_task_and_marks_it_inflight(self) -> None:
        self.tasks = {"a": task("a", M.TODO), "b": task("b", M.TODO)}
        self.plan("a", "b")
        _, out = self.run_op(ns("next"))
        self.assertIn("PJ_NIGHT=next slug=a", out)
        self.assertIn("inflight=1/3", out)
        self.assertEqual(list(M.load_state(PROJECT)["inflight"]), ["a"])

    def test_status_changed_since_plan_is_skipped_and_next_slot_is_filled(self) -> None:
        for status in ("진행 중", "검토 대기", M.DONE):
            with self.subTest(status=status):
                self.tasks = {s: task(s, M.TODO) for s in ("a", "b")}
                if M.state_path(PROJECT).exists():
                    self.run_op(ns("stop"))
                self.plan("a", "b")
                self.tasks["a"]["status"] = status
                _, out = self.run_op(ns("next"))
                self.assertIn("PJ_NIGHT=skipped slug=a", out)
                self.assertIn("PJ_NIGHT=next slug=b", out)
                st = M.load_state(PROJECT)
                self.assertEqual(list(st["inflight"]), ["b"])
                self.assertEqual(st["results"]["a"]["outcome"], "skipped")
                self.assertIn(status, st["results"]["a"]["reason"])

    def test_fills_every_slot_before_reporting_busy(self) -> None:
        self.tasks = {s: task(s, M.TODO) for s in ("a", "b", "c", "d")}
        self.run_op(ns("plan", slug=["a", "b", "c", "d"], parallel=2))
        self.assertIn("PJ_NIGHT=next slug=a", self.run_op(ns("next"))[1])
        self.assertIn("PJ_NIGHT=next slug=b", self.run_op(ns("next"))[1])
        _, out = self.run_op(ns("next"))
        self.assertIn("PJ_NIGHT=busy", out)
        self.assertIn("limit=2", out)
        self.assertEqual(list(M.load_state(PROJECT)["inflight"]), ["a", "b"])

    def test_a_freed_slot_is_refilled(self) -> None:
        self.tasks = {s: task(s, M.TODO) for s in ("a", "b", "c")}
        self.run_op(ns("plan", slug=["a", "b", "c"], parallel=2))
        self.run_op(ns("next")); self.run_op(ns("next"))
        self.run_op(ns("record", slug="a", outcome="merged"))
        _, out = self.run_op(ns("next"))
        self.assertIn("PJ_NIGHT=next slug=c", out)
        self.assertEqual(sorted(M.load_state(PROJECT)["inflight"]), ["b", "c"])

    def test_unmet_dep_is_skipped_and_recorded_not_started(self) -> None:
        self.tasks = {"a": task("a", M.TODO), "b": task("b", M.TODO, deps=["x"]),
                      "x": task("x", "진행 중")}
        self.plan("a", "b")
        self.run_op(ns("next"))
        self.run_op(ns("record", slug="a", outcome="merged"))
        _, out = self.run_op(ns("next"))
        self.assertIn("PJ_NIGHT=skipped slug=b", out)
        self.assertIn("PJ_NIGHT=done", out)
        self.assertEqual(M.load_state(PROJECT)["results"]["b"]["outcome"], "skipped")

    def test_a_dep_still_in_flight_holds_the_dependent_instead_of_skipping_it(self) -> None:
        self.tasks = {"a": task("a", M.TODO), "b": task("b", M.TODO, deps=["a"])}
        self.plan("a", "b")
        self.run_op(ns("next"))                      # a goes out
        _, out = self.run_op(ns("next"))             # b waits on a, slot stays free
        self.assertIn("PJ_NIGHT=waiting", out)
        self.assertIn("held=b", out)
        self.assertNotIn("b", M.load_state(PROJECT)["results"])

    def test_dep_completed_earlier_in_the_night_lets_the_dependent_run(self) -> None:
        self.tasks = {"a": task("a", M.TODO), "b": task("b", M.TODO, deps=["a"])}
        self.plan("a", "b")
        self.run_op(ns("next"))
        self.tasks["a"] = task("a", M.DONE)          # pj-wrap moved it
        self.run_op(ns("record", slug="a", outcome="merged"))
        _, out = self.run_op(ns("next"))
        self.assertIn("PJ_NIGHT=next slug=b", out)

    def test_a_dep_that_failed_releases_the_dependent_as_skipped(self) -> None:
        self.tasks = {"a": task("a", M.TODO), "b": task("b", M.TODO, deps=["a"])}
        self.plan("a", "b")
        self.run_op(ns("next"))
        self.run_op(ns("record", slug="a", outcome="failed", reason="무응답"))
        _, out = self.run_op(ns("next"))
        self.assertIn("PJ_NIGHT=skipped slug=b", out)
        self.assertEqual(M.load_state(PROJECT)["results"]["b"]["outcome"], "skipped")

    def test_exhausted_queue_finishes_the_run(self) -> None:
        self.tasks = {"a": task("a", M.TODO)}
        self.plan("a")
        self.run_op(ns("next"))
        self.run_op(ns("record", slug="a", outcome="merged"))
        _, out = self.run_op(ns("next"))
        self.assertIn("PJ_NIGHT=done", out)
        self.assertEqual(M.load_state(PROJECT)["state"], "finished")


class Record(Base):
    def test_clears_current_so_the_loop_can_advance(self) -> None:
        self.tasks = {"a": task("a", M.TODO)}
        self.run_op(ns("plan", slug=["a"]))
        self.run_op(ns("next"))
        self.run_op(ns("record", slug="a", outcome="failed", reason="무응답"))
        st = M.load_state(PROJECT)
        self.assertEqual(st["inflight"], {})
        self.assertEqual(st["results"]["a"]["reason"], "무응답")

    def test_slug_outside_the_queue_is_refused(self) -> None:
        self.tasks = {"a": task("a", M.TODO)}
        self.run_op(ns("plan", slug=["a"]))
        with self.assertRaises(M.Fail):
            self.run_op(ns("record", slug="zz", outcome="merged"))


class Watch(Base):
    def setUp(self) -> None:
        super().setUp()
        self.tasks = {"a": task("a", M.TODO)}
        self.run_op(ns("plan", slug=["a"]))
        self.run_op(ns("next"))

    def test_done_report_ends_the_watch(self) -> None:
        self.events = [{"event_id": "evt_1", "type": "done_report",
                        "created_at": "2099-01-01T00:00:00+09:00",
                        "payload": {"commit": "ok", "typecheck": "ok"}}]
        _, out = self.run_op(ns("watch", slug="a", quiet=60, timeout=60))
        self.assertIn("PJ_NIGHT=report slug=a", out)
        self.assertIn("commit=ok", out)

    def test_stop_before_watch_refuses_even_an_explicit_slug(self) -> None:
        self.run_op(ns("stop"))
        self.assertIn("PJ_NIGHT=stopped", self.run_op(ns("watch", slug="a"))[1])

    def test_stop_during_event_read_drops_the_late_report(self) -> None:
        def late_report(_):
            self.run_op(ns("stop"))
            return [{"event_id": "evt_late", "type": "done_report",
                     "created_at": "2099-01-01T00:00:00Z", "payload": {}}]
        with mock.patch.object(M, "events_for", side_effect=late_report):
            _, out = self.run_op(ns("watch"))
        self.assertIn("PJ_NIGHT=stopped", out)
        self.assertNotIn("PJ_NIGHT=report", out)

    def test_recorded_target_is_removed_while_other_target_keeps_running(self) -> None:
        self.tasks["b"] = task("b", M.TODO)
        st = M.load_state(PROJECT)
        st["queue"].append("b")
        M.save_state(PROJECT, st)
        self.run_op(ns("next"))
        def reports(slug):
            if slug == "a":
                self.run_op(ns("record", slug="a", outcome="merged"))
            return [{"event_id": f"evt_{slug}", "type": "done_report",
                     "created_at": "2099-01-01T00:00:00Z", "payload": {}}]
        with mock.patch.object(M, "events_for", side_effect=reports):
            _, out = self.run_op(ns("watch"))
        self.assertIn("PJ_NIGHT=report slug=b", out)
        self.assertNotIn("PJ_NIGHT=report slug=a", out)

    def test_old_watch_does_not_follow_a_replacement_run_in_the_same_second(self) -> None:
        stamp = M.load_state(PROJECT)["started_at"]
        def restart(_):
            self.run_op(ns("stop"))
            self.run_op(ns("plan", slug=["a"]))
            self.run_op(ns("next"))
        with mock.patch.object(M, "now_iso", return_value=stamp), \
             mock.patch.object(M.time, "sleep", side_effect=restart):
            _, out = self.run_op(ns("watch", quiet=60, timeout=60))
        self.assertIn("PJ_NIGHT=stopped", out)
        self.assertEqual(M.load_state(PROJECT)["state"], "running")

    def test_done_report_with_failed_review_is_blocked(self) -> None:
        self.events = [{"event_id": "evt_done", "type": "done_report",
                        "created_at": "2099-01-01T00:00:00Z", "payload": {}}]
        self.night_failure = "grok 회신 없음"
        _, out = self.run_op(ns("watch"))
        self.assertIn("PJ_NIGHT=blocked slug=a", out)
        self.assertNotIn("PJ_NIGHT=report", out)

    def test_premerge_check_requires_active_task_and_review_success(self) -> None:
        self.assertIn("PJ_NIGHT=ready", self.run_op(ns("check", slug="a"))[1])
        self.night_failure = "grok 회신 없음"
        with self.assertRaisesRegex(M.Fail, "grok"):
            self.run_op(ns("check", slug="a"))
        self.run_op(ns("stop"))
        with self.assertRaisesRegex(M.Fail, "현재 실행"):
            self.run_op(ns("check", slug="a"))

    def test_utc_stamped_event_matches_a_local_offset_since(self) -> None:
        # pj-cmux.py stamps UTC with a Z; the inflight stamp is local with an offset. As strings
        # the Z stamp sorts BEFORE its own local rendering, so a string compare drops every
        # real event and the watch reports quiet on a task that finished.
        since = "2026-09-16T10:00:00+09:00"
        st = M.load_state(PROJECT)
        st["inflight"]["a"] = since
        M.save_state(PROJECT, st)
        utc = (M.parse_ts(since) + dt.timedelta(seconds=30)).astimezone(dt.timezone.utc)
        stamp = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertLess(stamp, since, "이 테스트는 문자열 비교가 역전되는 경우여야 의미가 있다")
        self.events = [{"event_id": "evt_1", "type": "done_report", "created_at": stamp,
                        "payload": {"commit": "ok", "typecheck": "none"}}]
        _, out = self.run_op(ns("watch", slug="a", quiet=60, timeout=60))
        self.assertIn("PJ_NIGHT=report slug=a", out)

    def test_since_override_filters_old_reports_including_other_timezones(self) -> None:
        self.events = [{"event_id": "evt_old", "type": "done_report",
                        "created_at": "2099-01-01T00:00:00Z", "payload": {}}]
        _, out = self.run_op(ns("watch", since="2099-01-01T09:00:01+09:00",
                               quiet=0, timeout=60))
        self.assertIn("PJ_NIGHT=quiet", out)
        # The override can also precede the task's start, and its boundary is inclusive.
        self.events = [{"event_id": "evt_equal", "type": "done_report",
                        "created_at": "1999-01-01T00:00:00Z", "payload": {}}]
        _, out = self.run_op(ns("watch", slug="a", since="1999-01-01T09:00:00+09:00",
                               quiet=0, timeout=60))
        self.assertIn("PJ_NIGHT=report slug=a", out)

    def test_invalid_since_is_rejected(self) -> None:
        for since in ("", "not-a-date", "2026-99-99T00:00:00Z"):
            with self.subTest(since=since), self.assertRaisesRegex(M.Fail, "--since"):
                self.run_op(ns("watch", since=since, quiet=0, timeout=0))

    def test_random_ids_reset_quiet_timer_but_repeated_events_do_not(self) -> None:
        clock = [0]
        self.events = [{"event_id": "evt_ffffffffffffffff", "type": "request",
                        "created_at": "2099-01-01T00:00:00Z"}]

        def tick(_):
            clock[0] += 20
            if clock[0] <= 60:
                self.events.append({"event_id": f"evt_{clock[0]:016x}", "type": "request",
                                    "created_at": "2099-01-01T00:00:01Z"})

        with mock.patch.object(M.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(M.time, "sleep", side_effect=tick):
            _, out = self.run_op(ns("watch", quiet=30, timeout=200))
        self.assertIn("PJ_NIGHT=quiet slug=a for=40s", out)
        self.assertEqual(clock[0], 100, "Silence starts after the LAST new ID, at t=60")

    def test_a_report_from_before_this_task_started_is_not_this_one(self) -> None:
        self.events = [{"event_id": "evt_0", "type": "done_report",
                        "created_at": "1999-01-01T00:00:00+09:00", "payload": {}}]
        with mock.patch.object(M.time, "sleep"):
            _, out = self.run_op(ns("watch", slug="a", quiet=0, timeout=60))
        self.assertIn("PJ_NIGHT=quiet", out)

    def test_watching_every_inflight_task_returns_the_one_that_reported(self) -> None:
        self.tasks["b"] = task("b", M.TODO)
        st = M.load_state(PROJECT)
        st["queue"].append("b"); M.save_state(PROJECT, st)
        self.run_op(ns("next"))                      # b joins a in flight
        self.assertEqual(sorted(M.load_state(PROJECT)["inflight"]), ["a", "b"])
        self.per_slug["b"] = [{"event_id": "evt_b", "type": "done_report",
                               "created_at": "2099-01-01T00:00:00+09:00",
                               "payload": {"commit": "ok", "typecheck": "none"}}]
        _, out = self.run_op(ns("watch", quiet=60, timeout=60))
        self.assertIn("PJ_NIGHT=report slug=b", out)

    def test_silence_ends_the_watch_so_the_queue_moves_on(self) -> None:
        self.events = []
        with mock.patch.object(M.time, "sleep"):
            _, out = self.run_op(ns("watch", slug="a", quiet=0, timeout=60))
        self.assertIn("PJ_NIGHT=quiet slug=a", out)


class Report(Base):
    def test_groups_every_queued_task_including_the_ones_never_run(self) -> None:
        self.tasks = {s: task(s, M.TODO) for s in ("a", "b", "c")}
        self.run_op(ns("plan", slug=["a", "b", "c"]))
        self.run_op(ns("next"))
        self.run_op(ns("record", slug="a", outcome="merged"))
        self.run_op(ns("record", slug="b", outcome="failed", reason="머지 실패: 충돌"))
        _, out = self.run_op(ns("report"))
        self.assertIn("머지됨 (1)", out)
        self.assertIn("실패 (1)", out)
        self.assertIn("머지 실패: 충돌", out)
        self.assertIn("미실행 (1)", out)


class StateSafety(Base):
    def test_project_escape_and_trailing_newline_are_refused(self):
        for name in ("../escape", "project/child", PROJECT + "\n"):
            with self.subTest(name=name), self.assertRaises(M.Fail):
                M.save_state(name, {"state": "running"})

    def test_symlinked_project_cannot_create_state_or_lock_outside_root(self):
        root = pathlib.Path(self.tmp.name)
        outside = root / "outside"
        outside.mkdir()
        project = root / PROJECT
        project.rmdir()
        project.symlink_to(outside, target_is_directory=True)
        self.tasks = {"a": task("a", M.TODO)}
        with self.assertRaises(M.Fail):
            self.run_op(ns("plan"))
        self.assertEqual(list(outside.iterdir()), [])

    def test_state_and_lock_links_are_refused_without_touching_target(self):
        root = pathlib.Path(self.tmp.name)
        victim = root / "unrelated.json"
        victim.write_text('{"state":"untouched"}')
        self.tasks = {"a": task("a", M.TODO)}
        for name in ("night.json", "night.lock"):
            for kind in ("symlink", "hardlink"):
                path = root / PROJECT / name
                if path.exists():
                    path.unlink()
                if kind == "symlink":
                    path.symlink_to(victim)
                else:
                    os.link(victim, path)
                with self.subTest(name=name, kind=kind), self.assertRaises(M.Fail):
                    self.run_op(ns("plan"))
                self.assertEqual(victim.read_text(), '{"state":"untouched"}')
                path.unlink()

    def test_old_predictable_temp_symlink_is_never_written(self):
        root = pathlib.Path(self.tmp.name)
        victim = root / "unrelated.txt"
        victim.write_text("untouched")
        (root / PROJECT / "night.json.tmp").symlink_to(victim)
        self.tasks = {"a": task("a", M.TODO)}
        self.run_op(ns("plan"))
        self.assertEqual(victim.read_text(), "untouched")
        self.assertEqual(M.load_state(PROJECT)["queue"], ["a"])
        self.assertEqual((root / PROJECT / "night.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list((root / PROJECT).glob(".night-*.tmp")), [])

    def test_special_lock_file_is_refused_without_blocking(self):
        os.mkfifo(pathlib.Path(self.tmp.name) / PROJECT / "night.lock")
        with self.assertRaises(M.Fail):
            self.run_op(ns("stop"))


if __name__ == "__main__":
    unittest.main()
