"""Observable portability checks independent of the original workstation."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

PACKAGE = Path(__file__).resolve().parents[2]
SCRIPTS = PACKAGE / "_shared/scripts"
sys.path.insert(0, str(SCRIPTS))
import pj_config


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_install_complete_idempotent_and_refuses_conflicts(tmp_path):
    installer = module("install")
    project = tmp_path / "consumer with spaces"
    project.mkdir()
    assert installer.install(project, dry_run=True) == 44
    assert list(project.iterdir()) == []
    assert installer.install(project) == 44
    assert installer.install(project) == 0
    for runtime in (".claude", ".codex"):
        assert (project / runtime / "skills/pj/SKILL.md").is_file()
        assert (project / runtime / "skills/pj-rebase/scripts/rebase.py").is_file()
        assert (project / runtime / "skills/pj-wrap-default/SKILL.md").is_file()
        assert (project / runtime / "skills/pj-watcher/SKILL.md").is_file()
        assert (project / runtime / "skills/_shared/scripts/pj-startup-watch.py").is_file()
        assert (project / runtime / "skills/_shared/scripts/pj-wrap-mode.py").is_file()
        assert (project / runtime / "skills/pj-ser-up/scripts/servers.py").is_file()
        assert (project / runtime / "skills/_shared/scripts/pj-tasks.py").is_file()
    other = tmp_path / "conflict"
    (other / ".claude/skills/pj-work").mkdir(parents=True)
    with pytest.raises(ValueError, match="nothing changed"):
        installer.install(other)
    assert not (other / ".codex").exists()


def test_config_precedence_paths_and_shell_quoting(tmp_path, monkeypatch):
    package = tmp_path / "package ' with spaces"
    package.mkdir()
    monkeypatch.setattr(pj_config, "PACKAGE", package)
    for key in pj_config.DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    saved = tmp_path / "local data"
    env = tmp_path / "override ' path"
    (package / "config.local.json").write_text(json.dumps({"PJ_VAULT": str(saved)}))
    assert pj_config.load_config()["PJ_VAULT"] == str(saved)
    monkeypatch.setenv("PJ_VAULT", str(env))
    assert pj_config.load_config()["PJ_VAULT"] == str(env)
    output = subprocess.check_output([sys.executable, str(SCRIPTS / "pj_config.py"), "--shell"], text=True)
    observed = subprocess.check_output(["bash", "-c", output + '\nprintf "%s" "$PJ_VAULT"'], text=True)
    assert observed == str(env)


def test_launch_standard_argv_registry_and_cleanup_in_relocated_package(tmp_path):
    package = tmp_path / "relocated package"
    scripts = package / "_shared/scripts"
    scripts.mkdir(parents=True)
    data = package / "_shared/data"
    data.mkdir()
    for name in ("pj-launch.py", "pj_config.py"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    # A local fake CLI checks the parent's registry and preserves literal argv bytes.
    fake = tmp_path / "fake_codex.py"
    report = tmp_path / "report.json"
    literal_env = "spaces ' and $(touch should-not-exist)"
    fake.write_text('''import json, os, pathlib, sys, time
p = pathlib.Path(os.environ["PJ_CMUX_REGISTRY_ROOT"]) / "workspace-test" / "surface-test"
for _ in range(100):
    if p.exists(): break
    time.sleep(.01)
pathlib.Path(sys.argv[1]).write_text(json.dumps({"args":sys.argv[2:], "registry":p.read_text(),
    "account":os.environ["CODEX_HOME"], "literal":os.environ["PJ_TEST_LITERAL"]}))
''')
    (data / "launchers.local.json").write_text(json.dumps({
        "my-profile": {"runtime": "codex", "argv": [sys.executable, str(fake), str(report)],
                       "env": {"CODEX_HOME": str(tmp_path / "work account"),
                               "PJ_TEST_LITERAL": literal_env}}
    }))
    env = dict(os.environ, CMUX_WORKSPACE_ID="workspace-test", CMUX_SURFACE_ID="surface-test",
               PJ_CMUX_REGISTRY_ROOT=str(tmp_path / "registry"))
    literal = "spaces ' and $(touch should-not-exist)"
    result = subprocess.run([sys.executable, str(scripts / "pj-launch.py"),
                             "--profile", "my-profile", "--", literal], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    observed = json.loads(report.read_text())
    assert observed["args"] == [literal]
    assert observed["account"] == str(tmp_path / "work account")
    assert observed["literal"] == literal_env
    assert "kind=codex\n" in observed["registry"]
    assert "surface=surface-test\n" in observed["registry"]
    context = json.loads(next(line.removeprefix("context=") for line in observed["registry"].splitlines()
                              if line.startswith("context=")))
    assert context == {"profile": "my-profile", "cwd": os.getcwd(),
                       "codex_home": str(tmp_path / "work account")}
    assert not (tmp_path / "registry/workspace-test/surface-test").exists()


def test_alias_upsert_preserves_other_settings(tmp_path):
    path = tmp_path / "aliases.json"
    policy = {"schema": 1, "policy": {"subject": "saved format"}}
    path.write_text(json.dumps({"aliases": {"demo": {"short": "OLD", "linkPaths": [".codex/skills"],
                                                     "commitPolicy": policy}}}))
    subprocess.run([sys.executable, str(SCRIPTS / "save-repo-alias.py"), "--key", "demo", "--short", "NEW"],
                   env=dict(os.environ, PJ_ALIASES=str(path)), check=True, capture_output=True)
    assert json.loads(path.read_text())["aliases"]["demo"] == {
        "short": "NEW", "linkPaths": [".codex/skills"], "commitPolicy": policy}


def test_profile_runtime_controls_skill_sigil(monkeypatch):
    transport = module("pj-cmux")
    monkeypatch.setattr(transport, "launcher_profiles", lambda: {
        "custom-reviewer": {"runtime": "codex", "argv": ["codex"]}})
    assert transport.sigil_for("custom-reviewer") == "$"
    argv = shlex.split(transport.launcher_shell_command("custom-reviewer"))
    assert argv[-3:] == ["--profile", "custom-reviewer", "--"]


def test_surface_runtime_must_match_requested_profile(monkeypatch):
    transport = module("pj-cmux")
    monkeypatch.setattr(transport, "registry_find_surface", lambda *args:
                        ("ws", {"kind": "claude", "pid": "123"}))
    monkeypatch.setattr(transport, "pid_command", lambda pid: "claude --resume")
    with pytest.raises(transport.Fail, match="runtime"):
        transport.validate_target("ws", "surface", "codex")


def test_worker_startup_uses_configured_registry_without_shell_exports(tmp_path, monkeypatch):
    registry = tmp_path / "registry ' with spaces"
    (registry / "workspace-test").mkdir(parents=True)
    (registry / "workspace-test/ready").write_text("ready")
    monkeypatch.setenv("PJ_CMUX_REGISTRY_ROOT", str(registry))
    transport = module("pj-cmux")
    env = dict(os.environ, CMUX_WORKSPACE_ID="workspace-test")
    env.pop("PJ_CMUX_REGISTRY_ROOT", None)
    result = subprocess.run(["bash", "-c", transport.POLL_PREAMBLE + "printf ready"],
                            env=env, capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert result.stdout == "ready"
