#!/usr/bin/env python3
"""save-repo-alias.py — Upsert a `{short}` repo alias into the configured private alias file.

Used by worktree creation (Step 4a) when a previously-unseen repo is detected, so the chosen
abbreviation persists for next time. Values are taken from argv (NOT interpolated into
source), so a repo key or abbreviation containing quotes is stored as plain data and can
never break out into executed code.

The aliases file comes from PJ_ALIASES / pj_config. Preserve cached commit policy and
other fields under the same lock used by policy registration.

Usage:
    save-repo-alias.py --key <repo-key> --short <ABBR>

Exit codes:
    0  Alias written
    2  Bad usage or invalid settings
"""
from __future__ import annotations

import argparse
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
from pj_aliases import update_alias
_load_config()


ALIASES = _aliases_path()


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--key", required=True, help="Repo key (git-common-dir basename)")
    ap.add_argument("--short", required=True, help="Abbreviation, e.g. FE")
    args = ap.parse_args()

    try:
        update_alias(ALIASES, args.key, lambda entry: {**entry, "short": args.short})
    except (OSError, ValueError) as exc:
        print(f"alias error: {exc}", file=sys.stderr)
        return 2
    print(f"alias saved: {args.key} -> {args.short}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
