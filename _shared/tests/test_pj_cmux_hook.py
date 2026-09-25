#!/usr/bin/env python3
"""Hook adapter: marker extraction, per-runtime output shapes, and never-break semantics."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-cmux.py"
SPEC = importlib.util.spec_from_file_location("pj_cmux", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

REQ_A = "req_" + "a" * 16
REQ_B = "req_" + "b" * 16


def claude_payload(stdout: str) -> str:
    """Shape observed from Claude Code PostToolUse for Bash (fields beyond tool_response are
    incidental — the hook scans text, it does not depend on the schema)."""
    return json.dumps({
        "session_id": "sess-1", "cwd": "/tmp", "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pj-cmux.py request done.report --slug t-one"},
        "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False},
    })


def run_hook(payload: str, runtime: str = "claude") -> tuple[int, dict | str]:
    out = io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(out):
        rc = MODULE.hook_run(runtime)
    text = out.getvalue().strip()
    try:
        return rc, json.loads(text)
    except ValueError:
        return rc, text


class HookTests(unittest.TestCase):
    def test_marker_relayed_with_same_turn_feedback(self) -> None:
        with mock.patch.object(MODULE, "relay_request",
                               return_value="pj-cmux: delivered") as relay:
            rc, out = run_hook(claude_payload(f"event=evt\nPJ_CMUX_REQUEST={REQ_A}\n"))
        self.assertEqual(rc, 0)
        relay.assert_called_once_with(REQ_A, None)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("delivered", out["hookSpecificOutput"]["additionalContext"])

    def test_multiple_markers_relay_in_order_deduplicated(self) -> None:
        text = f"PJ_CMUX_REQUEST={REQ_A}\nPJ_CMUX_REQUEST={REQ_B}\nPJ_CMUX_REQUEST={REQ_A}\n"
        with mock.patch.object(MODULE, "relay_request", return_value="ok") as relay:
            rc, _ = run_hook(claude_payload(text))
        self.assertEqual([c.args[0] for c in relay.call_args_list], [REQ_A, REQ_B])

    def test_no_marker_is_quiet_and_touches_nothing(self) -> None:
        with mock.patch.object(MODULE, "relay_request") as relay, \
                mock.patch.object(MODULE, "append_event") as append:
            rc, out = run_hook(claude_payload("plain output\n"))
        self.assertEqual(rc, 0)
        relay.assert_not_called()
        append.assert_not_called()
        self.assertEqual(out, {"continue": True, "suppressOutput": True})

    def test_relay_failure_reports_pending_with_retry_command_exit_zero(self) -> None:
        with mock.patch.object(MODULE, "relay_request",
                               side_effect=MODULE.Fail(3, "대상 세션이 없습니다")):
            rc, out = run_hook(claude_payload(f"PJ_CMUX_REQUEST={REQ_A}\n"))
        self.assertEqual(rc, 0)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("PENDING", ctx)
        self.assertIn(f"retry --request-id {REQ_A}", ctx)

    def test_unexpected_exception_never_raises(self) -> None:
        with mock.patch.object(MODULE, "relay_request",
                               side_effect=RuntimeError("boom")):
            rc, out = run_hook(claude_payload(f"PJ_CMUX_REQUEST={REQ_A}\n"))
        self.assertEqual(rc, 0)
        self.assertIn("PENDING", out["hookSpecificOutput"]["additionalContext"])

    def test_garbage_stdin_is_quiet(self) -> None:
        rc, out = run_hook("this is not json {{{")
        self.assertEqual(rc, 0)
        self.assertEqual(out, {"continue": True, "suppressOutput": True})

    def test_codex_runtime_feedback_and_quiet_shapes(self) -> None:
        # codex supports hookSpecificOutput.additionalContext, but NOT suppressOutput/continue
        # (unsupported keys mark the run failed) — quiet must be exactly {}
        with mock.patch.object(MODULE, "relay_request", return_value="delivered") as relay:
            rc, out = run_hook(claude_payload(f"PJ_CMUX_REQUEST={REQ_A}\n"),
                               runtime="codex")
        self.assertEqual(rc, 0)
        relay.assert_called_once()
        self.assertEqual(list(out.keys()), ["hookSpecificOutput"])
        self.assertIn("delivered", out["hookSpecificOutput"]["additionalContext"])
        rc, out = run_hook(claude_payload("no marker here\n"), runtime="codex")
        self.assertEqual(out, {})

    def test_marker_regex_matches_only_the_exact_shape(self) -> None:
        self.assertEqual(MODULE.MARKER_RE.findall("PJ_CMUX_REQUEST=req_short"), [])
        self.assertEqual(MODULE.MARKER_RE.findall(f"request={REQ_A}"), [])
        self.assertEqual(MODULE.MARKER_RE.findall(f"x PJ_CMUX_REQUEST={REQ_A} y"), [REQ_A])


if __name__ == "__main__":
    unittest.main()
