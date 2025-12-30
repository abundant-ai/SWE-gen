from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, Set

import requests
from rich.console import Console

from taskgen.reversal import is_test_file

from .farm_hand import PRCandidate, _slug
from .state import StreamState


def load_skip_list(skip_list_file: Path, repo: str) -> Set[int]:
    """Load PR numbers from a skip list file for the given repository.
    
    The file should contain task IDs like (SWEBench format):
        owner__repo-123
        owner__repo-456
    
    This function extracts PR numbers matching the current repo.
    
    Args:
        skip_list_file: Path to the skip list file
        repo: Repository in owner/repo format (e.g., "python/pillow")
    
    Returns:
        Set of PR numbers to skip
    """
    if not skip_list_file.exists():
        return set()

    # Create expected prefix from repo (e.g., "python/pillow" -> "python__pillow-")
    repo_slug = _slug(repo)
    prefix = f"{repo_slug}-"

    skip_prs: Set[int] = set()
    try:
        content = skip_list_file.read_text()
        for line in content.strip().split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            # Check if this task ID matches our repo
            if line.startswith(prefix):
                # Extract PR number from task ID (e.g., "python__pillow-9272" -> 9272)
                pr_part = line[len(prefix):]
                try:
                    pr_number = int(pr_part)
                    skip_prs.add(pr_number)
                except ValueError:
                    # Ignore malformed entries
                    pass
    except Exception:
        # If file read fails, return empty set
        pass

    return skip_prs


