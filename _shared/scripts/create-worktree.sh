#!/usr/bin/env bash
# create-worktree.sh — Create a git worktree. Wiring is delegated, terminal setup is not here.
# Git worktree helper for the PJ family.
#
# WHAT THIS OWNS: git. Branch derivation, collision checks, `git worktree add`, and the result
# block. WHAT IT DOES NOT: making the checkout usable by an agent session — that is
# wire-session-root.py, which this script calls when --link-paths/--copy-paths/--env-copy are
# given. The flags and their meaning are unchanged for every caller.
#
# The split exists because the wiring job has a SECOND target that git knows nothing about: a
# task spanning several repos puts its worktrees under one parent, and the session stands in
# that parent needing the same config assembled from all of them. That is this job with more
# than one repo, not a different job.
#
# TWO LAYOUTS, and the default is the legacy one
# ----------------------------------------------
# Without --root the worktree lands at {main repo}/.worktrees/{name} — unchanged, because
# four skill families document that path. Pass --root to place it outside the repo instead:
#
#     {root}/{name}/{wt-folder}
#
# which is what the pj family uses (root $PJ_WORKTREE_ROOT). The extra {wt-folder}
# level is not decoration: one task may need a worktree from SEVERAL repos side by side, and
# then {root}/{name}/ is their shared parent. A single-repo task is that same shape with one
# child, so there is one path convention rather than two.
#
# TWO BRANCH MODES
# ----------------
# Default: cut a new branch off --base (and refuse if it exists — a collision is a real
# conflict, not something to auto-suffix past). With --attach <branch>: check out a branch
# that already exists, which is how a board attaches to its project's long-lived branch.
#
# Usage:
#   ./create-worktree.sh \
#     --ticket TASK-123 \
#     --name prerequisite-computed-block \
#     --prefix refactor \
#     --base master \
#     --env-copy "apps/main-web/.env.development.local" \
#     --link-paths ".claude/skills,.claude/settings.local.json" \
#     --copy-paths "some/config"
#
#   ./create-worktree.sh --name ctx-compact --repo $PJ_REPOS_ROOT/example-backend \
#     --root $PJ_WORKTREE_ROOT --wt-folder backend --attach feat/context-compaction
#
# Exit codes:
#   0  Success
#   1  Bad usage / missing args
#   2  Required command (git) not found
#   3  Not inside a git repo (and no usable --repo)
#   4  Worktree dir or branch already exists (name collision)
#   5  --attach branch does not exist, or is already checked out in another worktree

set -euo pipefail

# Resolved BEFORE any cd: this script changes directory into the repo, and a relative
# $BASH_SOURCE stops resolving the moment it does.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WIRE="$SCRIPT_DIR/wire-session-root.py"

# ---------- Arg parsing ----------
TICKET=""
NAME=""
PREFIX="feature"
BASE="master"
ENV_COPY=""
LINK_PATHS=""
COPY_PATHS=""
DRY_RUN=0
ROOT=""
WT_FOLDER=""
REPO=""
ATTACH=""

usage() {
  cat <<EOF
Usage: $0 --name <slug> [options]

Required:
  --name          English slug, kebab-case (e.g. prerequisite-computed-block)

Optional:
  --ticket        Jira ticket key (e.g. TASK-123). When provided, branch becomes {prefix}/{TICKET}-{slug}.
  --prefix        Branch prefix (feature|refactor|fix|chore|... default: feature)
  --base          Base branch (default: master)
  --attach        Check out this EXISTING branch instead of creating one (--prefix/--ticket/--base ignored)
  --repo          Repo to create the worktree from (default: the repo containing cwd)
  --root          Place the worktree at {root}/{name}/{wt-folder} instead of {repo}/.worktrees/{name}
  --wt-folder     Child folder name under {root}/{name}/ (default: the repo's folder name)
  --env-copy      Repo-relative path to env file to copy from main repo to worktree
  --link-paths    Comma-separated repo-relative paths to symlink from main repo into worktree
  --copy-paths    Comma-separated repo-relative paths to copy from main repo to worktree (preserves symlinks)
  --dry-run       Print commands without executing
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ticket)       TICKET="$2"; shift 2 ;;
    --name)         NAME="$2"; shift 2 ;;
    --prefix)       PREFIX="$2"; shift 2 ;;
    --base)         BASE="$2"; shift 2 ;;
    --env-copy)     ENV_COPY="$2"; shift 2 ;;
    --link-paths)   LINK_PATHS="$2"; shift 2 ;;
    --copy-paths)   COPY_PATHS="$2"; shift 2 ;;
    --root)         ROOT="$2"; shift 2 ;;
    --wt-folder)    WT_FOLDER="$2"; shift 2 ;;
    --repo)         REPO="$2"; shift 2 ;;
    --attach)       ATTACH="$2"; shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

# ---------- Validation ----------
if [[ -z "$NAME" ]]; then
  echo "Missing required arg: --name" >&2
  usage
  exit 1
fi

