from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from rich.console import Console

from swegen.create import is_test_file

from .farm_hand import PRCandidate, _slug
from .state import StreamState

# A single transient GitHub failure used to end the whole run: the page fetch gave up on
# the first error. At this call volume a 5xx or a dropped connection is routine, so pages
# are retried with backoff and only a persistent failure stops the stream.
_MAX_PAGE_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 2.0
_MAX_BACKOFF_SECONDS = 60.0

# A rate limit is not a failure - it is GitHub telling us exactly when to come back - so a
# wait does not consume a retry attempt. GitHub's primary limit resets on the hour, so the
# ceiling is generous enough to sit one out (the proactive check further down already
# sleeps to the reset uncapped). The number of waits is bounded instead, so a bogus header
# cannot park the farm forever.
_MAX_RATE_LIMIT_WAIT_SECONDS = 3600.0
_MAX_RATE_LIMIT_WAITS = 3

# Worth another try: server-side wobble or throttling, both of which pass.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Only these can be a rate limit. Retry-After is NOT exclusive to rate limits (a 503 often
# carries one), so the status has to gate the decision or a 5xx would take the rate-limit
# path and a 401/404 would stop failing fast.
_RATE_LIMIT_STATUS = frozenset({403, 429})


def load_skip_list(skip_list_file: Path, repo: str) -> set[int]:
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

    skip_prs: set[int] = set()
    try:
        content = skip_list_file.read_text()
        for line in content.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Check if this task ID matches our repo
            if line.startswith(prefix):
                # Extract PR number from task ID (e.g., "python__pillow-9272" -> 9272)
                pr_part = line[len(prefix) :]
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
        # Why the stream stopped early, or None if it ran to genuine exhaustion. The loop
        # SWALLOWS a page-fetch error and breaks, which is indistinguishable from "no more
        # PRs" to the caller - so a GitHub 5xx would otherwise be reported as a clean
        # finish. The farmer reads this to tell the two apart in its run report.
        self.stop_reason: str | None = None

        # GitHub API setup
        self.api_base = "https://api.github.com"
        self.github_token = (
            os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or os.getenv("REPO_CREATION_TOKEN")
        )
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "swegen-stream-farm",
        }
        if self.github_token:
            self.headers["Authorization"] = f"token {self.github_token}"

    def _rate_limit_wait(self, resp: requests.Response) -> float | None:
        """Seconds to wait if `resp` is a rate-limit rejection, else None.

        GitHub signals a primary rate limit as 403/429 with X-RateLimit-Remaining: 0, and a
        secondary one as 403/429 with Retry-After. Both clear on their own, so both are
        worth waiting out rather than abandoning the run.

        The STATUS is checked first, and Retry-After alone is never enough. That header is
        not exclusive to rate limits - a 503 routinely carries one - and trusting it on its
        own would route a 5xx down the rate-limit path (waits that spend no retry budget,
        logged as "rate limited") and, worse, stop a 401/404 from failing on the first
        response as it must.
        """
        if resp.status_code not in _RATE_LIMIT_STATUS:
            return None

        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_RATE_LIMIT_WAIT_SECONDS)
            except ValueError:
                pass

        if resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = resp.headers.get("X-RateLimit-Reset")
            if reset:
                try:
                    wait = float(reset) - time.time()
                except ValueError:
                    return None
                if wait > 0:
                    return min(wait + 1, _MAX_RATE_LIMIT_WAIT_SECONDS)
                return 0.0
        return None

    def _get_page_with_retry(
        self, url: str, params: dict[str, Any], page: int
    ) -> requests.Response | None:
        """Fetch one page of PRs, retrying transient failures.

        Returns the response, or None when the stream should stop - in which case
        stop_reason is set so the farmer can report a failure rather than an exhaust.

        Retries a 5xx, a rate limit, and any network/timeout error: at this call volume
        those are routine and self-clearing, and abandoning the run over one costs an
        entire farm. Does NOT retry a 401/403-without-rate-limit/404 - a bad token or a
        missing repo will not fix itself, so retrying only delays the real error.
        """
        last_error = "unknown"
        # An explicit counter, not `for attempt in range(...)`: a rate-limit wait must NOT
        # advance it. With a for-loop, `continue` after a wait silently burns an attempt,
        # so a primary limit whose reset is an hour out would exhaust the budget waiting and
        # then kill the run - the opposite of the intent.
        attempt = 0
        rate_limit_waits = 0

        def _backoff(n: int) -> float:
            return min(_BACKOFF_BASE_SECONDS * 2 ** (n - 1), _MAX_BACKOFF_SECONDS)

        while attempt < _MAX_PAGE_ATTEMPTS:
            try:
                resp = requests.get(url, headers=self.headers, params=params, timeout=30)
            except requests.exceptions.RequestException as exc:
                # Connection reset, DNS, timeout: always transient.
                attempt += 1
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= _MAX_PAGE_ATTEMPTS:
                    break
                delay = _backoff(attempt)
                self.console.print(
                    f"[yellow]Page {page}: {last_error} - retrying in {delay:.0f}s "
                    f"(attempt {attempt}/{_MAX_PAGE_ATTEMPTS})[/yellow]"
                )
                time.sleep(delay)
                continue

            if resp.status_code < 400:
                return resp

            wait = self._rate_limit_wait(resp)
            if wait is not None:
                # Throttled. GitHub told us when to come back, so this is not a failed
                # attempt - do not touch `attempt`. Bound the number of waits instead, so a
                # malformed reset header cannot park the farm indefinitely.
                rate_limit_waits += 1
                last_error = f"HTTP {resp.status_code} {resp.reason} (rate limited)"
                if rate_limit_waits > _MAX_RATE_LIMIT_WAITS:
                    last_error = (
                        f"still rate limited after {_MAX_RATE_LIMIT_WAITS} waits "
                        f"({resp.status_code} {resp.reason})"
                    )
                    break
                self.console.print(
                    f"[yellow]Page {page}: rate limited ({resp.status_code}), waiting "
                    f"{wait:.0f}s (wait {rate_limit_waits}/{_MAX_RATE_LIMIT_WAITS}, "
                    f"does not count as a retry)...[/yellow]"
                )
                time.sleep(wait)
                continue

            attempt += 1
            last_error = f"HTTP {resp.status_code} {resp.reason}"
            if resp.status_code not in _RETRYABLE_STATUS:
                # 401 (bad token), 404 (no such repo), 403 (no access): terminal.
                self.stop_reason = f"GitHub API error on page {page}: {last_error}"
                self.console.print(f"[red]API error on page {page}: {last_error}[/red]")
                return None

            if attempt >= _MAX_PAGE_ATTEMPTS:
                break
            delay = _backoff(attempt)
            self.console.print(
                f"[yellow]Page {page}: {last_error} - retrying in {delay:.0f}s "
                f"(attempt {attempt}/{_MAX_PAGE_ATTEMPTS})[/yellow]"
            )
            time.sleep(delay)

        # Reached either by exhausting retry attempts or by exhausting rate-limit waits;
        # last_error says which, so the run report names the real cause.
        self.stop_reason = f"GitHub API error on page {page}, gave up: {last_error}"
        self.console.print(f"[red]API error on page {page}, giving up: {last_error}[/red]")
        return None

    def stream_prs(
        self,
        resume_from_time: str | None = None,
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
            resume_dt = datetime.fromisoformat(resume_from_time.replace("Z", "+00:00"))
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
                last_dt = datetime.fromisoformat(self.state.last_created_at.replace("Z", "+00:00"))
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
            params: dict[str, Any] = {**params_base, "page": page}

            resp = self._get_page_with_retry(url, params, page)
            if resp is None:
                # Retries exhausted, or a failure that will never self-heal (401/404).
                # _get_page_with_retry has set stop_reason, which is what lets the farmer
                # report this as stream_failed rather than a clean exhaust.
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
                # Exception: a PR left pending on purpose - its task was built but never
                # published, or Claude rate-limited the run before it could be farmed - is
                # always retried. The stream is created-desc, so such a PR is normally older
                # than the cursor and passes anyway, but it shares the cursor's timestamp on a
                # tie, and >= would otherwise strand it forever.
                pending_retry = (
                    pr_number in self.state.publish_failed_prs
                    or pr_number in self.state.claude_rate_limited_prs
                )
                if resume_from_time is not None and created_at:
                    pr_created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    resume_dt = datetime.fromisoformat(resume_from_time.replace("Z", "+00:00"))
                    if pr_created_dt >= resume_dt and not pending_retry:
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
