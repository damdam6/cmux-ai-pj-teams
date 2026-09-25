#!/usr/bin/env python3
"""Collect commit-style evidence and persist the agent's reviewed repository policy.

Git and configuration are read as data. No hooks, config modules, commits, or AI calls run here.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from pj_config import aliases_path
from pj_aliases import read_aliases, update_alias

PATTERNS = (
    "AGENTS.md", "CLAUDE.md", "CONTRIBUTING*", "README*", ".gitmessage*",
    "commitlint.config.*", ".commitlintrc*", ".husky/commit-msg",
    ".github/CONTRIBUTING*", ".github/*commit*.md", "docs/**/*commit*.md",
    "docs/**/*contribut*.md",
)


def git(repo, *args, optional=False):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, timeout=15)
    if result.returncode and not optional:
        raise ValueError(result.stderr.strip() or "Git repository could not be read")
    return result.stdout.strip() if result.returncode == 0 else None


def identity(repo):
    root = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    return {"repoKey": common.parent.name if common.name == ".git" else common.name,
            "commonDir": str(common), "worktree": str(root),
            "head": git(root, "rev-parse", "--verify", "HEAD", optional=True)}


def file_bytes(root, path):
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Commit policy source must stay inside the repository: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError(f"Commit policy source must be a regular file <= 1 MiB: {path}")
        return handle.read()


def sources(info, extra=()):
    root = Path(info["worktree"])
    if (not isinstance(extra, (list, tuple)) or len(extra) > 40 or any(
            not isinstance(p, str) or Path(p).is_absolute() or ".." in Path(p).parts for p in extra)):
        raise ValueError("Extra sources must be repository-relative file paths")
    paths = {p for pattern in PATTERNS for p in root.glob(pattern) if not p.is_dir()}
    paths.update(root / p for p in extra)
    template = git(root, "config", "--get", "commit.template", optional=True)
    external_template = None
    if template:
        target = Path(template).expanduser()
        if not target.is_absolute():
            target = root / target
        if target.resolve().is_relative_to(root):
            paths.add(target)
        else:
            external_template = template  # Report the setting; never read outside the repo.
    result = []
    for path in sorted(paths):
        if not path.resolve().is_relative_to(root):
            result.append({"path": str(path.relative_to(root)),
                           "externalLink": str(path.resolve())})
            continue
        if not path.exists():
            result.append({"path": str(path.relative_to(root)), "missing": True})
            continue
        content = file_bytes(root, path)
        result.append({"path": str(path.relative_to(root)),
                       "sha256": hashlib.sha256(content).hexdigest()})
    package = root / "package.json"
    if package.exists():
        try:
            data = json.loads(file_bytes(root, package))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict) and "commitlint" in data:
            content = json.dumps(data["commitlint"], sort_keys=True, ensure_ascii=False)
            result.append({"path": "package.json#commitlint",
                           "sha256": hashlib.sha256(content.encode()).hexdigest()})
    return {"files": result, "externalTemplate": external_template}


def inspect_repo(repo, extra=()):
    info = identity(repo)
    samples = []
    if info["head"]:
        for line in git(info["worktree"], "log", "-30", "--no-merges", "--format=%H%x00%s").splitlines():
            oid, separator, subject = line.partition("\0")
            if separator:
                samples.append({"commit": oid, "subject": subject})
    return {"schema": 1, **info, "extraSources": list(extra),
            "sources": sources(info, extra), "samples": samples}


def validate_policy(policy, snapshot):
    if not isinstance(policy, dict):
        raise ValueError("Policy must be a JSON object")
    allowed = {"status", "origin", "subject", "body", "language", "integration",
               "examples", "notes", "sourcePaths"}
    if set(policy) != allowed:
        raise ValueError(f"Policy requires exactly these fields: {', '.join(sorted(allowed))}")
    if policy["status"] not in ("ready", "needs-confirmation"):
        raise ValueError("Policy status must be ready or needs-confirmation")
    if policy["origin"] not in ("instructions", "history", "user"):
        raise ValueError("Policy origin must be instructions, history, or user")
    for key in ("subject", "body", "language", "integration", "notes"):
        if not isinstance(policy[key], str) or len(policy[key]) > 4000 or "\0" in policy[key]:
            raise ValueError(f"Policy {key} must be text up to 4000 characters")
    if policy["status"] == "ready" and not policy["subject"].strip():
        raise ValueError("A ready policy needs an actionable subject rule")
    for key, limit in (("examples", 5), ("sourcePaths", 40)):
        if (not isinstance(policy[key], list) or len(policy[key]) > limit
                or any(not isinstance(v, str) or len(v) > 1000 for v in policy[key])):
            raise ValueError(f"Invalid {key}")
    available = {item["path"] for item in snapshot["sources"]["files"] if "sha256" in item}
    if set(policy["sourcePaths"]) - available:
        raise ValueError("sourcePaths must name files from the analysis snapshot")
    if policy["origin"] == "instructions" and not policy["sourcePaths"]:
        raise ValueError("Instruction-based policy needs at least one source file")
    if policy["origin"] == "history" and policy["status"] == "ready" and not snapshot["samples"]:
        raise ValueError("History-based policy needs commit samples")
    external = snapshot["sources"].get("externalTemplate") or any(
        "externalLink" in item for item in snapshot["sources"]["files"])
    if external and policy["origin"] != "user" and policy["status"] == "ready":
        raise ValueError("External rule sources need a user-defined policy or needs-confirmation")
    return policy


def get_policy(repo, alias_file):
    info = identity(repo)
    entry = read_aliases(alias_file).get("aliases", {}).get(info["repoKey"], {})
    cached = entry.get("commitPolicy") if isinstance(entry, dict) else None
    if cached is None:
        return {"repoKey": info["repoKey"], "status": "missing"}
    if not isinstance(cached, dict) or cached.get("schema") != 1:
        raise ValueError("Invalid cached commit policy; inspect before replacing it")
    validate_policy(cached["policy"], cached)
    if cached["commonDir"] != info["commonDir"]:
        return {"repoKey": info["repoKey"], "status": "stale", "reason": "repository moved or alias reused"}
    if cached["sources"] != sources(info, cached.get("extraSources", [])):
        return {"repoKey": info["repoKey"], "status": "stale", "reason": "commit rule sources changed"}
    return {"repoKey": info["repoKey"], "status": cached["policy"]["status"],
            "analyzedAt": cached["analyzedAt"], "analysisHead": cached["head"],
            "policy": cached["policy"]}


def save_policy(repo, alias_file, snapshot, policy, refresh=False):
    info = identity(repo)
    if snapshot.get("schema") != 1 or any(snapshot.get(k) != info[k] for k in ("repoKey", "commonDir", "head")):
        raise ValueError("Analysis snapshot no longer matches this repository/HEAD; inspect again")
    if snapshot.get("sources") != sources(info, snapshot.get("extraSources", [])):
        raise ValueError("Commit rule sources changed during analysis; inspect again")
    validate_policy(policy, snapshot)
    cached = {"schema": 1, "commonDir": info["commonDir"], "head": info["head"],
              "analyzedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "sources": snapshot["sources"],
              "extraSources": snapshot.get("extraSources", []),
              "samples": [{"commit": s["commit"]} for s in snapshot["samples"]],
              "policy": policy}
    def update(entry):
        if "commitPolicy" in entry and not refresh:
            raise ValueError("Commit policy already exists; use --refresh for a reviewed update")
        return {**entry, "commitPolicy": cached}
    update_alias(alias_file, info["repoKey"], update)
    return {"repoKey": info["repoKey"], "status": policy["status"], "saved": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("get", "inspect", "save"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--repo", type=Path, required=True)
        if name == "inspect":
            cmd.add_argument("--source", action="append", default=[],
                             help="Additional repository-relative rule file (repeatable)")
        if name == "save":
            cmd.add_argument("--analysis", type=Path, required=True)
            cmd.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect_repo(args.repo, args.source)
        elif args.command == "get":
            result = get_policy(args.repo, aliases_path())
        else:
            result = save_policy(args.repo, aliases_path(), json.loads(args.analysis.read_text()),
                                 json.load(sys.stdin), args.refresh)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f"PJ_COMMIT_POLICY=error {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
