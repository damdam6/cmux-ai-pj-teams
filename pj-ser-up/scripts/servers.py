#!/usr/bin/env python3
"""Configured paired development servers; no repository edits or persistent port state."""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit

PACKAGE = Path(__file__).resolve().parents[2]
PRELOAD = Path(__file__).with_name("keep-env.cjs")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FAILURE = re.compile(r"PJ_SERVER_EXIT=[1-9]|^Node\.js v\d|ELIFECYCLE|Found [1-9]\d* errors?", re.M)


def argv(value, label):
    if not isinstance(value, list) or not value or not all(
            isinstance(v, str) and v and "\0" not in v for v in value):
        raise ValueError(f"{label} requires a nonempty argv array")
    return value


def relative(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Expected a relative path")
    p = Path(value)
    if p.is_absolute() or ".." in p.parts or p == Path("."):
        raise ValueError(f"Path must stay below the project root: {value!r}")
    return p


def port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(f"Invalid port: {value!r}")
    return value


def load(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Configure {path} from _shared/data/servers.example.json first")
    config = json.loads(path.read_text(encoding="utf-8"))
    for side in ("backend", "frontend"):
        spec = config[side]
        relative(spec["directory"])
        argv(spec["command"], f"{side}.command")
        if spec.get("install"):
            argv(spec["install"], f"{side}.install")
            relative(spec["installMarker"])
        env = spec.get("env", {})
        if not isinstance(env, dict) or any(not ENV_KEY.fullmatch(k) or not isinstance(v, str)
                                           or "\0" in v for k, v in env.items()):
            raise ValueError(f"Invalid environment for {side}")
        keys = spec.get("preserveEnv", [])
        if not isinstance(keys, list) or any(not isinstance(k, str) or k not in env for k in keys):
            raise ValueError(f"{side}.preserveEnv must list keys from its env object")
        if not isinstance(spec["healthPath"], str) or not spec["healthPath"].startswith("/"):
            raise ValueError(f"{side}.healthPath must start with /")
    ports = config["ports"]
    count = ports["count"]
    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError("ports.count must be 1..100")
    ranges = []
    for side in ("backend", "frontend"):
        first = port(ports[side])
        port(first + count - 1)
        ranges.append(set(range(first, first + count)))
    if ranges[0] & ranges[1]:
        raise ValueError("Backend and frontend port ranges must not overlap")
    for value in config.get("requiredPorts", []):
        port(value)
    if type(config["timeout"]) is not int or not 1 <= config["timeout"] <= 3600:
        raise ValueError("timeout must be 1..3600 seconds")
    db = config["database"]
    if db.get("mode") not in ("none", "isolated"):
        raise ValueError("Set database.mode to none (no DB), or isolated with a prepare argv")
    if db["mode"] == "isolated":
        argv(db.get("prepare"), "database.prepare")
    return config


def run_capture(command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, check=True, **kwargs).stdout.strip()


def directories(root, config):
    root = Path(root).resolve()
    result = {}
    for side in ("backend", "frontend"):
        path = (root / config[side]["directory"]).resolve()
        if not path.is_relative_to(root) or not path.is_dir():
            raise ValueError(f"Missing or escaping {side} directory")
        top = Path(run_capture(["git", "-C", str(path), "rev-parse", "--show-toplevel"]))
        if top.resolve() != path:
            raise ValueError(f"{side} must name a Git checkout/worktree root")
        result[side] = path
    if result["backend"] == result["frontend"]:
        raise ValueError("Backend and frontend must be distinct checkouts")
    return result


def resolve_root(start, config):
    start = Path(start).resolve()
    for root in (start, *start.parents):
        if all((root / config[s]["directory"]).is_dir() for s in ("backend", "frontend")):
            directories(root, config)
            return root
    raise ValueError("No paired backend/frontend checkout found above the current directory")


def listeners(number):
    if not shutil.which("lsof"):
        raise ValueError("lsof is required to inspect port ownership")
    result = subprocess.run(["lsof", "-nP", f"-iTCP:{port(number)}", "-sTCP:LISTEN", "-t"],
                            capture_output=True, text=True)
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise ValueError(f"Cannot inspect port {number}: {result.stderr.strip()}")
    owners = []
    for pid in sorted(set(result.stdout.split())):
        if not pid.isdigit():
            raise ValueError("Unexpected lsof process ID")
        cwd = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                             capture_output=True, text=True)
        paths = [line[1:] for line in cwd.stdout.splitlines() if line.startswith("n")]
        owners.append({"pid": int(pid), "cwd": paths[0] if paths else None})
    return owners


def belongs(owner, directory):
    return bool(owner["cwd"] and Path(owner["cwd"]).resolve().is_relative_to(directory))


def scan(root, config, index=None):
    dirs = directories(root, config)
    count = config["ports"]["count"]
    if index is not None and not 0 <= index < count:
        raise ValueError(f"index must be 0..{count - 1}")
    pairs, occupied, own = [], [], set()
    for n in range(count):
        pair = {}
        for side in ("backend", "frontend"):
            number = config["ports"][side] + n
            owners = listeners(number)
            pair[side] = {"port": number, "owners": owners}
            for owner in owners:
                is_self = belongs(owner, dirs[side])
                occupied.append(dict(owner, side=side, port=number, self=is_self))
                if is_self:
                    own.add(n)
        pairs.append(pair)
    if len(own) > 1 or (own and index is not None and index not in own):
        raise ValueError("This worktree already uses a different or inconsistent port pair")
    selected = index if index is not None else next(iter(own), None)
    if selected is None:
        selected = next((n for n, pair in enumerate(pairs)
                         if all(not p["owners"] for p in pair.values())), None)
    if selected is None:
        raise ValueError("No free port pair in the configured range")
    pair = pairs[selected]
    for side, data in pair.items():
        if any(not belongs(o, dirs[side]) for o in data["owners"]):
            raise ValueError(f"Port {data['port']} belongs to another process: {data['owners']}")
    return {"root": str(root), "index": selected, "occupied": occupied,
            "backend": pair["backend"], "frontend": pair["frontend"]}


def context(root, config, be_port, fe_port):
    dirs = directories(root, config)
    be_port, fe_port = port(be_port), port(fe_port)
    index = be_port - config["ports"]["backend"]
    if not 0 <= index < config["ports"]["count"] or fe_port != config["ports"]["frontend"] + index:
        raise ValueError("Ports must be one of the configured pairs")
    return {"ROOT": str(Path(root).resolve()), "BE_DIR": str(dirs["backend"]),
            "FE_DIR": str(dirs["frontend"]), "BE_PORT": str(be_port), "FE_PORT": str(fe_port),
            "BE_URL": f"http://localhost:{be_port}", "FE_URL": f"http://localhost:{fe_port}"}


def expand(value, values):
    # Only these named placeholders are interpreted; shell syntax and other braces stay literal.
    return re.sub(r"\{(ROOT|BE_DIR|FE_DIR|BE_PORT|FE_PORT|BE_URL|FE_URL)\}",
                  lambda m: values[m[1]], value)


def prepare_database(config, values, env):
    db = config["database"]
    if db["mode"] == "none":
        return {"mode": "none"}
    output = run_capture([expand(v, values) for v in db["prepare"]], cwd=values["BE_DIR"],
                         env=env, timeout=config["timeout"])
    report = json.loads(output)
    if not isinstance(report, dict) or report.get("mode") != "isolated" or not report.get("database"):
        raise ValueError("DB preparation did not confirm an isolated database; server not started")
    # Do not echo arbitrary tool output or connection credentials.
    return {"mode": "isolated", "database": report["database"]}


def wait_ready(url, timeout, surface=None):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Health checks must use a local HTTP URL")
    deadline = time.monotonic() + timeout
    while True:
        if surface:
            screen = run_capture(["cmux", "read-screen", "--surface", surface, "--lines", "60"])
            if FAILURE.search(screen):
                raise ValueError(f"Server startup failed in {surface}; inspect its output")
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        try:
            connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""))
            if connection.getresponse().status == 200:
                return {"ready": True, "url": url}
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Server readiness timed out: {url}")
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def run_server(side, config, values, be_surface=None):
    spec = config[side]
    directory = values["BE_DIR" if side == "backend" else "FE_DIR"]
    number = int(values["BE_PORT" if side == "backend" else "FE_PORT"])
    if listeners(number):
        raise ValueError(f"Port {number} is already occupied; no process was stopped")
    for required in config.get("requiredPorts", []):
        if not listeners(required):
            raise ValueError(f"Required local service port {required} is not listening")
    env = dict(os.environ, **{k: expand(v, values) for k, v in spec.get("env", {}).items()})
    if spec.get("install") and not (Path(directory) / spec["installMarker"]).exists():
        subprocess.run([expand(v, values) for v in spec["install"]], cwd=directory, env=env, check=True)
    if side == "backend":
        print(json.dumps({"database": prepare_database(config, values, env)}), flush=True)
    else:
        wait_ready(values["BE_URL"] + config["backend"]["healthPath"], config["timeout"], be_surface)
    if spec.get("preserveEnv"):
        env["PJ_KEEP_ENV_KEYS"] = ",".join(spec["preserveEnv"])
        # Node's NODE_OPTIONS parser accepts double-quoted paths; shell quoting differs.
        env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --require "
                               + json.dumps(str(PRELOAD), ensure_ascii=False)).strip()
    if listeners(number):
        raise ValueError(f"Port {number} became occupied during preparation")
    result = subprocess.run([expand(v, values) for v in spec["command"]], cwd=directory, env=env)
    print(f"PJ_SERVER_EXIT={result.returncode or 1}", flush=True)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(PACKAGE / "_shared/data/servers.local.json"))
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--index", type=int)
    for action in ("command", "run"):
        p = sub.add_parser(action)
        p.add_argument("--side", choices=("backend", "frontend"), required=True)
        p.add_argument("--root", required=True)
        p.add_argument("--be-port", type=int, required=True)
        p.add_argument("--fe-port", type=int, required=True)
        p.add_argument("--be-surface")
    p = sub.add_parser("wait")
    p.add_argument("--url", required=True)
    p.add_argument("--surface")
    p.add_argument("--timeout", type=int)
    args = parser.parse_args()
    try:
        config = load(args.config)
        if args.action == "scan":
            print(json.dumps(scan(resolve_root(args.cwd, config), config, args.index)))
        elif args.action == "wait":
            timeout = args.timeout if args.timeout is not None else config["timeout"]
            if not 1 <= timeout <= 3600:
                raise ValueError("timeout must be 1..3600")
            print(json.dumps(wait_ready(args.url, timeout, args.surface)))
        else:
            values = context(args.root, config, args.be_port, args.fe_port)
            if args.action == "run":
                return run_server(args.side, config, values, args.be_surface)
            command = [sys.executable, str(Path(__file__).resolve()), "--config",
                       str(Path(args.config).expanduser().resolve()), "run", "--side", args.side,
                       "--root", values["ROOT"], "--be-port", values["BE_PORT"], "--fe-port", values["FE_PORT"]]
            if args.be_surface:
                command += ["--be-surface", args.be_surface]
            print(shlex.join(command))
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        print(f"BLOCKER={exc}", file=sys.stderr)
        if args.action == "run":
            print("PJ_SERVER_EXIT=1", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
