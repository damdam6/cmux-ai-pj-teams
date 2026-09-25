"""Portable paths and launcher profiles shared by the PJ distribution."""
import json
import os
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]
DEFAULTS = {
    "PJ_VAULT": "~/.local/share/pj",
    "PJ_REPOS_ROOT": "~/projects",
    "PJ_WORKTREE_ROOT": "~/worktrees",
    "PJ_ALIASES": str(PACKAGE / "_shared/data/repo-aliases.local.json"),
    "PJ_CMUX_REGISTRY_ROOT": "/tmp/cmux-agents",
}


def load_config():
    path = PACKAGE / "config.local.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise ValueError(f"Invalid PJ settings: {path}; see config.example.json")
    for key, default in DEFAULTS.items():
        value = os.environ.get(key, config.get(key, default))
        if not isinstance(value, str) or not value or "\n" in value:
            raise ValueError(f"{key} must be a nonempty path")
        value = Path(value).expanduser()
        if not value.is_absolute():
            raise ValueError(f"{key} must be absolute (or start with ~)")
        os.environ[key] = str(value)
    os.environ["PJ_PACKAGE_ROOT"] = str(PACKAGE)
    return {key: os.environ[key] for key in (*DEFAULTS, "PJ_PACKAGE_ROOT")}


def aliases_path():
    return Path(load_config()["PJ_ALIASES"])


def launcher_profiles():
    path = PACKAGE / "_shared/data/launchers.local.json"
    if not path.exists():
        path = path.with_name("launchers.example.json")
    profiles = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("Launcher profiles must be a nonempty object")
    for name, spec in profiles.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or not isinstance(spec, dict):
            raise ValueError(f"Invalid launcher profile: {name!r}")
        if spec.get("runtime") not in ("claude", "codex", "grok"):
            raise ValueError(f"Invalid launcher runtime: {name}")
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x and "\0" not in x for x in argv):
            raise ValueError(f"Launcher {name} requires a nonempty argv array")
        env = spec.get("env", {})
        if not isinstance(env, dict) or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
                or not isinstance(v, str) or "\0" in v for k, v in env.items()):
            raise ValueError(f"Invalid launcher environment: {name}")
        option = spec.get("reviewOption", spec["runtime"])
        if "reviewOption" in spec and (not isinstance(option, str) or option not in profiles):
            raise ValueError(f"Unknown reviewOption for {name}: {option!r}")
        reviewers = spec.get("reviewers", {})
        if not isinstance(reviewers, dict) or any(
                not isinstance(v, str) or v not in profiles for v in reviewers.values()):
            raise ValueError(f"Invalid reviewer profiles: {name}")
    return profiles


if __name__ == "__main__":
    import argparse
    import shlex
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell", action="store_true", help="print quoted exports for a shell")
    args = parser.parse_args()
    config = load_config()
    if args.shell:
        print("\n".join(f"export {key}={shlex.quote(value)}" for key, value in config.items()))
    else:
        print(json.dumps(config, indent=2))