if [[ ! "$NAME" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "Error: --name must be a kebab-case slug: '$NAME'" >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "Error: 'git' not found" >&2
  exit 2
fi

# --repo lets a caller that is NOT standing in the repo (a pj board parent folder, the vault)
# name it explicitly. Without it we resolve the repo from cwd, exactly as before.
if [[ -n "$REPO" ]]; then
  if [[ ! -d "$REPO" ]]; then
    echo "Error: --repo is not a directory: $REPO" >&2
    exit 3
  fi
  cd "$REPO"
fi

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "Error: not inside a git repo${REPO:+ (--repo $REPO)}" >&2
  exit 3
fi
GIT_COMMON_DIR=$(git rev-parse --path-format=absolute --git-common-dir)
MAIN_REPO="${GIT_COMMON_DIR%/.git}"
cd "$MAIN_REPO"

# ---------- Derived values ----------
if [[ -n "$ATTACH" ]]; then
  BRANCH="$ATTACH"
elif [[ -n "$TICKET" ]]; then
  BRANCH="${PREFIX}/${TICKET}-${NAME}"
else
  BRANCH="${PREFIX}/${NAME}"
fi

if [[ -n "$ROOT" ]]; then
  # External layout: {root}/{name}/{wt-folder}. The wt-folder level is what lets several
  # repos share one {root}/{name}/ parent.
  ROOT="${ROOT/#\~/$HOME}"
  [[ "$ROOT" = /* ]] || ROOT="$(pwd)/$ROOT"
  WORKTREE_ABS="$ROOT/$NAME/${WT_FOLDER:-$(basename "$MAIN_REPO")}"
  WORKTREE_DIR="$WORKTREE_ABS"
else
  WORKTREE_DIR=".worktrees/${NAME}"
  WORKTREE_ABS="$MAIN_REPO/$WORKTREE_DIR"
fi

# ---------- Collision checks ----------
if [[ -e "$WORKTREE_ABS" ]]; then
  echo "Error: worktree directory already exists: $WORKTREE_ABS" >&2
  echo "       Use a different --name." >&2
  exit 4
fi

if [[ -n "$ATTACH" ]]; then
  # Attach mode: the branch MUST exist, and must not be checked out elsewhere — git would
  # refuse the second checkout anyway, but its message does not say which worktree holds it.
  if ! git show-ref --verify --quiet "refs/heads/$ATTACH"; then
    echo "Error: --attach branch does not exist: $ATTACH" >&2
    exit 5
  fi
  HOLDER=$(git worktree list --porcelain \
    | awk -v b="refs/heads/$ATTACH" '/^worktree /{p=$2} /^branch /{if ($2==b) print p}')
  if [[ -n "$HOLDER" ]]; then
    echo "Error: branch $ATTACH is already checked out in: $HOLDER" >&2
    exit 5
  fi
elif git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "Error: branch already exists: $BRANCH" >&2
  echo "       Use --attach to check it out instead of creating it." >&2
  exit 4
fi

# ---------- Execution ----------
run() {
  if (( DRY_RUN )); then
    echo "[dry-run] $*"
  else
    echo "$ $*"
    "$@"
  fi
}

# 1) Create git worktree
if [[ -n "$ROOT" ]] && (( ! DRY_RUN )); then
  mkdir -p "$(dirname "$WORKTREE_ABS")"
fi
if [[ -n "$ATTACH" ]]; then
  run git worktree add "$WORKTREE_ABS" "$ATTACH"
else
  run git worktree add -b "$BRANCH" "$WORKTREE_ABS" "$BASE"
fi

# 2) Wire the new worktree as a session root — delegated, never duplicated.
#
# Linking and copying used to live here. They do not any more, because the SAME job has a
# second target: a task spanning several repos puts its worktrees under one parent and the
# session stands in that parent, which needs the same runtime config assembled from all of
# them. Two implementations of one job is how the two drift, so wire-session-root.py owns it
# and this script owns git.
#
# Keep explicit paths and worktree wiring consistent across PJ callers; one repo is the degenerate
# case there, and it produces the same plain symlinks it always did.
if [[ -n "$LINK_PATHS" || -n "$COPY_PATHS" || -n "$ENV_COPY" ]]; then
  wire_args=(--target "$WORKTREE_ABS" --repo "$MAIN_REPO")
  [[ -n "$LINK_PATHS" ]] && wire_args+=(--link-paths "$LINK_PATHS")
  [[ -n "$COPY_PATHS" ]] && wire_args+=(--copy-paths "$COPY_PATHS")
  [[ -n "$ENV_COPY" ]]   && wire_args+=(--env-copy "$ENV_COPY")
  (( DRY_RUN )) && wire_args+=(--dry-run)

  if (( DRY_RUN )); then
    echo "[dry-run] $WIRE ${wire_args[*]}"
  else
    # A wiring conflict (exit 3) is reported, not fatal: the worktree exists and is usable,
    # and the caller has to see which entry is missing rather than lose the whole run.
    "$WIRE" "${wire_args[@]}" || {
      rc=$?
      [[ $rc -eq 3 ]] || { echo "Error: 배선 실패 (exit $rc)" >&2; exit $rc; }
    }
  fi
fi

# ---------- Result ----------
echo
echo "---WORKTREE_RESULT---"
echo "BRANCH=$BRANCH"
echo "WORKTREE_ABS=$WORKTREE_ABS"
echo "MAIN_REPO=$MAIN_REPO"
echo "REPO=$(basename "$MAIN_REPO")"
echo "MODE=$([[ -n "$ATTACH" ]] && echo attach || echo create)"
echo "LAYOUT=$([[ -n "$ROOT" ]] && echo external || echo nested)"
echo "---END_WORKTREE_RESULT---"
