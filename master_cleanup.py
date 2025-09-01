"""
Cleanup script for devicetype-library-master.

Behaviors:
 - Remove files that start with '.' (hidden files).
 - Remove directories that start with '.' (hidden folders).
 - Remove files named LICENSE / README (common variants).
 - Remove any 'tests' directory and its contents.
By default runs a dry-run and prints what would be removed.
Use --yes to actually perform deletions.

Usage (examples):
  python master_cleanup.py
  python master_cleanup.py --yes
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
from pathlib import Path
from typing import List, Tuple

LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

LICENSE_PATTERNS = {"license", "license.txt", "license.md", "license.rst"}
README_PREFIXES = {"readme", "readme.md", "readme.txt", "readme.rst"}
REQUIREMENTS_FILENAMES = {"requirements.txt"}
CONTRIBUTING_FILENAMES = {"contributing", "contributing.md", "contributing.txt"}


def should_remove_file(name: str) -> bool:
    """Decide whether a filename matches removal patterns."""
    lower_name = name.lower()
    if lower_name.startswith("."):
        return True
    if lower_name in LICENSE_PATTERNS:
        return True
    if lower_name in REQUIREMENTS_FILENAMES:
        return True
    if lower_name in CONTRIBUTING_FILENAMES:
        return True
    # remove filenames that start with readme (covers README, README.md, etc.)
    for prefix in README_PREFIXES:
        if lower_name == prefix or lower_name.startswith(prefix + "."):
            return True
    return False


def collect_targets(root: Path) -> Tuple[List[Path], List[Path]]:
    """Walk the tree and collect files and directories to remove.

    Returns a tuple of (files_to_remove, dirs_to_remove). Directories are
    sorted so children appear before parents.
    """
    files_to_remove: List[Path] = []
    dirs_to_remove: List[Path] = []

    # os.walk accepts a string path reliably across versions
    for dirpath, dirnames, filenames in os.walk(str(root)):
        current = Path(dirpath)
        # Check directories (modify dirnames in-place to avoid descending into removed dirs)
        keep_dirs: List[str] = []
        for directory in list(dirnames):
            lower_dir = directory.lower()
            if (
                directory.startswith(".")
                or lower_dir in ("tests", "scripts", "schema")
                or lower_dir.endswith("-images")
            ):
                dirs_to_remove.append(current / directory)
                # do not descend into this directory
            else:
                keep_dirs.append(directory)
        # mutate dirnames to control os.walk descent
        dirnames[:] = keep_dirs

        # Check files
        for filename in filenames:
            if should_remove_file(filename):
                files_to_remove.append(current / filename)

    # Sort directories by path length descending so children removed before parents
    dirs_to_remove.sort(key=lambda path: len(str(path)), reverse=True)
    return files_to_remove, dirs_to_remove


def perform_deletions(files: List[Path], dirs: List[Path], dry_run: bool) -> Tuple[int, int]:
    """Remove listed files and directories. Returns counts (files, dirs)."""
    removed_files = 0
    removed_dirs = 0

    for file_path in files:
        if dry_run:
            LOGGER.info("DRY: file: %s", file_path)
        else:
            try:
                file_path.unlink()
                removed_files += 1
                LOGGER.info("removed file: %s", file_path)
            except OSError as error:
                LOGGER.error("error removing file %s - %s", file_path, error)

    for dir_path in dirs:
        if dry_run:
            LOGGER.info("DRY: dir: %s", dir_path)
        else:
            try:
                shutil.rmtree(dir_path)
                removed_dirs += 1
                LOGGER.info("removed dir: %s", dir_path)
            except OSError as error:
                LOGGER.error("error removing dir %s - %s", dir_path, error)

    return removed_files, removed_dirs


def main() -> int:
    """Parse arguments and run the cleanup procedure."""
    parser = argparse.ArgumentParser(
        description="Cleanup devicetype-library-master repository."
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Actually perform deletions. If not provided the script runs a dry-run.",
    )
    args = parser.parse_args()

    # Always operate on the sibling folder "devicetype-library-master"
    script_dir = Path(__file__).resolve().parent
    target = script_dir / "devicetype-library-master"

    if not target.exists() or not target.is_dir():
        LOGGER.error("Target does not exist or is not a directory: %s", target)
        return 2

    # Basic safety: require that target contains something that looks like device-type repo
    marker = any((target / d).exists() for d in ("device-types", "scripts", "schema", "module-types"))
    if not marker:
        LOGGER.warning(
            "Target does not look like devicetype-library-master (no expected marker folders). "
            "Proceeding anyway — double-check the repository folder next to this script."
        )

    LOGGER.info("Collecting items to remove under: %s", target)
    files_to_remove, dirs_to_remove = collect_targets(target)

    LOGGER.info("Found %d files and %d directories that match cleanup rules.",
                len(files_to_remove), len(dirs_to_remove))
    if files_to_remove or dirs_to_remove:
        if not args.yes:
            LOGGER.info(
                "Dry-run mode. No files or directories will be deleted. "
                "Re-run with --yes to apply changes."
            )
        removed_files, removed_dirs = perform_deletions(
            files_to_remove, dirs_to_remove, dry_run=not args.yes
        )
        if args.yes:
            LOGGER.info("Deleted %d files and %d directories.", removed_files, removed_dirs)
        else:
            LOGGER.info(
                "Dry-run listed %d files and %d directories (no changes made).",
                len(files_to_remove),
                len(dirs_to_remove),
            )
    else:
        LOGGER.info("Nothing to remove.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())