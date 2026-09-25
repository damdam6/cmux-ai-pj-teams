#!/usr/bin/env bash
# Refresh the complete package family after merging a multi-repository session root.
set -euo pipefail
if [ "$#" -ne 2 ] || [ "$1" != "--target" ] || [ ! -d "$2" ]; then
  printf 'usage: %s --target <existing directory>\n' "$0" >&2
  exit 1
fi
HERE="$(cd "$(dirname "$0")" && pwd -P)"
exec python3 "$HERE/install.py" --project "$2" --skip-conflicts
