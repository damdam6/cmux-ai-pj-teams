"""merge-settings.py — type-driven N-way JSON merge.

The property under test is that the rules are driven by the value's TYPE, not by its key: a
settings file that grows a key nobody enumerated must still merge, and anything the rules
cannot decide must be REPORTED rather than resolved in some input's favour. A settings file
resolved the wrong way fails in the permissive direction, which is the direction that matters.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "merge-settings.py"
SPEC = importlib.util.spec_from_file_location("merge_settings", SCRIPT)
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def merge(*inputs: dict) -> tuple[dict, list[str]]:
    conflicts: list[str] = []
    return M.merge_dicts(list(inputs), conflicts), conflicts


# ---------------------------------------------------------------- type-driven rules


def test_key_in_one_input_only_is_taken():
    got, c = merge({"a": 1}, {"b": 2})
    assert got == {"a": 1, "b": 2} and c == []


def test_equal_values_collapse():
    got, c = merge({"a": "x"}, {"a": "x"})
    assert got == {"a": "x"} and c == []


def test_lists_union_in_first_seen_order():
    got, c = merge({"allow": ["a", "b"]}, {"allow": ["b", "c"]})
    assert got["allow"] == ["a", "b", "c"] and c == []


def test_list_union_is_not_sorted():
    """Order is meaningful in permission lists, and a stable output keeps re-runs diffable."""
    got, _ = merge({"allow": ["z", "a"]}, {"allow": ["m"]})
    assert got["allow"] == ["z", "a", "m"]


def test_nested_dicts_recurse():
    got, c = merge({"permissions": {"allow": ["a"], "deny": ["x"]}},
                   {"permissions": {"allow": ["b"]}})
    assert got == {"permissions": {"allow": ["a", "b"], "deny": ["x"]}}
    assert c == []


def test_deeply_nested_dicts_recurse():
    got, _ = merge({"a": {"b": {"c": [1]}}}, {"a": {"b": {"c": [2], "d": 3}}})
    assert got == {"a": {"b": {"c": [1, 2], "d": 3}}}


def test_unknown_key_of_a_known_type_needs_no_rule_of_its_own():
    """The point of type-driven merging: a key nobody enumerated still merges."""
    got, c = merge({"somethingNew": ["p"]}, {"somethingNew": ["q"]})
    assert got["somethingNew"] == ["p", "q"] and c == []


def test_dicts_of_lists_inside_an_unknown_key():
    got, c = merge({"hooks": {"Stop": ["a"]}}, {"hooks": {"Stop": ["b"], "Start": ["c"]}})
    assert got["hooks"] == {"Stop": ["a", "b"], "Start": ["c"]} and c == []


# ---------------------------------------------------------------- conflicts


def test_differing_scalars_are_omitted_and_reported():
    """Picking one side would let a repo's setting govern another repo's work."""
    got, c = merge({"model": "opus"}, {"model": "sonnet"})
    assert "model" not in got
    assert len(c) == 1 and "CONFLICT model" in c[0]
    assert '"opus"' in c[0] and '"sonnet"' in c[0]


def test_differing_booleans_are_a_conflict_not_an_or():
    got, c = merge({"flag": True}, {"flag": False})
    assert "flag" not in got and len(c) == 1


def test_a_boolean_present_in_one_input_is_not_a_conflict():
    got, c = merge({"flag": True}, {})
    assert got == {"flag": True} and c == []


def test_mixed_types_are_a_conflict_not_a_coercion():
    """A list in one input and a scalar in another has no combination — coercing is how a
    `deny` list becomes a string."""
    got, c = merge({"deny": ["a"]}, {"deny": "a"})
    assert "deny" not in got and len(c) == 1


def test_a_conflict_deep_in_a_dict_reports_its_dotted_path():
    got, c = merge({"permissions": {"defaultMode": "auto"}},
                   {"permissions": {"defaultMode": "plan"}})
    assert got["permissions"] == {}
    assert "CONFLICT permissions.defaultMode" in c[0]


def test_a_conflict_does_not_discard_its_siblings():
    got, c = merge({"permissions": {"allow": ["a"], "defaultMode": "auto"}},
                   {"permissions": {"allow": ["b"], "defaultMode": "plan"}})
    assert got["permissions"]["allow"] == ["a", "b"]
    assert "defaultMode" not in got["permissions"]
    assert len(c) == 1


# ---------------------------------------------------------------- N inputs


def test_three_inputs_merge_at_once():
    got, c = merge({"allow": ["a"]}, {"allow": ["b"]}, {"allow": ["c"]})
    assert got["allow"] == ["a", "b", "c"] and c == []


def test_a_conflict_report_lists_every_differing_value():
    _, c = merge({"m": 1}, {"m": 2}, {"m": 3})
    assert c[0].count("|") == 2


def test_two_agreeing_inputs_and_one_dissenting_still_conflict():
    got, c = merge({"m": 1}, {"m": 1}, {"m": 2})
    assert "m" not in got and len(c) == 1


# ---------------------------------------------------------------- cli


def run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["python3", str(SCRIPT), *argv], capture_output=True, text=True)


@pytest.fixture
def files(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"permissions": {"allow": ["x"],
                                             "additionalDirectories": ["/one"]},
                             "enableAllProjectMcpServers": True}))
    b.write_text(json.dumps({"permissions": {"allow": ["y"],
                                             "additionalDirectories": ["/two"]}}))
    return {"a": a, "b": b, "dir": tmp_path}


def test_cli_writes_the_merged_file(files):
    out = files["dir"] / "nested" / "merged.json"
    p = run("--out", str(out), str(files["a"]), str(files["b"]))
    assert p.returncode == 0, p.stderr
    d = json.loads(out.read_text())
    assert d["permissions"]["allow"] == ["x", "y"]
    assert d["permissions"]["additionalDirectories"] == ["/one", "/two"]
    assert d["enableAllProjectMcpServers"] is True


def test_cli_print_writes_nothing(files):
    p = run("--print", str(files["a"]), str(files["b"]))
    assert p.returncode == 0
    assert json.loads(p.stdout)["permissions"]["allow"] == ["x", "y"]
    assert not (files["dir"] / "merged.json").exists()


def test_cli_is_idempotent(files):
    out = files["dir"] / "merged.json"
    run("--out", str(out), str(files["a"]), str(files["b"]))
    first = out.read_text()
    run("--out", str(out), str(files["a"]), str(files["b"]))
    assert out.read_text() == first


def test_cli_skips_a_missing_input_with_a_note(files):
    p = run("--print", str(files["a"]), str(files["dir"] / "ghost.json"))
    assert p.returncode == 0
    assert "파일 없음" in p.stderr
    assert json.loads(p.stdout)["permissions"]["allow"] == ["x"]


def test_cli_refuses_invalid_json(files):
    bad = files["dir"] / "bad.json"
    bad.write_text("{nope")
    p = run("--print", str(files["a"]), str(bad))
    assert p.returncode == 2
    assert "JSON 이 아닙니다" in p.stderr


def test_cli_refuses_a_non_object_top_level(files):
    arr = files["dir"] / "arr.json"
    arr.write_text("[1,2]")
    p = run("--print", str(files["a"]), str(arr))
    assert p.returncode == 2


def test_cli_requires_exactly_one_output_mode(files):
    assert run(str(files["a"])).returncode == 1
    assert run("--print", "--out", "/tmp/x", str(files["a"])).returncode == 1


def test_cli_all_inputs_missing_is_an_error(files):
    p = run("--print", str(files["dir"] / "ghost.json"))
    assert p.returncode == 1


def test_cli_reports_conflicts_but_still_exits_0(files):
    c = files["dir"] / "c.json"
    c.write_text(json.dumps({"enableAllProjectMcpServers": False}))
    p = run("--print", str(files["a"]), str(c))
    assert p.returncode == 0
    assert "CONFLICT enableAllProjectMcpServers" in p.stderr
    assert "enableAllProjectMcpServers" not in json.loads(p.stdout)


def test_cli_strict_exits_3_on_a_conflict(files):
    c = files["dir"] / "c.json"
    c.write_text(json.dumps({"enableAllProjectMcpServers": False}))
    p = run("--print", "--strict", str(files["a"]), str(c))
    assert p.returncode == 3


def test_cli_strict_exits_0_without_conflicts(files):
    p = run("--print", "--strict", str(files["a"]), str(files["b"]))
    assert p.returncode == 0
