"""All package tests use isolated configuration; no user registry or task data."""
import os
from pathlib import Path
import tempfile
import atexit

_scratch = tempfile.TemporaryDirectory(prefix="pj-package-tests-")
atexit.register(_scratch.cleanup)
os.environ["PJ_VAULT"] = str(Path(_scratch.name) / "data")
os.environ["PJ_REPOS_ROOT"] = str(Path(_scratch.name) / "repos")
os.environ["PJ_WORKTREE_ROOT"] = str(Path(_scratch.name) / "worktrees")
os.environ["PJ_CMUX_REGISTRY_ROOT"] = str(Path(_scratch.name) / "registry")
os.environ["PJ_ALIASES"] = str(Path(__file__).parent / "_shared/tests/fixtures/repo-aliases.json")
