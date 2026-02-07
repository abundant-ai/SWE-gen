from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .utils import _is_relevant_source, strip_tests_prefix


def get_merge_base(repo_path: Path, base_sha: str, head_sha: str) -> str:
    """
    Find the merge-base (common ancestor) between base and head.

    GitHub API's base.sha returns the current tip of the target branch,
    not the actual fork point. This causes noisy patches when main has
    moved forward since the PR was created.

    Args:
        repo_path: Path to the git repository
        base_sha: Base commit SHA from GitHub API (tip of target branch)
        head_sha: Head commit SHA (PR branch head)

    Returns:
        The merge-base commit SHA
    """
    logger = logging.getLogger("swegen")
    result = subprocess.run(
        ["git", "merge-base", base_sha, head_sha],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
        text=True,
    )
    merge_base = result.stdout.strip()
    if merge_base != base_sha:
        logger.debug("Using merge-base %s instead of base_sha %s", merge_base, base_sha)
    return merge_base


def generate_diffs(
    repo_path: Path,
    base_sha: str,
    head_sha: str,
    test_file_paths: list[str],
) -> tuple[str, str]:
    """
    Generate fix.patch and bug.patch from a repository.

    Reversed Baseline Strategy:
    - fix.patch: base→head, SOURCE files only (what oracle applies to fix)
    - bug.patch: head→base, ALL files (reverts everything to BASE state)

    Args:
        repo_path: Path to the git repository
        base_sha: Base commit SHA (pre-PR state)
        head_sha: Head commit SHA (post-PR state with fix)
        test_file_paths: List of test file paths (for logging only)

    Returns:
        Tuple of (solution_diff, bug_diff)
    """
    logger = logging.getLogger("swegen")

    # Use merge-base to get actual fork point (fixes noisy patches)
    base_sha = get_merge_base(repo_path, base_sha, head_sha)

    # Get all changed files
    result = subprocess.run(
        ["git", "diff", "--name-only", base_sha, head_sha],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
        text=True,
    )
    all_changed = [f for f in result.stdout.strip().split("\n") if f]

    # Filter for source files (exclude tests and CI; includes docs, examples, configs)
    source_files = [f for f in all_changed if _is_relevant_source(f)]

    logger.debug("Total changed files: %d", len(all_changed))
    logger.debug("Relevant source files (for fix.patch): %d", len(source_files))
    logger.debug("Test files (included in bug.patch): %s", test_file_paths)

    # Generate fix.patch (base → head, source only)
    # This is what the oracle applies to fix the bug
    logger.debug("Generating fix.patch (base → head, source only)...")
    if source_files:
        result = subprocess.run(
            ["git", "diff", base_sha, head_sha, "--"] + source_files,
            cwd=str(repo_path),
            check=True,
            capture_output=True,
            text=True,
        )
        solution_diff = result.stdout
    else:
        logger.warning("No source files changed! fix.patch will be empty.")
        solution_diff = ""

    # Generate bug.patch (head → base, ALL files)
    # This reverts everything so agent sees BASE state
    logger.debug("Generating bug.patch (head → base, ALL files)...")
    result = subprocess.run(
        ["git", "diff", head_sha, base_sha],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
        text=True,
    )
    bug_diff = result.stdout

    return solution_diff, bug_diff


def extract_test_files(
    repo_path: Path,
    test_file_paths: list[str],
    head_sha: str,
    output_dir: Path,
) -> list[str]:
    """
    Extract test files from HEAD commit to task/tests/ directory.

    These files will be copied into the container at verification time,
    overwriting the BASE state test files (after bug.patch is applied).

    Args:
        repo_path: Path to the git repository
        test_file_paths: List of repo-relative test file paths
        head_sha: Commit SHA to extract files from
        output_dir: Task output directory (tests/ subdir will be used)

    Returns:
        List of successfully extracted test file paths (repo-relative)
    """
    logger = logging.getLogger("swegen")
    logger.debug("Extracting test files from HEAD commit...")

    # Create tests directory in task output
    test_dir = output_dir / "tests"
    test_dir.mkdir(exist_ok=True, parents=True)
    test_dir = test_dir.resolve()

    if not test_file_paths:
        logger.warning("No test files found in PR!")
        return []

    extracted = []
    for test_file_path in test_file_paths:
        try:
            # Extract file content directly from HEAD commit
            content = subprocess.run(
                ["git", "show", f"{head_sha}:{test_file_path}"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            ).stdout

            # Preserve directory structure under tests/
            # Strip leading "tests/" since we're already putting in tests dir
            relative_path = Path(strip_tests_prefix(test_file_path))

            dest_path = test_dir / relative_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(content)

            logger.debug("Extracted test file: %s -> %s", test_file_path, dest_path)
            extracted.append(test_file_path)

        except subprocess.CalledProcessError:
            logger.warning("Test file not found in HEAD: %s", test_file_path)
            continue

    logger.debug("Extracted %d/%d test files to %s", len(extracted), len(test_file_paths), test_dir)
    return extracted
