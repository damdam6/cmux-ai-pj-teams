#!/usr/bin/env python3
"""Link the complete portable PJ set into a consumer project's runtime skill directories."""
import argparse
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parents[2]


def install(project, dry_run=False, skip_conflicts=False):
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"Project directory does not exist: {project}")
    sources = sorted(p for p in PACKAGE.iterdir() if (p / "SKILL.md").is_file())
    sources.append(PACKAGE / "_shared")
    pending, conflicts = [], []
    for runtime in (".claude", ".codex"):
        for source in sources:
            dest = project / runtime / "skills" / source.name
            if dest.is_symlink() and dest.resolve() == source.resolve():
                print(f"already-linked: {dest}")
            elif dest.exists() or dest.is_symlink():
                conflicts.append(dest)
            else:
                pending.append((source, dest))
    if conflicts and not skip_conflicts:
        raise ValueError("Conflicting destinations (nothing changed):\n" + "\n".join(map(str, conflicts)))
    for dest in conflicts:
        print(f"SKIP (exists, not the package link): {dest}")
    for source, dest in pending:
        print(f"{'would-link' if dry_run else 'linked'}: {dest} -> {source}")
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(source, target_is_directory=True)
    return len(pending)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-conflicts", action="store_true",
                        help="keep conflicting entries and link the remaining family")
    args = parser.parse_args()
    try:
        install(args.project, args.dry_run, args.skip_conflicts)
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
