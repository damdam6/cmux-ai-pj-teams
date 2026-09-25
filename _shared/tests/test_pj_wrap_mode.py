"""Persistent wrap preferences through the real CLI in isolated data roots."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/pj-wrap-mode.py"


def run(vault, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          env=dict(os.environ, PJ_VAULT=str(vault)),
                          capture_output=True, text=True, timeout=5)


def test_unset_default_is_merge_without_creating_data(tmp_path):
    vault = tmp_path / "not created"
    result = run(vault, "get")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"mode": "merge", "source": "builtin"}
    assert not vault.exists()


def test_choice_persists_across_cli_calls_and_can_return_to_merge(tmp_path):
    vault = tmp_path / "data ' with spaces"
    for mode in ("squash", "merge"):
        saved = run(vault, "set", mode)
        assert saved.returncode == 0, saved.stderr
        read = run(vault, "get")
        assert read.returncode == 0, read.stderr
        assert json.loads(read.stdout) == {"mode": mode, "source": "saved"}
    path = vault / "raw/tasks/pj-wrap-default.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_unknown_mode_cannot_replace_saved_preference(tmp_path):
    assert run(tmp_path, "set", "squash").returncode == 0
    path = tmp_path / "raw/tasks/pj-wrap-default.json"
    before = path.read_bytes()
    assert run(tmp_path, "set", "rebase").returncode != 0
    assert run(tmp_path, "set", "merge", "squash").returncode != 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("content", ["{bad", "[]", '{"mode":"unknown"}'])
def test_invalid_existing_settings_are_reported_and_preserved(tmp_path, content):
    path = tmp_path / "raw/tasks/pj-wrap-default.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    for args in (("get",), ("set", "merge")):
        result = run(tmp_path, *args)
        assert result.returncode == 2
        assert "PJ_WRAP_MODE=error" in result.stderr
        assert path.read_text() == content


@pytest.mark.parametrize("name", ["pj-wrap-default.json", "pj-wrap-default.lock"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_settings_and_lock_do_not_follow_links_or_open_special_files(tmp_path, name, kind):
    vault = tmp_path / "vault"
    target = tmp_path / "outside.json"
    target.write_text('{"mode":"squash"}')
    path = vault / "raw/tasks" / name
    path.parent.mkdir(parents=True)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path)
    result = run(vault, "set", "merge")
    assert result.returncode == 2, result.stdout
    assert target.read_text() == '{"mode":"squash"}'
    if name.endswith(".json"):
        assert run(vault, "get").returncode == 2


def test_task_directory_symlink_is_not_followed(tmp_path):
    vault = tmp_path / "vault"
    (vault / "raw").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "raw/tasks").symlink_to(outside, target_is_directory=True)
    assert run(vault, "get").returncode == 2
    assert run(vault, "set", "squash").returncode == 2
    assert list(outside.iterdir()) == []


def test_concurrent_writers_leave_one_complete_setting(tmp_path):
    env = dict(os.environ, PJ_VAULT=str(tmp_path))
    processes = [subprocess.Popen([sys.executable, str(SCRIPT), "set", mode], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for mode in ["merge", "squash"] * 4]
    try:
        for process in processes:
            _, err = process.communicate(timeout=5)
            assert process.returncode == 0, err
        result = run(tmp_path, "get")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["mode"] in ("merge", "squash")
        assert not list((tmp_path / "raw/tasks").glob("*.tmp"))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
