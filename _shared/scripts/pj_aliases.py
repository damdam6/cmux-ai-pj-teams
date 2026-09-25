"""Locked, atomic updates of private repository aliases and cached policy data."""
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid


def regular(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Alias settings and locks must be regular files without hard links")


def read_aliases(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, encoding="utf-8") as handle:
        regular(handle.fileno())
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("aliases", {}), dict):
        raise ValueError("Alias settings must contain an aliases object")
    return data


def update_alias(path, key, update):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT
                   | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(lock, "r+") as handle:
        regular(handle.fileno())
        fcntl.flock(handle, fcntl.LOCK_EX)
        data = read_aliases(path)
        aliases = data.setdefault("aliases", {})
        entry = aliases.get(key, {})
        if isinstance(entry, str):
            entry = {"short": entry}
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid repository alias: {key}")
        aliases[key] = update(dict(entry))
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return aliases[key]
