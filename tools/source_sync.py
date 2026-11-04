#!/usr/bin/env python3
"""Mirror a source repository checkout into a mirror checkout.

The mirror is a source-code copy of the upstream project, but it deliberately
does not inherit any upstream GitHub automation.  The mirror owns its own
``.github/workflows/mirror.yml``, ``mirror.config.json`` and generated
``index.json``; all other source files are copied.

If the source has no README, a small README is generated so the mirror always
has a landing page and release metadata target.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Dict, Iterable, Set

MANAGED = {
    ".github/workflows/mirror.yml",
    "index.json",
    "mirror-report.json",
    "mirror.config.json",
}
SKIP_TARGET_DIRS = {".git", ".packager", ".source", ".mirror-assets"}
SKIP_SOURCE_DIRS = {".git", ".github", ".mirror-assets"}
README_PREFIX = "readme."


def _posix(path: Path) -> str:
    return path.as_posix()


def _source_files(source: Path) -> Dict[str, Path]:
    files: Dict[str, Path] = {}
    for root, dirs, names in os.walk(source):
        dirs[:] = [name for name in dirs if name not in SKIP_SOURCE_DIRS]
        root_path = Path(root)
        for name in names:
            full = root_path / name
            relative = full.relative_to(source)
            if any(part in SKIP_SOURCE_DIRS for part in relative.parts):
                continue
            key = _posix(relative)
            if key in MANAGED:
                continue
            files[key] = full
    return files


def _is_readme(path: str) -> bool:
    name = Path(path).name.casefold()
    return "/" not in path and name.startswith(README_PREFIX)


def _fallback_readme(source_repo: str) -> str:
    source = source_repo or "(unknown source)"
    return (
        "# Portable flashing packages\n\n"
        f"Source repository: https://github.com/{source}\n\n"
        "This mirror contains the source repository files and portable flashing "
        "packages for Windows, Linux and macOS. Original release ZIPs are kept "
        "alongside the portable packages in Releases.\n"
    )


def sync_source(source: Path, target: Path, source_repo: str = "") -> Dict[str, object]:
    """Copy source files into target and remove stale non-managed files."""

    if not source.is_dir():
        raise ValueError(f"source directory does not exist: {source}")
    if not target.is_dir():
        raise ValueError(f"target directory does not exist: {target}")

    files = _source_files(source)
    source_readmes = [path for path in files if _is_readme(path)]
    source_keys: Set[str] = set(files)
    removed = 0

    # Remove stale files before copying.  This also handles case-insensitive
    # filesystems where an old README.md and a source readme.md collide.
    for root, dirs, names in os.walk(target):
        dirs[:] = [name for name in dirs if name not in SKIP_TARGET_DIRS]
        root_path = Path(root)
        for name in names:
            full = root_path / name
            relative = _posix(full.relative_to(target))
            if relative in MANAGED or relative in source_keys:
                continue
            full.unlink()
            removed += 1

    copied = 0
    for relative, full in files.items():
        destination = target / relative
        if destination.exists() and destination.is_dir():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(full, destination)
        copied += 1

    generated_readme = False
    if not source_readmes:
        readme = target / "README.md"
        if not readme.exists():
            readme.write_text(_fallback_readme(source_repo), encoding="utf-8")
            generated_readme = True

    return {
        "copied": copied,
        "removed": removed,
        "readme": source_readmes,
        "generated_readme": generated_readme,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy source repository files into a mirror checkout."
    )
    parser.add_argument("--source", required=True, help="checked-out source repository")
    parser.add_argument("--target", required=True, help="checked-out mirror repository")
    parser.add_argument("--source-repo", default="", help="owner/repo for a generated README")
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = sync_source(Path(args.source), Path(args.target), args.source_repo)
    print(
        "source sync: copied={copied} removed={removed} "
        "readme={readme} generated_readme={generated_readme}".format(**result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
