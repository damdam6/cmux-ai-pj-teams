#!/usr/bin/env python3
"""wt-registry.py — manage <main-repo>/.worktrees/registry.json

Shared by worktree creation (writes entries on creation) and worktree registry reconciliation (reads/prunes).

registry.json schema:
{
  "upstreams": { "<branch>": "<upstream-branch>" },     // A` -> A mapping
  "worktrees": [
    {
      "branch": "refactor/TASK-123-prerequisite-computed-block",
      "baseBranch": "feature/my-work",                  // branch it was forked from
      "path": ".worktrees/prerequisite-computed-block", // relative to main repo, OR absolute
                                                        // when the worktree lives outside it
      "workspaceName": "pre-block",                     // cmux workspace name (primary lookup key)
      "workspaceId": "workspace:12",                    // cmux ref at creation time (hint; may go stale)
      "ticket": "TASK-123",                              // optional
      "createdAt": "2026-06-12"
    }
  ]
}

Subcommands:
  add          --repo R --branch B --base BASE --path P --workspace-name N
               [--workspace-id ID] [--ticket T]          (upserts by branch)
  list         --repo R [--base BASE]                    (prints JSON array)
  prune        --repo R                                  (drop entries whose worktree path or branch is gone)
  resolve      --repo R --branch B [--heal]              (branch -> live cmux workspace ref, or exit 1)
  reconcile    --repo R                                  (refresh every stale workspaceId from live cmux state)
  remove       --repo R --branch B                       (delete one entry by branch; exit 1 if absent)
  get-upstream --repo R --branch B                       (prints upstream or exits 1)
  set-upstream --repo R --branch B --upstream U

A worktree does not have to live under the repo. The pj family places them at
`{root}/{name}/{repo-folder}` instead, so `path` may be an absolute path outside the repo; every
read joins it as `repo / path`, which yields the absolute path unchanged. Relative entries
written before that change keep resolving exactly as they did — both forms coexist by design,
because the existing worktrees are not being moved.

resolve/reconcile bridge the registry to live cmux state, scanning ALL cmux windows
(workspace:N refs are global and targetable cross-window). workspaceId is only a hint
(cmux reassigns refs when a workspace is closed/reopened), so the primary lookup key is
workspaceName -> live workspace `title`. resolve prefers a still-live workspaceId and
falls back to title; reconcile re-derives every entry's id by title. An entry reported
`missing` (no live workspace in any window) is NOT auto-deleted — the caller decides
(registry maintenance asks the user, then calls `remove`).
"""
import argparse
import datetime
import json
import pathlib
import subprocess
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()



def registry_path(repo: str) -> pathlib.Path:
    return pathlib.Path(repo) / ".worktrees" / "registry.json"


def load(repo: str) -> dict:
    p = registry_path(repo)
    if p.exists():
        d = json.loads(p.read_text())
    else:
        d = {}
    d.setdefault("upstreams", {})
    d.setdefault("worktrees", [])
    return d


def save(repo: str, d: dict) -> None:
    p = registry_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")


def _workspaces_in_window(window_id=None):
    """List workspaces of one cmux window (or the caller's, if window_id is None). None on failure."""
    cmd = ["cmux", "workspace", "list", "--json"]
    if window_id:
        cmd += ["--window", window_id]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        return json.loads(out).get("workspaces", [])
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        return None


def live_workspaces():
    """Return live cmux workspaces across ALL windows (each has `ref` + `title`), or None if cmux is
    unavailable. `workspace:N` refs are global, so a ref found here is targetable from any window
    without a --window flag. None means "could not query cmux" — callers must NOT treat it as empty."""
    try:
        wout = subprocess.run(
            ["cmux", "list-windows", "--json"],
            capture_output=True, text=True, check=True,
        ).stdout
        windows = json.loads(wout)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        # No window enumeration — fall back to the caller's window only.
        return _workspaces_in_window()

    result, seen, any_ok = [], set(), False
    for w in windows:
        ws = _workspaces_in_window(w.get("id"))
        if ws is None:
            continue
        any_ok = True
        for x in ws:
            ref = x.get("ref")
            if ref in seen:
                continue
            seen.add(ref)
            result.append(x)
    # If every per-window query failed, signal unavailable rather than a false "empty".
    return result if any_ok else None


def cmd_add(a) -> int:
    d = load(a.repo)
    entry = {
        "branch": a.branch,
        "baseBranch": a.base,
        "path": a.path,
        "workspaceName": a.workspace_name,
        "workspaceId": a.workspace_id or "",
        "ticket": a.ticket or "",
        "createdAt": datetime.date.today().isoformat(),
    }
    wts = [w for w in d["worktrees"] if w.get("branch") != a.branch]
    wts.append(entry)
    d["worktrees"] = wts
    save(a.repo, d)
    print(f"registered: {a.branch} -> {a.path} (workspace: {a.workspace_name})")
    return 0


def cmd_list(a) -> int:
    d = load(a.repo)
    wts = d["worktrees"]
    if a.base:
        wts = [w for w in wts if w.get("baseBranch") == a.base]
    print(json.dumps(wts, indent=2, ensure_ascii=False))
    return 0