class StreamingPRFetcher:
    """Fetches PRs from GitHub in a streaming fashion.
    
    Yields PRs one at a time after filtering. Handles pagination,
    rate limiting, and various filters (merged, has tests).
    
    Attributes:
        repo: Repository in "owner/repo" format
        console: Rich console for output
        state: StreamState for tracking processed PRs
        min_files: Minimum total files changed (early approximate filter)
        require_tests: Whether PRs must have test file changes
        api_delay: Delay between API calls in seconds
    """

    def __init__(
        self,
        repo: str,
        console: Console,
        state: StreamState,
        min_files: int = 3,
        require_tests: bool = True,
        api_delay: float = 0.5,
    ):
        self.repo = repo
        self.console = console
        self.state = state
        self.min_files = min_files
        self.require_tests = require_tests
        self.api_delay = api_delay

        # GitHub API setup
        self.api_base = "https://api.github.com"
        self.github_token = (
            os.getenv("GITHUB_TOKEN")
            or os.getenv("GH_TOKEN")
            or os.getenv("REPO_CREATION_TOKEN")
        )
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "taskgen-stream-farm",
        }
        if self.github_token:
            self.headers["Authorization"] = f"token {self.github_token}"

    def stream_prs(
        self,
        resume_from_time: Optional[str] = None,
    ) -> Iterator[PRCandidate]:
        """Stream PRs from GitHub API, skipping already processed ones.
        
        Yields PRs one at a time after validation. Fetches in pages
        but yields immediately, allowing processing to happen concurrently.
        
        Works backwards in time from present day (or resume point) by PR creation time.
        
        Args:
            resume_from_time: If specified, only process PRs created before this timestamp.
                             Format: ISO 8601 string (e.g., "2024-01-15T23:59:59.999999+00:00")
                             This allows resuming from a specific time and continuing backwards.
                             
        Yields:
            PRCandidate instances for each PR that passes filters
        """
        yielded = 0
        page = 1

        # Fetch closed PRs sorted by created time descending
        # This gives us all merged PRs in reverse chronological order (by creation)
        params_base = {
            "state": "closed",
            "sort": "created",
            "direction": "desc",
            "per_page": 100,
        }

        self.console.print(f"[dim]Streaming PRs from {self.repo}...[/dim]")
        if resume_from_time is not None:
            resume_dt = datetime.fromisoformat(resume_from_time.replace('Z', '+00:00'))
            self.console.print(
                f"[yellow]Resuming from {resume_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} "
                f"(only processing PRs created before this time)[/yellow]"
            )
        elif self.state.total_processed > 0:
            self.console.print(
                f"[yellow]Resuming: {self.state.total_processed} PRs already processed "
                f"({self.state.successful} successful, {self.state.failed} failed)[/yellow]"
            )
            if self.state.last_created_at:
                last_dt = datetime.fromisoformat(self.state.last_created_at.replace('Z', '+00:00'))
                self.console.print(
                    f"[yellow]Last processed PR created at: {last_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}[/yellow]"
                )

        skipped_stats = {
            "already_processed": 0,
            "in_skip_list": 0,
            "not_merged": 0,
            "too_few_changes": 0,
            "no_tests": 0,
            "api_error": 0,
            "after_resume_time": 0,
        }

        while True:
            # Fetch next page
            url = f"{self.api_base}/repos/{self.repo}/pulls"
            params = {**params_base, "page": page}

            try:
                resp = requests.get(url, headers=self.headers, params=params, timeout=30)
                resp.raise_for_status()
            except requests.exceptions.RequestException as exc:
                self.console.print(f"[red]API error on page {page}: {exc}[/red]")
                skipped_stats["api_error"] += 1
                break

            prs = resp.json()
            if not prs:
                self.console.print("[dim]No more PRs available[/dim]")
                break

            # Check rate limiting
            remaining = int(resp.headers.get("X-RateLimit-Remaining", 999))
            if remaining < 10:
                reset_time = int(resp.headers.get("X-RateLimit-Reset", 0))
                wait_seconds = max(0, reset_time - time.time())
                self.console.print(
                    f"[yellow]Rate limit low ({remaining}), waiting {wait_seconds:.0f}s...[/yellow]"
                )
                time.sleep(wait_seconds + 1)

            # Process PRs from this page
            for pr_data in prs:
                pr_number = pr_data["number"]

                # Filter: must be merged
                merged_at = pr_data.get("merged_at")
                if not merged_at:
                    skipped_stats["not_merged"] += 1
                    continue

                # Get creation time
                created_at = pr_data.get("created_at")

                # Skip if this PR was created after our resume time
                # (we're working backwards, so we only want PRs created before the resume point)
                if resume_from_time is not None and created_at:
                    pr_created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    resume_dt = datetime.fromisoformat(resume_from_time.replace('Z', '+00:00'))
                    if pr_created_dt >= resume_dt:
                        skipped_stats["after_resume_time"] += 1
                        continue

                # Skip if already processed
                if pr_number in self.state.processed_prs:
                    skipped_stats["already_processed"] += 1
                    continue

                # Skip if in external skip list
                if pr_number in self.state.skip_list_prs:
                    skipped_stats["in_skip_list"] += 1
                    continue

                # Fetch full PR details
                try:
                    pr_url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}"
                    pr_resp = requests.get(pr_url, headers=self.headers, timeout=30)
                    pr_resp.raise_for_status()
                    pr_full = pr_resp.json()
                    time.sleep(self.api_delay)
                except requests.exceptions.RequestException:
                    skipped_stats["api_error"] += 1
                    continue

                # Get file change count for metadata
                files_changed = pr_full.get("changed_files", 0)

                # Filter: minimum files changed (early approximate filter to save API calls)
                # Note: This is total files (including tests/docs/CI)
                # The accurate source-only check happens later in the pipeline
                if files_changed < self.min_files:
                    skipped_stats["too_few_changes"] += 1
                    continue

                # Filter: test file changes (if required)
                if self.require_tests:
                    try:
                        has_tests = self._pr_has_test_changes(pr_number)
                        time.sleep(self.api_delay)
                        if not has_tests:
                            skipped_stats["no_tests"] += 1
                            continue
                    except requests.exceptions.RequestException:
                        skipped_stats["api_error"] += 1
                        continue

                # Passed all filters - yield this PR
                candidate = PRCandidate(
                    number=pr_number,
                    title=pr_full.get("title", ""),
                    created_at=pr_full.get("created_at", ""),
                    merged_at=pr_full.get("merged_at", ""),
                    author=pr_full.get("user", {}).get("login", "unknown"),
                    files_changed=files_changed,
                    additions=pr_full.get("additions", 0),
                    deletions=pr_full.get("deletions", 0),
                    url=pr_full.get("html_url", ""),
                )

                self.state.total_fetched += 1
                yielded += 1

                yield candidate

            # Move to next page
            page += 1

            # Break if we got fewer results than expected (last page)
            if len(prs) < 100:
                self.console.print("[dim]Reached last page of PRs[/dim]")
                break

        # Final stats
        self._print_stats(skipped_stats)
        self.console.print(
            f"[green]Stream complete: {yielded} PRs yielded, "
            f"{self.state.total_processed} total processed[/green]"
        )

    def _pr_has_test_changes(self, pr_number: int) -> bool:
        """Check if PR modifies test files.
        
        Args:
            pr_number: PR number to check
            
        Returns:
            True if PR has test file changes
        """
        files_url = f"{self.api_base}/repos/{self.repo}/pulls/{pr_number}/files"
        page = 1

        while True:
            params = {"page": page, "per_page": 100}
            resp = requests.get(files_url, headers=self.headers, params=params, timeout=30)
            resp.raise_for_status()

            files = resp.json()
            if not files:
                break

            for file in files:
                filename = file.get("filename", "")
                # Use centralized test file detection (supports all languages)
                if is_test_file(filename):
                    return True

            if len(files) < 100:
                break
            page += 1

        return False

    def _print_stats(self, skipped: dict) -> None:
        """Print skipping statistics.
        
        Args:
            skipped: Dict of skip reasons to counts
        """
        total_skipped = sum(skipped.values())
        if total_skipped == 0:
            return

        self.console.print("\n[dim]Skipped PRs:[/dim]")
        for reason, count in skipped.items():
            if count > 0:
                self.console.print(f"  [dim]• {reason}: {count}[/dim]")

