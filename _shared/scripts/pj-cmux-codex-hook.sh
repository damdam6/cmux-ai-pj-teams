#!/bin/sh
# codex PostToolUse → pj-cmux relay. Codex's hooks.command is not guaranteed to accept a
# command with arguments, so this wrapper pins the runtime. Synchronous on purpose — the
# exact tool-call/request binding forbids detaching (see pj/references/cmux-orchestration.md);
# a request whose relay dies stays visibly pending and is retried explicitly.
exec /usr/bin/env python3 "$(dirname "$0")/pj-cmux.py" hook run --runtime codex
