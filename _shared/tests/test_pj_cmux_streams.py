#!/usr/bin/env python3
"""Exchange stream mechanics: concurrent appends, crash tail, dedupe, symlinks, encoding."""
from __future__ import annotations

import concurrent.futures
import importlib.util
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


class StreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tasks_dir = pathlib.Path(self.tmp.name) / "raw" / "tasks"
        (self.tasks_dir / "pjtest" / "exchanges" / "t-one").mkdir(parents=True)
        self.patch = mock.patch.object(MODULE, "TASKS_DIR", self.tasks_dir)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.stream = self.tasks_dir / "pjtest" / "exchanges" / "t-one" / "worker.jsonl"

    def ev(self, **kw) -> dict:
        base = dict(type_="done_report", from_="worker", to="board", slug="t-one",
                    payload={"commit": "ok"})
        base.update(kw)
        return MODULE.make_event(**base)

    def test_concurrent_appends_lose_nothing(self) -> None:
        def worker(n: int) -> None:
            for _ in range(200):
                MODULE.append_event(self.stream, self.ev())
        with concurrent.futures.ThreadPoolExecutor(4) as ex:
            list(ex.map(worker, range(4)))
        lines = self.stream.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 800)
        ids = {json.loads(ln)["event_id"] for ln in lines}
        self.assertEqual(len(ids), 800)

    def test_partial_final_line_is_recovered_not_kept(self) -> None:
        first = self.ev()
        MODULE.append_event(self.stream, first)
        with open(self.stream, "ab") as fh:
            fh.write(b'{"event_id":"evt_partial","type":"done_re')  # crash mid-line
        self.assertEqual(len(MODULE.read_stream(self.stream)), 1)  # reader drops the tail
        second = self.ev()
        MODULE.append_event(self.stream, second)
        events = MODULE.read_stream(self.stream)
        self.assertEqual([e["event_id"] for e in events],
                         [first["event_id"], second["event_id"]])
        raw = self.stream.read_text(encoding="utf-8")
        self.assertNotIn("evt_partial", raw)
        self.assertTrue(raw.endswith("\n"))

    def test_duplicate_same_digest_is_noop(self) -> None:
        ev = self.ev()
        self.assertEqual(MODULE.append_event(self.stream, ev), "appended")
        self.assertEqual(MODULE.append_event(self.stream, ev), "duplicate")
        self.assertEqual(len(MODULE.read_stream(self.stream)), 1)

    def test_same_id_different_content_is_a_conflict(self) -> None:
        ev = self.ev()
        MODULE.append_event(self.stream, ev)
        clone = dict(ev, payload={"commit": "fail"})
        clone["digest"] = MODULE.digest_of(clone)
        with self.assertRaises(MODULE.Fail) as cm:
            MODULE.append_event(self.stream, clone)
        self.assertEqual(cm.exception.code, 4)

    def test_symlinked_stream_dir_is_rejected(self) -> None:
        outside = pathlib.Path(self.tmp.name) / "outside"
        outside.mkdir()
        link_dir = self.tasks_dir / "pjtest" / "exchanges" / "linked"
        link_dir.symlink_to(outside)
        with self.assertRaises(MODULE.Fail) as cm:
            MODULE.append_event(link_dir / "worker.jsonl", self.ev())
        self.assertEqual(cm.exception.code, 2)

    def test_symlinked_stream_file_is_rejected(self) -> None:
        real = pathlib.Path(self.tmp.name) / "real.jsonl"
        real.write_text("", encoding="utf-8")
        link = self.stream.parent / "planner.jsonl"
        link.symlink_to(real)
        with self.assertRaises(MODULE.Fail):
            MODULE.append_event(link, self.ev())

    def test_path_outside_tasks_dir_is_rejected(self) -> None:
        with self.assertRaises(MODULE.Fail) as cm:
            MODULE.append_event(pathlib.Path(self.tmp.name) / "stray.jsonl", self.ev())
        self.assertEqual(cm.exception.code, 2)

    def test_korean_multiline_payload_stays_one_line_and_roundtrips(self) -> None:
        finding = "한국어 소견입니다.\n두 번째 줄 — 근거 포함."
        ev = self.ev(type_="code_review", from_="reviewer", to="worker",
                     payload={"findings": [{"finding": finding}]})
        MODULE.append_event(self.stream, ev)
        raw = self.stream.read_text(encoding="utf-8")
        self.assertEqual(len(raw.splitlines()), 1)      # newline stays escaped in JSON
        self.assertIn("한국어", raw)                     # ensure_ascii=False
        back = MODULE.read_stream(self.stream)[0]
        self.assertEqual(back["payload"]["findings"][0]["finding"], finding)
        self.assertEqual(back["digest"], MODULE.digest_of(back))

    def test_wakeup_refuses_multiline(self) -> None:
        with self.assertRaises(MODULE.Fail) as cm:
            MODULE.send_wakeup("WS", "SURF", "line one\nline two")
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
