"""Verify scoped typechecks never execute the legacy whole-project command."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/pj-typecheck.py"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("pj_typecheck_scoped_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-b", "project")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (root / "untouched.ts").write_text("untouched")
    git("add", "untouched.ts")
    git("commit", "-m", "base")
    git("checkout", "-b", "task")
    for name in ("changed file.ts", "-option.ts"):
        (root / name).write_text("changed")
    git("add", "--", "changed file.ts", "-option.ts")
    git("commit", "-m", "task changes")
    aliases = tmp_path / "aliases.json"
    monkeypatch.setattr(module, "ALIASES", aliases)
    monkeypatch.chdir(root)
    monkeypatch.setattr(module, "task_info", lambda slug: {"repo": "repo"})
    monkeypatch.setattr(module, "pick_repo", lambda *args: ("repo", "project", str(root)))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--slug", "task", "--changed-only"])
    return module, root, aliases


def config(aliases, cmd=None):
    entry = {"typecheckCmd": "touch WHOLE_PROJECT_WAS_RUN"}
    if cmd is not None:
        entry["typecheckChangedCmd"] = cmd
    aliases.write_text(json.dumps({"aliases": {"repo": entry}}))


def test_only_changed_paths_reach_checker(setup):
    module, root, aliases = setup
    script = "import json,sys; from pathlib import Path; Path('received.json').write_text(json.dumps(sys.argv[1:]))"
    config(aliases, [sys.executable, "-c", script, "{files}"])
    assert module.main() == 0
    assert set(json.loads((root / "received.json").read_text())) == {"./changed file.ts", "./-option.ts"}
    assert not (root / "WHOLE_PROJECT_WAS_RUN").exists()


@pytest.mark.parametrize("cmd", ["tsc --noEmit", ["tsc", "--noEmit"]])
def test_missing_or_unscoped_command_never_falls_back(setup, cmd, capsys):
    module, root, aliases = setup
    config(aliases, cmd)
    assert module.main() == 2
    assert not (root / "WHOLE_PROJECT_WAS_RUN").exists()
    assert "PJ_TYPECHECK=ok" not in capsys.readouterr().out


def test_bad_base_never_runs_checker(setup):
    module, root, aliases = setup
    config(aliases, ["touch", "{files}"])
    assert module.run_changed_only("repo", "nonexistent-base", str(root)) == 2


def test_no_changes_reports_none(setup, capsys):
    module, root, aliases = setup
    config(aliases, ["must-not-run", "{files}"])
    assert module.run_changed_only("repo", "HEAD", str(root)) == 3
    assert "PJ_TYPECHECK=none" in capsys.readouterr().out


def test_scoped_failure_blocks_completion(setup, capsys):
    module, root, aliases = setup
    config(aliases, [sys.executable, "-c", "import sys; sys.exit(1)", "{files}"])
    assert module.main() == 1
    assert "PJ_TYPECHECK=fail" in capsys.readouterr().out
    assert not (root / "WHOLE_PROJECT_WAS_RUN").exists()


@pytest.fixture
def typescript_repo(setup):
    module, root, aliases = setup
    compiler = os.environ.get("PJ_TEST_TYPESCRIPT")
    if not compiler:
        pytest.skip("Set PJ_TEST_TYPESCRIPT to a local typescript package for compiler integration tests")
    modules = root / "node_modules"
    modules.mkdir()
    (modules / "typescript").symlink_to(Path(compiler).resolve(), target_is_directory=True)
    (root / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {"strict": True, "target": "ES2020", "noEmit": True},
        "include": ["**/*.ts"],
    }))
    (root / "changed file.ts").write_text("export const value: number = 1;")
    (root / "-option.ts").write_text("export const flag = true;")
    (root / "untouched.ts").write_text("const bad: number = 'unrelated'; const syntax = ;")
    config(aliases)
    return module, root, aliases


def test_builtin_runs_without_extra_configuration(typescript_repo, capsys):
    module, root, aliases = typescript_repo
    assert module.main() == 0
    output = capsys.readouterr().out
    assert "checked=2" in output and "PJ_TYPECHECK=ok" in output
    assert "untouched.ts" not in output
    assert not (root / "WHOLE_PROJECT_WAS_RUN").exists()
    assert not list(root.glob("*.js"))


def test_builtin_catches_real_changed_type_error(typescript_repo, capsys):
    module, root, aliases = typescript_repo
    (root / "changed file.ts").write_text("export const value: number = 'wrong';")
    assert module.main() == 1
    assert '"code": 2322' in capsys.readouterr().out


def test_builtin_preserves_extends_paths_and_ambient_types(typescript_repo):
    module, root, aliases = typescript_repo
    (root / "tsconfig.base.json").write_text(json.dumps({
        "compilerOptions": {"strict": True, "paths": {"@/*": ["./lib/*"]}},
    }))
    (root / "tsconfig.json").write_text('{"extends":"./tsconfig.base.json","include":["**/*.ts"]}')
    (root / "lib").mkdir()
    (root / "lib/value.ts").write_text("export const value: number = 1; const unrelated: number = 'bad';")
    (root / "globals.d.ts").write_text("declare const AMBIENT: number;")
    (root / "changed file.ts").write_text("import {value} from '@/value'; export const n: number = value + AMBIENT;")
    assert module.main() == 0
    (root / "changed file.ts").write_text("import {value} from '@/value'; export const n: string = value;")
    assert module.main() == 1


def test_builtin_uses_referenced_app_configuration(typescript_repo, capsys):
    module, root, aliases = typescript_repo
    (root / "tsconfig.json").write_text('{"files":[],"references":[{"path":"./tsconfig.app.json"}]}')
    (root / "tsconfig.app.json").write_text(json.dumps({
        "compilerOptions": {"strict": True, "composite": True}, "include": ["*.ts"],
    }))
    assert module.main() == 0
    assert '"config": "tsconfig.app.json"' in capsys.readouterr().out
    assert not list(root.glob("*.tsbuildinfo"))


def test_builtin_missing_config_is_error_not_skip(typescript_repo, capsys):
    module, root, aliases = typescript_repo
    (root / "tsconfig.json").unlink()
    assert module.main() == 2
    assert "PJ_TYPECHECK=unparsed" in capsys.readouterr().out


def test_changed_declaration_is_checked_even_with_skip_lib_check(typescript_repo):
    module, root, aliases = typescript_repo
    (root / "changed.d.ts").write_text("declare const broken: MissingType;")
    (root / "tsconfig.json").write_text('{"compilerOptions":{"skipLibCheck":true},"include":["*.ts"]}')
    helper = SCRIPT.with_name("pj-typecheck-changed.cjs")
    result = subprocess.run(["node", str(helper)], input=json.dumps({
        "root": str(root), "files": ["changed.d.ts"],
    }), text=True, capture_output=True)
    assert result.returncode == 1
    receipt = json.loads(result.stdout)
    assert receipt["checked"] == [{"file": "changed.d.ts", "config": "tsconfig.json"}]
    assert any(item["code"] == 2304 for item in receipt["diagnostics"])
