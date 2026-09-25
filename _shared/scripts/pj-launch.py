#!/usr/bin/env python3
"""Run a standard agent CLI and register its live child in the cmux surface registry."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

from pj_config import launcher_profiles, load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    config = load_config()
    profiles = launcher_profiles()
    if args.profile not in profiles:
        parser.error(f"Unknown profile: {args.profile}; available: {', '.join(profiles)}")
    spec = profiles[args.profile]
    extra = args.args[1:] if args.args[:1] == ["--"] else args.args
    entry = None
    workspace = os.environ.get("CMUX_WORKSPACE_ID")
    surface = os.environ.get("CMUX_SURFACE_ID")
    if workspace and surface:
        if not all(re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in (workspace, surface)):
            parser.error("Invalid cmux workspace/surface identity")
        entry = Path(config["PJ_CMUX_REGISTRY_ROOT"]) / workspace / surface
        entry.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Inherit the terminal; no pipe and no shell aliases, eval, or permission overrides.
        env = dict(os.environ, **spec.get("env", {}))
        if "CODEX_HOME" in spec.get("env", {}):
            env["CODEX_HOME"] = str(Path(env["CODEX_HOME"]).expanduser())
        child = subprocess.Popen(spec["argv"] + extra, env=env)
    except OSError as exc:
        parser.exit(2, f"Cannot launch {spec['argv'][0]}: {exc}\n")
    if entry:
        temporary = entry.with_name(f".{surface}.{child.pid}.tmp")
        context = {"profile": args.profile, "cwd": os.getcwd()}
        if spec["runtime"] == "codex":
            context["codex_home"] = str(Path(env.get("CODEX_HOME") or "~/.codex").expanduser())
        temporary.write_text(f"kind={spec['runtime']}\npid={child.pid}\nsurface={surface}\n"
                             + "context=" + json.dumps(context, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(entry)
    def terminate(signum, frame):
        if child.poll() is None:
            child.send_signal(signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, terminate)
    try:
        while True:
            try:
                return child.wait()
            except KeyboardInterrupt:
                # Ctrl-C is already delivered to the foreground process group.
                continue
    finally:
        if entry and entry.exists() and f"pid={child.pid}\n" in entry.read_text():
            entry.unlink()


if __name__ == "__main__":
    sys.exit(main())
