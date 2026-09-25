#!/usr/bin/env python3
"""Read or set the shared PJ integration mode; no Git or cmux operations."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from pj_config import load_config

FILENAME = "pj-wrap-default.json"
MODES = ("merge", "squash")


@contextlib.contextmanager
def settings_dir(vault, create=False):
    vault = Path(vault).resolve()
    if create:
        vault.mkdir(parents=True, exist_ok=True)
    handles = []
    try:
        try:
            fd = os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.append(fd)
            for name in ("raw", "tasks"):
                if create:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                handles.append(fd)
        except FileNotFoundError:
            if create:
                raise
            yield None
            return
        yield fd
    finally:
        for fd in reversed(handles):
            os.close(fd)


def regular_file(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("PJ wrap settings and locks must be regular files without hard links")


def read_settings(directory):
    if directory is None:
        return {}, False
    try:
        fd = os.open(FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return {}, False
    with os.fdopen(fd, encoding="utf-8") as handle:
        regular_file(handle.fileno())
        data = json.load(handle)
    if not isinstance(data, dict) or data.get("mode") not in MODES:
        raise ValueError("Invalid PJ wrap settings: mode must be merge or squash")
    return data, True


def get_mode(vault):
    with settings_dir(vault) as directory:
        data, saved = read_settings(directory)
    return {"mode": data.get("mode", "merge"), "source": "saved" if saved else "builtin"}


def set_mode(vault, mode):
    if mode not in MODES:
        raise ValueError("mode must be merge or squash")
    with settings_dir(vault, create=True) as directory:
        lock = os.open("pj-wrap-default.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                       | os.O_NONBLOCK, 0o600, dir_fd=directory)
        with os.fdopen(lock, "r+") as handle:
            regular_file(handle.fileno())
            fcntl.flock(handle, fcntl.LOCK_EX)
            data, _ = read_settings(directory)
            data["mode"] = mode
            temp = f".{FILENAME}.{uuid.uuid4().hex}.tmp"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(data, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, FILENAME, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temp, dir_fd=directory)
                except FileNotFoundError:
                    pass
    return {"mode": mode, "source": "saved"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("get")
    sub.add_parser("set").add_argument("mode", choices=MODES)
    args = parser.parse_args()
    try:
        vault = load_config()["PJ_VAULT"]
        result = set_mode(vault, args.mode) if args.command == "set" else get_mode(vault)
        print(json.dumps(result))
    except (OSError, ValueError) as exc:
        print(f"PJ_WRAP_MODE=error {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