def cmd_prune(a) -> int:
    d = load(a.repo)
    repo = pathlib.Path(a.repo)
    try:
        out = subprocess.run(
            ["git", "-C", a.repo, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout
        live_paths = {ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")}
    except subprocess.CalledProcessError:
        live_paths = None  # git failed; fall back to path existence only

    kept, dropped = [], []
    for w in d["worktrees"]:
        abs_path = str((repo / w.get("path", "")).resolve())
        alive = (repo / w.get("path", "")).is_dir()
        if alive and live_paths is not None:
            alive = abs_path in live_paths
        (kept if alive else dropped).append(w)
    d["worktrees"] = kept
    save(a.repo, d)
    for w in dropped:
        print(f"pruned: {w.get('branch')} ({w.get('path')})")
    print(f"kept {len(kept)}, pruned {len(dropped)}")
    return 0


def cmd_resolve(a) -> int:
    d = load(a.repo)
    entry = next((w for w in d["worktrees"] if w.get("branch") == a.branch), None)
    if entry is None:
        print(f"no registry entry for branch: {a.branch}", file=sys.stderr)
        return 1

    live = live_workspaces()
    if live is None:
        # cmux unavailable — emit the stored id as a best-effort hint.
        wid = entry.get("workspaceId")
        if wid:
            print(wid)
            return 0
        print("cmux unavailable and no stored workspaceId", file=sys.stderr)
        return 1

    refs = {w.get("ref") for w in live}
    wid = entry.get("workspaceId")
    if wid and wid in refs:
        print(wid)
        return 0

    # workspaceId is stale or empty — fall back to the primary key (name -> title).
    name = entry.get("workspaceName")
    match = next((w for w in live if w.get("title") == name), None)
    if match is None:
        print(f"no live workspace for branch {a.branch} (name={name!r}, staleId={wid!r})", file=sys.stderr)
        return 1
    ref = match.get("ref")
    if a.heal and ref != wid:
        entry["workspaceId"] = ref
        save(a.repo, d)
    print(ref)
    return 0


def cmd_reconcile(a) -> int:
    d = load(a.repo)
    live = live_workspaces()
    if live is None:
        print("cmux unavailable — cannot reconcile", file=sys.stderr)
        return 2

    refs = {w.get("ref") for w in live}
    by_title = {}
    for w in live:
        by_title.setdefault(w.get("title"), w)  # first wins on duplicate titles

    healed, missing, ok = [], [], 0
    for w in d["worktrees"]:
        wid = w.get("workspaceId")
        if wid and wid in refs:
            ok += 1
            continue
        match = by_title.get(w.get("workspaceName"))
        if match is not None:
            w["workspaceId"] = match.get("ref")
            healed.append((w.get("branch"), wid, match.get("ref")))
        else:
            missing.append((w.get("branch"), w.get("workspaceName")))
    save(a.repo, d)

    for branch, old, new in healed:
        print(f"healed: {branch}: {old or '∅'} -> {new}")
    for branch, name in missing:
        print(f"missing: {branch} (workspace {name!r} not live)")
    print(f"ok {ok}, healed {len(healed)}, missing {len(missing)}")
    return 0


def cmd_remove(a) -> int:
    d = load(a.repo)
    before = len(d["worktrees"])
    d["worktrees"] = [w for w in d["worktrees"] if w.get("branch") != a.branch]
    removed = before - len(d["worktrees"])
    if removed:
        save(a.repo, d)
        print(f"removed: {a.branch}")
        return 0
    print(f"no entry for branch: {a.branch}", file=sys.stderr)
    return 1


def cmd_get_upstream(a) -> int:
    d = load(a.repo)
    up = d["upstreams"].get(a.branch)
    if not up:
        return 1
    print(up)
    return 0


def cmd_set_upstream(a) -> int:
    d = load(a.repo)
    d["upstreams"][a.branch] = a.upstream
    save(a.repo, d)
    print(f"upstream: {a.branch} -> {a.upstream}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def base_args(p, branch=False):
        p.add_argument("--repo", required=True, help="main repo absolute path")
        if branch:
            p.add_argument("--branch", required=True)

    p = sub.add_parser("add")
    base_args(p, branch=True)
    p.add_argument("--base", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--workspace-name", required=True)
    p.add_argument("--workspace-id", default="")
    p.add_argument("--ticket", default="")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("list")
    base_args(p)
    p.add_argument("--base", default="")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("prune")
    base_args(p)
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("resolve")
    base_args(p, branch=True)
    p.add_argument("--heal", action="store_true",
                   help="write the freshly-resolved ref back into the registry")
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("reconcile")
    base_args(p)
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("remove")
    base_args(p, branch=True)
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("get-upstream")
    base_args(p, branch=True)
    p.set_defaults(fn=cmd_get_upstream)

    p = sub.add_parser("set-upstream")
    base_args(p, branch=True)
    p.add_argument("--upstream", required=True)
    p.set_defaults(fn=cmd_set_upstream)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
