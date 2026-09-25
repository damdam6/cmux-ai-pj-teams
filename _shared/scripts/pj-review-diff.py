#!/usr/bin/env python3
"""Read a task branch's cumulative changes, including its current working tree.

No index writes or commits. Compare the merge base to the working tree so committed,
staged and unstaged changes appear once in their current form; append untracked files.
Exit 0 = reviewable changes, 3 = empty scope, 2 = unavailable or wrong worktree.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def git(cwd: Path, *args: str, allowed=(0,)) -> bytes:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)
    if result.returncode not in allowed:
        raise ValueError(result.stderr.decode(errors="replace").strip()
                         or f"git {args[0]} failed ({result.returncode})")
    return result.stdout


def collect(worktree: Path, base: str, head: str) -> tuple[dict, bytes]:
    root = Path(os.fsdecode(git(worktree, "rev-parse", "--show-toplevel").rstrip(b"\n")))
    current = git(root, "rev-parse", "--verify", "HEAD").strip()
    expected = git(root, "rev-parse", "--verify", "--end-of-options", head + "^{commit}").strip()
    branch = git(root, "symbolic-ref", "-q", "HEAD", allowed=(0, 1)).strip()
    requested = git(root, "rev-parse", "--symbolic-full-name", "--verify", "--end-of-options", head).strip()
    if current != expected or (requested.startswith(b"refs/heads/") and branch != requested):
        raise ValueError(f"Wrong worktree: expected task branch {head}; use the worker's worktree")
    base_oid = git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").strip()
    ancestor = git(root, "merge-base", base_oid.decode(), expected.decode()).strip().decode()
    conflicts = git(root, "diff", "--name-only", "--diff-filter=U", "-z", "--")
    if conflicts:
        raise ValueError("Unresolved merge conflicts: review scope is unavailable")
    names = git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", ancestor, "--")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    files = [os.fsdecode(p) for p in names.split(b"\0") if p]
    new_files = [os.fsdecode(p) for p in untracked.split(b"\0") if p]
    patch = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", ancestor, "--")
    for path in new_files:
        patch += git(root, "diff", "--no-index", "--no-ext-diff", "--no-textconv",
                     "--no-color", "--", "/dev/null", path, allowed=(0, 1))
    manifest = {"worktree": str(root), "base": base, "head": head,
                "merge_base": ancestor, "head_oid": expected.decode(),
                "tracked_files": files, "untracked_files": new_files}
    return manifest, patch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    try:
        manifest, patch = collect(args.worktree, args.base, args.head)
    except (ValueError, OSError) as exc:
        print("PJ_REVIEW_DIFF=error " + json.dumps({"reason": str(exc)}, ensure_ascii=False))
        return 2
    ready = bool(manifest["tracked_files"] or manifest["untracked_files"])
    print("PJ_REVIEW_DIFF=" + ("ready" if ready else "empty"))
    print(json.dumps(manifest, ensure_ascii=False))
    sys.stdout.flush()
    sys.stdout.buffer.write(patch)
    return 0 if ready else 3


if __name__ == "__main__":
    raise SystemExit(main())
