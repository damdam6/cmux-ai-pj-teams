#!/usr/bin/env python3
"""merge-settings.py — Merge N JSON settings files into one, by TYPE, never by guessing.

WHY A TOOL AND NOT A MERGE
--------------------------
A session standing above several worktrees needs one settings file built from each repo's.
Hard-coding "union `permissions.allow`, union `additionalDirectories`, keep FE's MCP flags"
would work today and break the first time a repo grows a key nobody enumerated — silently, and
in the permissive direction, which is the direction that matters for a settings file.

So the rules are driven by the VALUE'S TYPE, not by its key, and anything the rules cannot
decide is reported instead of resolved:

    present in one input only   -> taken as is
    equal in every input        -> that value
    all lists                   -> union, first-seen order preserved (no sorting: order is
                                   meaningful in permission lists, and a stable output makes
                                   re-runs diffable)
    all dicts                   -> recurse, same rules
    differing scalars           -> CONFLICT: omitted from the output and reported

Omitting a conflicting scalar is deliberate. Picking one input's value would silently make a
repo's setting govern another repo's work; leaving the key out makes the consumer fall back to
its own default, and the report tells the user which key they have to decide. `--strict` turns
any conflict into a non-zero exit for callers that would rather stop than proceed with a hole.

Mixed types for one key (a list in one input, a scalar in another) are a conflict too — there
is no meaning to combine, and coercing one side is how a `deny` list becomes a string.

USAGE
    merge-settings.py --out <path> <input.json> [<input.json>...]
    merge-settings.py --print   <input.json> [...]      # to stdout, write nothing
    merge-settings.py --out <path> --strict <input.json> [...]

    A missing input is skipped with a note (a repo may simply not have the file); an input that
    is not valid JSON is an error, because merging around a broken file would produce a settings
    file that looks complete and is not.

OUTPUT
    Conflicts go to stderr, one per line:  CONFLICT <dotted.key>: <repr of each value>
    The merged JSON goes to --out (or stdout with --print).

EXIT CODES
    0  merged (conflicts may have been reported unless --strict)
    1  bad usage
    2  an input exists but is not valid JSON
    3  --strict and at least one conflict
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from pj_config import load_config as _load_config, aliases_path as _aliases_path
_load_config()


SENTINEL = object()


class Conflict(Exception):
    pass


def merge_values(key: str, values: list, conflicts: list[str]):
    """Merge one key's values from every input that had it. Raises Conflict when the rules
    cannot decide, so the caller can drop the key rather than invent a winner."""
    first = values[0]
    if all(v == first for v in values[1:]):
        return first
    if all(isinstance(v, list) for v in values):
        out: list = []
        for v in values:
            for item in v:
                if item not in out:
                    out.append(item)
        return out
    if all(isinstance(v, dict) for v in values):
        return merge_dicts(values, conflicts, prefix=key)
    raise Conflict(key)


def merge_dicts(inputs: list[dict], conflicts: list[str], prefix: str = "") -> dict:
    """Union of the keys, each value merged by type. Key order is first-seen across inputs, so
    a re-run of the same inputs produces the same file."""
    merged: dict = {}
    order: list[str] = []
    for d in inputs:
        for k in d:
            if k not in order:
                order.append(k)
    for k in order:
        dotted = f"{prefix}.{k}" if prefix else k
        values = [d[k] for d in inputs if k in d]
        try:
            merged[k] = merge_values(dotted, values, conflicts)
        except Conflict:
            conflicts.append(f"CONFLICT {dotted}: "
                             + " | ".join(json.dumps(v, ensure_ascii=False) for v in values))
    return merged


def load(path: pathlib.Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SystemExit(f"JSON 이 아닙니다: {path} ({e})")
    if not isinstance(data, dict):
        raise SystemExit(f"최상위가 객체가 아닙니다: {path}")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description=__doc__)
    ap.add_argument("inputs", nargs="+", help="병합할 JSON 파일들 (없는 파일은 건너뜀)")
    ap.add_argument("--out", help="결과를 쓸 경로")
    ap.add_argument("--print", action="store_true", dest="to_stdout",
                    help="쓰지 않고 stdout 으로 출력")
    ap.add_argument("--strict", action="store_true",
                    help="충돌이 하나라도 있으면 exit 3")
    a = ap.parse_args()
    if bool(a.out) == bool(a.to_stdout):
        print("--out 과 --print 중 정확히 하나가 필요합니다", file=sys.stderr)
        return 1

    loaded, missing = [], []
    for raw in a.inputs:
        p = pathlib.Path(raw).expanduser()
        d = load(p)
        (loaded.append(d) if d is not None else missing.append(str(p)))
    for m in missing:
        print(f"건너뜀 (파일 없음): {m}", file=sys.stderr)
    if not loaded:
        print("병합할 입력이 없습니다", file=sys.stderr)
        return 1

    conflicts: list[str] = []
    merged = merge_dicts(loaded, conflicts)
    for c in conflicts:
        print(c, file=sys.stderr)

    text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    if a.to_stdout:
        sys.stdout.write(text)
    else:
        out = pathlib.Path(a.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"merged {len(loaded)} file(s) → {out}"
              + (f" ({len(conflicts)} conflict(s))" if conflicts else ""), file=sys.stderr)
    return 3 if (a.strict and conflicts) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            sys.exit(2)
        raise
