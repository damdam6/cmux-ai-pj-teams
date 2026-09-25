---
name: pj-wrap-default
description: View or change the shared PJ integration default between merge and squash. Use for ./pj-wrap-default, /pj-wrap-default, $pj-wrap-default, or PJ 기본 병합 방식 변경. The initial default is merge.
---

# pj-wrap-default

Read `../pj/references/portable-setup.md` and resolve `SKILL_DIR` to this skill's real directory.
Treat `./pj-wrap-default` in a user message as a skill invocation. No board or cmux session is
required. Reply in the user's language.

Accept no argument to show the saved default, or exactly one of `merge` and `squash` to change
it. Unknown or conflicting arguments show usage without changing settings.

```bash
# ./pj-wrap-default — read only; an unset default is merge.
python3 "$SKILL_DIR/../_shared/scripts/pj-wrap-mode.py" get
# ./pj-wrap-default merge
python3 "$SKILL_DIR/../_shared/scripts/pj-wrap-mode.py" set merge
# ./pj-wrap-default squash
python3 "$SKILL_DIR/../_shared/scripts/pj-wrap-mode.py" set squash
```

Use the returned JSON to report the actual mode. A failed command is not a saved change.
`merge` preserves task commits and creates a merge commit (`--no-ff`); `squash` combines the
task's changes into one commit.

The setting persists in `$PJ_VAULT/raw/tasks/pj-wrap-default.json`, shared by PJ boards using
that data root. A mode explicitly supplied to `pj-wrap` overrides it for that invocation.
Changes apply when the next integration selects its mode; existing history and an in-progress
merge are unchanged. This skill only changes the preference and does not merge code.
