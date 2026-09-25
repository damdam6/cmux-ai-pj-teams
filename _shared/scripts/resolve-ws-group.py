#!/usr/bin/env python3
"""resolve-ws-group.py — find the cmux workspace group that owns a given cwd.

Given the working directory of the *current* workspace, locate the cmux
workspace whose current_directory matches, then the group (if any) that
lists that workspace as a member. Used by worktree creation to inherit the current
workspace's group for a newly created worktree workspace.

Output (stdout): when a group is found, a single line
    <group_ref>\t<group_name>
e.g. `workspace_group:2\tFE-repo`. Prints nothing when the cwd's workspace
is ungrouped or unmatched.

Exit codes:
  0  resolved (group found OR cleanly not-in-a-group — check stdout)
  2  cmux unavailable / CLI error
"""
import json
import os
import subprocess
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()



def cmux_json(*args):
    out = subprocess.run(
        ["cmux", *args, "--json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"cmux {' '.join(args)} failed")
    return json.loads(out.stdout)


def main():
    cwd = None
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--cwd" and i + 1 < len(argv):
            cwd = argv[i + 1]
    if not cwd:
        print("usage: resolve-ws-group.py --cwd <path>", file=sys.stderr)
        return 2

    target = os.path.realpath(cwd)

    try:
        ws = cmux_json("workspace", "list")
        groups = cmux_json("workspace-group", "list")
    except (RuntimeError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"cmux 조회 실패: {e}", file=sys.stderr)
        return 2

    # current_directory -> ref for the matching workspace
    my_ref = None
    for w in ws.get("workspaces", []):
        wd = w.get("current_directory")
        if wd and os.path.realpath(wd) == target:
            my_ref = w.get("ref")
            break
    if not my_ref:
        return 0  # current cwd has no matching workspace → nothing to inherit

    for g in groups.get("groups", []):
        if my_ref in g.get("member_workspace_refs", []):
            print(f"{g.get('ref')}\t{g.get('name', '')}")
            return 0

    return 0  # matched workspace but it is ungrouped


if __name__ == "__main__":
    sys.exit(main())
