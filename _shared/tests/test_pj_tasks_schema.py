"""pj-tasks.py list schema — the multi-repo widening must be invisible to every existing line.

The gate this file exists for is the round-trip: parse the LIVE vault lists and re-render them,
byte for byte. 24 projects and their tasks are live records that other sessions are reading right
now, so a rendering change is a silent corruption, not a cosmetic one.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pj-tasks.py"
SPEC = importlib.util.spec_from_file_location("pj_tasks_schema", SCRIPT)
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def reparse_and_render(path: pathlib.Path, sections, key, header) -> tuple[str, str]:
    """Parse the file, re-compose every line from its own machine comment, re-render."""
    text = path.read_text(encoding="utf-8")
    _, items = M.parse(text, sections, key)
    compose = M.compose_proj if key == "project" else M.compose_task
    for rows in items.values():
        for r in rows:
            r["line"] = compose(r["meta"], M.title_of(r["line"]))
    return text, M.render(sections, items, header)


# ---------------------------------------------------------------- live round-trip


WIKILINK_RE = __import__("re").compile(r"\[\[[^\]]+\]\]")


def strip_wikilink(line: str) -> str:
    """The visible half is documented as hand-editable, and four live projects were moved to
    `raw/tasks/OUTDATED/` in Obsidian — which rewrote their wikilink target. The composer
    regenerates the canonical `[[{project}/project]]`, so that one token is excluded here
    rather than pretending the divergence does not exist."""
    return WIKILINK_RE.sub("[[LINK]]", line)


@pytest.mark.skipif(not M.INDEX.is_file(), reason="live vault index.md not present")
def test_live_index_machine_comments_round_trip_byte_for_byte():
    """The machine comment IS the record — a change here is a silent corruption."""
    before, after = reparse_and_render(M.INDEX, M.PROJ_SECTIONS, "project", M.INDEX_HEADER)
    import re
    assert re.findall(r"<!--.*?-->", after) == re.findall(r"<!--.*?-->", before)


@pytest.mark.skipif(not M.INDEX.is_file(), reason="live vault index.md not present")
def test_live_index_lines_round_trip_apart_from_relocated_wikilinks():
    before, after = reparse_and_render(M.INDEX, M.PROJ_SECTIONS, "project", M.INDEX_HEADER)
    assert [strip_wikilink(l) for l in after.splitlines()] == \
           [strip_wikilink(l) for l in before.splitlines()]


@pytest.mark.skipif(not M.INDEX.is_file(), reason="live vault index.md not present")
def test_every_live_project_tasks_file_round_trips():
    _, projs = M.parse(M.INDEX.read_text(encoding="utf-8"), M.PROJ_SECTIONS, "project")
    names = [it["meta"]["project"] for rows in projs.values() for it in rows]
    assert names, "no projects parsed — the gate would be vacuous"
    checked = 0
    for name in names:
        path = M.tasks_path(name)
        if not path.is_file():
            continue
        before, after = reparse_and_render(path, M.TASK_SECTIONS, "slug",
                                           M.tasks_header(name))
        assert [strip_wikilink(l) for l in after.splitlines()] == \
               [strip_wikilink(l) for l in before.splitlines()], name
        checked += 1
    assert checked, "no tasks.md found — the gate would be vacuous"


# ---------------------------------------------------------------- field shapes


@pytest.mark.parametrize("field,value", [
    ("repo", "example-backend"),
    ("branch", "feat/context-compaction-logic"),
    ("branch", "fix/TASK-836-block-field-count"),
    ("project", "ctx-compact"),
    ("slug", "markdown-image-ocr"),
    ("deps", "icon-token-sync,uom-schema"),
])
def test_existing_single_values_still_validate(field, value):
    assert M.check(field, value) == value


@pytest.mark.parametrize("field,value", [
    ("repo", "example-backend,example-frontend"),
    ("branch", "feat/ctx-be,feat/ctx-fe"),
    ("repos", "example-frontend"),
    ("repos", "example-backend,example-frontend"),
])
def test_multi_repo_values_validate(field, value):
    assert M.check(field, value) == value


@pytest.mark.parametrize("field,value", [
    ("repo", "example-backend example-frontend"),   # whitespace breaks kv parsing
    ("repo", "example-backend,"),                  # trailing separator
    ("repo", ",example-backend"),
    ("repo", "example-backend,,example-frontend"),
    ("branch", "feat/x,"),
    ("branch", "feat/x -->"),                     # would end the html comment early
    ("repos", "../escape"),
])
def test_malformed_values_are_refused(field, value):
    with pytest.raises(M.Fail):
        M.check(field, value)


def test_clean_drops_a_malformed_field_rather_than_trusting_it():
    meta = M.clean({"project": "ok", "repo": "bad repo", "branch": "feat/x"})
    assert "repo" not in meta and meta["branch"] == "feat/x"


# ---------------------------------------------------------------- badges


def test_short_of_one_repo_is_unchanged():
    assert M.short_of("example-backend") == "BE"
    assert M.short_of("example-frontend") == "FE"


def test_short_of_several_repos_joins_them():
    assert M.short_of("example-backend,example-frontend") == "BE+FE"


def test_short_of_an_unknown_repo_falls_back_to_its_key():
    assert M.short_of("some-other-repo") == "some-other-repo"


def test_short_of_empty_is_empty():
    assert M.short_of("") == ""


# ---------------------------------------------------------------- rendering


def test_single_repo_project_line_is_unchanged():
    meta = {"project": "ctx-compact", "repo": "example-backend",
            "branch": "feat/ctx", "surface": "AAAA", "created": "09-08"}
    line = M.compose_proj(meta, "CPQ Context Management")
    assert line.startswith("- `BE` **CPQ Context Management** — [[ctx-compact/project]] · "
                           "`feat/ctx` · 09-08 <!-- ")
    assert "repo=example-backend" in line


def test_multi_repo_project_line_names_each_branch_with_its_repo():
    meta = {"project": "ctx", "repo": "example-backend,example-frontend",
            "branch": "feat/ctx-be,feat/ctx-fe", "surface": "AAAA", "created": "09-11"}
    line = M.compose_proj(meta, "결합 작업")
    assert line.startswith("- `BE+FE` ")
    assert "`BE feat/ctx-be` · `FE feat/ctx-fe`" in line


def test_single_repo_task_line_is_unchanged():
    meta = {"slug": "button-variants", "created": "08-04", "deps": "icon-token-sync"}
    line = M.compose_task(meta, "버튼 variant 정리")
    assert line == ("- **버튼 variant 정리** — [[button-variants]] · 선행: icon-token-sync"
                    " · 08-04 <!-- slug=button-variants created=08-04 deps=icon-token-sync -->")


def test_task_line_shows_its_repo_subset_when_it_has_one():
    meta = {"slug": "fe-only", "repos": "example-frontend", "created": "09-11"}
    line = M.compose_task(meta, "FE 쪽만")
    assert "`FE`" in line
    assert "repos=example-frontend" in line


def test_task_line_without_repos_shows_no_badge():
    line = M.compose_task({"slug": "plain", "created": "09-11"}, "전체")
    assert "`" not in line.split("<!--")[0].replace("[[plain]]", "")


def test_a_rendered_line_reparses_to_the_same_meta():
    meta = {"project": "ctx", "repo": "example-backend,example-frontend",
            "branch": "feat/ctx-be,feat/ctx-fe", "surface": "AAAA", "created": "09-11"}
    line = M.compose_proj(meta, "결합")
    _, items = M.parse(f"## 진행 중\n{line}\n", M.PROJ_SECTIONS, "project")
    assert items["진행 중"][0]["meta"] == meta
