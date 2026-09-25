"""pj-link-family.sh — portable PJ family (+ _shared) into a session root.

The combine path (pj-board-wt / pj-open-ws after wire-session-root) has to land
pj-ser-up at the FE+BE parent even when a child checkout never ran /pj. The glob
is the family, so this pins pj-ser-up without a one-skill special case.
"""
from __future__ import annotations

import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-link-family.sh"
PACKAGE = pathlib.Path(__file__).resolve().parents[2]


def run(target: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), "--target", str(target)],
        capture_output=True, text=True)


def test_links_pj_ser_up_and_shared_into_both_runtimes(tmp_path):
    p = run(tmp_path)
    assert p.returncode == 0, p.stderr
    for runtime in (".claude", ".codex"):
        ser = tmp_path / runtime / "skills" / "pj-ser-up"
        shared = tmp_path / runtime / "skills" / "_shared"
        assert ser.is_symlink()
        assert ser.resolve() == (PACKAGE / "pj-ser-up").resolve()
        assert (ser / "SKILL.md").is_file()
        assert shared.is_symlink()
        assert shared.resolve() == (PACKAGE / "_shared").resolve()
        assert f"linked: {ser}" in p.stdout


def test_second_run_is_already_linked(tmp_path):
    run(tmp_path)
    p = run(tmp_path)
    assert p.returncode == 0, p.stderr
    lines = p.stdout.splitlines()
    assert f"already-linked: {tmp_path}/.claude/skills/pj-ser-up" in lines
    assert not any(line.startswith("linked:") for line in lines)


def test_does_not_overwrite_a_foreign_link(tmp_path):
    dest = tmp_path / ".claude" / "skills"
    dest.mkdir(parents=True)
    (dest / "pj-ser-up").symlink_to(tmp_path / "elsewhere")
    p = run(tmp_path)
    assert p.returncode == 0, p.stderr
    assert f"SKIP (exists, not the package link): {dest}/pj-ser-up" in p.stdout
    assert (tmp_path / ".codex/skills/pj-ser-up/SKILL.md").is_file()
    assert (dest / "pj/SKILL.md").is_file()
    assert (dest / "pj-ser-up").is_symlink()
    assert (dest / "pj-ser-up").readlink() == tmp_path / "elsewhere"


def test_refuses_a_missing_target(tmp_path):
    p = run(tmp_path / "nope")
    assert p.returncode == 1
    assert "usage:" in p.stderr
