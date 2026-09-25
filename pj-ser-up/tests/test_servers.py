"""Server helper contracts in scratch checkouts; no real servers, DBs or cmux tabs."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/servers.py"
spec = importlib.util.spec_from_file_location("pj_servers", SCRIPT)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


@pytest.fixture
def pair(tmp_path):
    root = tmp_path / "pair ' with spaces"
    for side in ("backend", "frontend"):
        path = root / side
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
    config = json.loads((M.PACKAGE / "_shared/data/servers.example.json").read_text())
    config["database"] = {"mode": "none"}
    config["ports"]["count"] = 3
    path = tmp_path / "servers config.json"
    path.write_text(json.dumps(config))
    return root, config, path


def test_config_requires_explicit_db_choice_and_rejects_path_escape(pair):
    root, config, path = pair
    assert M.load(path)["database"]["mode"] == "none"
    for mutate in (lambda c: c["database"].update(mode="configure"),
                   lambda c: c["backend"].update(directory="../outside"),
                   lambda c: c["ports"].update(frontend=5000),
                   lambda c: c["ports"].update(count=-1),
                   lambda c: c["backend"].update(preserveEnv=["NOT_CONFIGURED"])):
        changed = copy.deepcopy(config)
        mutate(changed)
        path.write_text(json.dumps(changed))
        with pytest.raises(ValueError):
            M.load(path)


def test_root_discovery_and_symlink_escape(pair, tmp_path):
    root, config, _ = pair
    assert M.resolve_root(root / "frontend", config) == root
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escaped").symlink_to(outside, target_is_directory=True)
    config["backend"]["directory"] = "escaped"
    with pytest.raises(ValueError, match="escaping"):
        M.directories(root, config)


def test_scan_skips_occupied_pair_and_refuses_forced_conflict(pair, monkeypatch):
    root, config, _ = pair
    owners = {5000: [{"pid": 12, "cwd": "/other"}]}
    monkeypatch.setattr(M, "listeners", lambda p: owners.get(p, []))
    result = M.scan(root, config)
    assert result["index"] == 1
    assert result["backend"]["port"] == 5001
    assert result["frontend"]["port"] == 3101
    with pytest.raises(ValueError, match="another process"):
        M.scan(root, config, index=0)
    with pytest.raises(ValueError, match="index"):
        M.scan(root, config, index=9)


def test_scan_reuses_own_pair_and_detects_multiple_owners(pair, monkeypatch):
    root, config, _ = pair
    owners = {5002: [{"pid": 12, "cwd": str(root / "backend")}]}
    monkeypatch.setattr(M, "listeners", lambda p: owners.get(p, []))
    assert M.scan(root, config)["index"] == 2
    with pytest.raises(ValueError, match="different"):
        M.scan(root, config, index=1)
    owners[5002].append({"pid": 99, "cwd": None})
    with pytest.raises(ValueError, match="another process"):
        M.scan(root, config)


def test_command_shell_quoting_is_one_literal_argv(pair):
    root, config, path = pair
    result = subprocess.run([sys.executable, str(SCRIPT), "--config", str(path), "command",
                             "--side", "backend", "--root", str(root),
                             "--be-port", "5001", "--fe-port", "3101"],
                            capture_output=True, text=True, check=True)
    args = shlex.split(result.stdout)
    assert args[args.index("--root") + 1] == str(root)
    assert args[args.index("--config") + 1] == str(path)
    with pytest.raises(ValueError, match="pair"):
        M.context(root, config, 5001, 3102)


def test_db_preparation_and_environment_are_literal_and_gate_launch(pair, monkeypatch, tmp_path):
    root, config, _ = pair
    record = tmp_path / "report.json"
    adapter = tmp_path / "db adapter.py"
    adapter.write_text('import json; print(json.dumps({"mode":"isolated", "database":"branch_test"}))')
    app = tmp_path / "app.py"
    app.write_text('import json, os, pathlib, sys\npathlib.Path(sys.argv[1]).write_text(json.dumps(' 
                   '{"port":os.environ["PORT"], "url":os.environ["API"], "arg":sys.argv[2]}))')
    literal = "$(touch do-not-create); ' data"
    config["backend"].update(command=[sys.executable, str(app), str(record), literal], install=None,
                              env={"PORT": "{BE_PORT}", "API": "{FE_URL}"})
    config["database"] = {"mode": "isolated", "prepare": [sys.executable, str(adapter)]}
    monkeypatch.setattr(M, "listeners", lambda p: [])
    values = M.context(root, config, 5001, 3101)
    assert M.run_server("backend", config, values) == 0
    assert json.loads(record.read_text()) == {"port": "5001", "url": "http://localhost:3101", "arg": literal}
    record.unlink()
    adapter.write_text('print(\'{"mode":"shared", "database":"shared"}\')')
    with pytest.raises(ValueError, match="isolated"):
        M.run_server("backend", config, values)
    assert not record.exists()
    adapter.write_text('raise SystemExit(2)')
    with pytest.raises(subprocess.CalledProcessError):
        M.run_server("backend", config, values)
    assert not record.exists()


def test_frontend_wait_failure_prevents_start(pair, monkeypatch, tmp_path):
    root, config, _ = pair
    marker = tmp_path / "started"
    config["frontend"].update(install=None, command=[sys.executable, "-c",
        f"from pathlib import Path; Path({str(marker)!r}).touch()"])
    monkeypatch.setattr(M, "listeners", lambda p: [])
    def failed(*args):
        raise TimeoutError("backend health")
    monkeypatch.setattr(M, "wait_ready", failed)
    with pytest.raises(TimeoutError):
        M.run_server("frontend", config, M.context(root, config, 5000, 3100))
    assert not marker.exists()


def test_install_runs_only_when_marker_missing_and_after_port_gate(pair, monkeypatch, tmp_path):
    root, config, _ = pair
    calls = tmp_path / "installed"
    config["backend"].update(command=[sys.executable, "-c", "pass"], install=[sys.executable, "-c",
        f"from pathlib import Path; Path({str(calls)!r}).touch(); Path('node_modules').mkdir()"])
    monkeypatch.setattr(M, "listeners", lambda p: [])
    values = M.context(root, config, 5000, 3100)
    M.run_server("backend", config, values)
    assert calls.exists()
    calls.unlink()
    M.run_server("backend", config, values)
    assert not calls.exists()
    monkeypatch.setattr(M, "listeners", lambda p: [{"pid": 1, "cwd": None}])
    with pytest.raises(ValueError, match="occupied"):
        M.run_server("backend", config, values)


def test_health_requires_200_and_detects_failed_tab(monkeypatch):
    statuses = iter([503, 200])
    class HTTP:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args): pass
        def getresponse(self):
            return type("Response", (), {"status": next(statuses)})()
        def close(self): pass
    monkeypatch.setattr(M.http.client, "HTTPConnection", HTTP)
    monkeypatch.setattr(M.time, "sleep", lambda n: None)
    assert M.wait_ready("http://localhost:5000/health", 10)["ready"]
    monkeypatch.setattr(M, "run_capture", lambda cmd: "PJ_SERVER_EXIT=1")
    with pytest.raises(ValueError, match="failed"):
        M.wait_ready("http://localhost:5000/health", 10, "surface:4")
    with pytest.raises(ValueError, match="local HTTP"):
        M.wait_ready("https://example.com/", 10)


def test_preload_preserves_only_configured_keys(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for optional preload integration")
    overwrite = tmp_path / "overwrite.cjs"
    overwrite.write_text("process.env.PORT='wrong'; process.env.UNRELATED='changed';")
    relocated = tmp_path / "한글 preload ' directory"
    relocated.mkdir()
    preload = relocated / "keep-env.cjs"
    shutil.copy2(M.PRELOAD, preload)
    env = dict(os.environ, PJ_KEEP_ENV_KEYS="PORT", PORT="5003", UNRELATED="original",
               NODE_OPTIONS="--require " + json.dumps(str(preload), ensure_ascii=False))
    result = subprocess.run([node, "-e",
        "require(process.argv[1]);console.log(JSON.stringify([process.env.PORT,process.env.UNRELATED]))",
        str(overwrite)], env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["5003", "changed"]
