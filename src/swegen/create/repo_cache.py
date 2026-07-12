from __future__ import annotations

import logging
import subprocess
from pathlib import Path


class RepoCache:
    """Manages local clones of repositories for CC analysis."""

    def __init__(self, cache_dir: Path | None = None):
        """
        Initialize the repo cache.

        Args:
            cache_dir: Directory to store clones. Defaults to .cache/repos
        """
        self.cache_dir = cache_dir or Path(".cache/repos")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("swegen")

    def get_or_clone(
        self,
        repo: str,
        head_sha: str,
        repo_url: str | None = None,
        base_sha: str | None = None,
    ) -> Path:
        """
        Get cached repo or clone it. Checkout the specified commit.

        Args:
            repo: Repository in "owner/repo" format
            head_sha: Commit SHA to checkout
            repo_url: Optional clone URL (defaults to https://github.com/{repo}.git)
            base_sha: Optional base commit SHA. When given, it is fetched alongside
                head_sha so `git diff base..head` has both ends locally.

        Returns:
            Path to the repository root
        """
        owner, name = self._parse_repo(repo)
        repo_path = self.cache_dir / owner / name

        if repo_url is None:
            repo_url = f"https://github.com/{repo}.git"

        wanted = [sha for sha in (base_sha, head_sha) if sha]

        if repo_path.exists() and (repo_path / ".git").exists():
            self.logger.debug("Using cached repo: %s", repo_path)
            self._fetch_and_checkout(repo_path, head_sha, wanted)
        else:
            self.logger.info("Cloning repo to cache: %s -> %s", repo, repo_path)
            self._clone(repo_url, repo_path, head_sha)

        return repo_path

    def _parse_repo(self, repo: str) -> tuple[str, str]:
        """Parse 'owner/repo' into (owner, repo) tuple."""
        # Handle full URLs
        if repo.startswith("https://"):
            repo = repo.replace("https://github.com/", "").rstrip(".git")
        if repo.startswith("git@"):
            repo = repo.replace("git@github.com:", "").rstrip(".git")

        parts = repo.split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid repo format: {repo}. Expected 'owner/repo'")
        return parts[0], parts[1]

    def _clone(self, repo_url: str, repo_path: Path, head_sha: str) -> None:
        """Clone a repository and checkout the specified commit."""
        repo_path.parent.mkdir(parents=True, exist_ok=True)

        # Partial clone: full commit graph and trees (so `git diff base..head` and any
        # other history walk works), but no file contents up front. Blobs are fetched
        # lazily, on demand -- which for the two commits we actually diff is a handful
        # of files instead of every version of every file ever committed.
        self.logger.debug("Cloning %s (blobless)...", repo_url)
        subprocess.run(
            ["git", "clone", "--filter=blob:none", repo_url, str(repo_path)],
            check=True,
            capture_output=True,
        )

        # Checkout the target commit
        self._checkout(repo_path, head_sha)

    def _fetch_and_checkout(
        self,
        repo_path: Path,
        head_sha: str,
        wanted_shas: list[str] | None = None,
    ) -> None:
        """Fetch the commits we need and checkout the specified one.

        Fetches only the SHAs this task diffs, rather than `--all` (every branch and
        every tag on every remote) which costs real time per PR on large repos. Falls
        back to a full fetch if the targeted one fails -- e.g. a force-pushed SHA the
        remote will no longer serve directly.
        """
        shas = wanted_shas or [head_sha]
        self.logger.debug("Fetching %s for %s...", ", ".join(s[:8] for s in shas), repo_path)

        fetched = subprocess.run(
            ["git", "fetch", "origin", *shas],
            cwd=str(repo_path),
            check=False,
            capture_output=True,
        )
        if fetched.returncode != 0:
            self.logger.debug(
                "Targeted fetch failed (%s); falling back to full fetch",
                fetched.stderr.decode().strip() if fetched.stderr else "unknown",
            )
            subprocess.run(
                ["git", "fetch", "--all"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )

        # Try to checkout the commit
        self._checkout(repo_path, head_sha)

    def _clean_repo(self, repo_path: Path) -> None:
        """Thoroughly clean the repository, including submodules."""
        # Deinit all submodules to remove their contents
        subprocess.run(
            ["git", "submodule", "deinit", "--all", "-f"],
            cwd=str(repo_path),
            capture_output=True,  # Don't check - might fail if no submodules
        )
        # Reset any tracked changes
        subprocess.run(
            ["git", "reset", "--hard"],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
        )
        # Clean untracked files, including nested git repos (-ff) and ignored files (-x)
        subprocess.run(
            ["git", "clean", "-ffdx"],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
        )

    def _checkout(self, repo_path: Path, sha: str) -> None:
        """Checkout a specific commit, fetching if needed."""
        try:
            # First, thoroughly clean the repo
            self._clean_repo(repo_path)

            # Try direct checkout
            subprocess.run(
                ["git", "checkout", sha],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
            )
            self.logger.debug("Checked out %s", sha[:8])
        except subprocess.CalledProcessError as e:
            # Commit not available, fetch it specifically
            self.logger.debug(
                "Commit %s not found, fetching... (stderr: %s)",
                sha[:8],
                e.stderr.decode() if e.stderr else "",
            )
            try:
                subprocess.run(
                    ["git", "fetch", "origin", sha],
                    cwd=str(repo_path),
                    check=True,
                    capture_output=True,
                )
                # Clean again before checkout to ensure no untracked files
                self._clean_repo(repo_path)
                subprocess.run(
                    ["git", "checkout", sha],
                    cwd=str(repo_path),
                    check=True,
                    capture_output=True,
                )
                self.logger.debug("Fetched and checked out %s", sha[:8])
            except subprocess.CalledProcessError as fetch_err:
                # Provide more context in the error
                stderr = fetch_err.stderr.decode() if fetch_err.stderr else ""
                self.logger.error("Failed to checkout %s: %s", sha[:8], stderr)
                raise RuntimeError(
                    f"Cannot checkout commit {sha[:8]}. It may have been force-pushed or deleted. Error: {stderr}"
                ) from fetch_err

        # Update submodules if any
        try:
            subprocess.run(
                ["git", "submodule", "update", "--init", "--recursive"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            self.logger.debug("Submodule update skipped or failed (non-fatal)")
