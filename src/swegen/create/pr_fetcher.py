from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

import requests


class GitHubPRFetcher:
    """Fetches PR metadata from GitHub API."""

    def __init__(self, repo: str, pr_number: int, github_token: str | None = None):
        """
        Initialize the PR fetcher.

        Args:
            repo: GitHub repo in format "owner/repo" or full URL
            pr_number: PR number
            github_token: Optional GitHub token for API access
        """
        self.repo = self._parse_repo(repo)
        self.pr_number = pr_number
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN")

        # API setup
        self.api_base = "https://api.github.com"
        self.headers = {"Accept": "application/vnd.github.v3+json"}
        if self.github_token:
            self.headers["Authorization"] = f"token {self.github_token}"

    def _parse_repo(self, repo: str) -> str:
        """Parse repo URL or owner/repo format to owner/repo."""
        if repo.startswith("http"):
            parsed = urlparse(repo)
            # Extract owner/repo from path
            path = parsed.path.strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return path
        return repo

    def _api_get(self, endpoint: str) -> dict:
        """Make a GET request to GitHub API."""
        url = f"{self.api_base}{endpoint}"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def _get_upstream_repo(self) -> str | None:
        """Get the upstream (parent) repo if current repo is a fork.

        Returns:
            The parent repo in "owner/repo" format, or None if not a fork.
        """
        logger = logging.getLogger("swegen")
        try:
            repo_data = self._api_get(f"/repos/{self.repo}")
            if repo_data.get("fork") and repo_data.get("parent"):
                upstream = repo_data["parent"]["full_name"]
                logger.debug("Detected fork: %s → upstream: %s", self.repo, upstream)
                return upstream
        except Exception as e:
            logger.debug("Failed to check fork status: %s", str(e))
        return None

    def fetch_pr_metadata(self, allow_unmerged: bool = False) -> dict:
        """Fetch PR metadata from GitHub API.
        
        Args:
            allow_unmerged: If True, allow unmerged PRs (for testing/preview). Default False.
        """
        logger = logging.getLogger("swegen")
        logger.debug("Fetching PR #%s metadata from %s...", self.pr_number, self.repo)
        pr_data = self._api_get(f"/repos/{self.repo}/pulls/{self.pr_number}")

        if not allow_unmerged and not pr_data.get("merged"):
            raise ValueError(f"PR #{self.pr_number} is not merged yet!")

        # Get the commits
        base_sha = pr_data["base"]["sha"]
        head_sha = pr_data["head"]["sha"]
        merge_commit_sha = pr_data.get("merge_commit_sha")
        logger.debug("Base SHA: %s", base_sha)
        logger.debug("Head SHA: %s", head_sha)
        logger.debug("Merge SHA: %s", merge_commit_sha)

        return {
            "title": pr_data["title"],
            "body": pr_data.get("body", ""),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "merge_commit_sha": merge_commit_sha,
            "base_ref": pr_data["base"]["ref"],
            "head_ref": pr_data["head"]["ref"],
            "repo_url": pr_data["base"]["repo"]["clone_url"],
            "html_url": pr_data["html_url"],
            "created_at": pr_data["created_at"],
            "merged_at": pr_data["merged_at"],
        }

    def fetch_pr_files(self) -> list[dict]:
        """Fetch list of files changed in the PR."""
        logger = logging.getLogger("swegen")
        logger.debug("Fetching changed files for PR #%s...", self.pr_number)
        files_response = self._api_get(f"/repos/{self.repo}/pulls/{self.pr_number}/files")
        # API may return dict with pagination info or list directly
        files = (
            files_response if isinstance(files_response, list) else files_response.get("files", [])
        )
        logger.debug("Found %d changed files", len(files))
        for f in files:
            logger.debug("  %s %s", f["status"], f["filename"])

        return files

    def fetch_linked_issues(self) -> list[dict]:
        """Fetch issues linked/referenced in the PR.

        Uses the BROADEST approach possible:
        1. GitHub Timeline API (catches manual links and cross-references)
        2. PR title parsing
        3. PR body parsing

        Automatically detects fork PRs and looks for issues in upstream repos.
        Supports cross-repo references like `owner/repo#123` and full GitHub URLs.

        Returns a list of issue dictionaries with 'number', 'title', and 'body'.
        """
        logger = logging.getLogger("swegen")
        logger.debug("Fetching linked issues for PR #%s...", self.pr_number)

        # Dict mapping (repo, issue_num) to avoid duplicates
        issue_refs: dict[tuple[str, int], None] = {}

        # Check if this is a fork and get upstream repo
        upstream_repo = self._get_upstream_repo()

        try:
            # Method 1: Use timeline API to find closing references and manual links
            timeline_url = f"/repos/{self.repo}/issues/{self.pr_number}/timeline"
            headers = self.headers.copy()
            headers["Accept"] = "application/vnd.github.mockingbird-preview+json"

            url = f"{self.api_base}{timeline_url}"
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            timeline = response.json()

            for event in timeline:
                if event.get("event") == "cross-referenced":
                    source = event.get("source", {})
                    if source.get("type") == "issue":
                        issue_data = source.get("issue", {})
                        issue_num = issue_data.get("number")
                        # Get the repo from the issue URL
                        issue_url = issue_data.get("repository_url", "")
                        issue_repo = self.repo
                        if issue_url:
                            # Extract repo from URL like https://api.github.com/repos/owner/repo
                            match = re.search(r"/repos/([^/]+/[^/]+)$", issue_url)
                            if match:
                                issue_repo = match.group(1)
                        if issue_num and issue_num != self.pr_number:
                            issue_refs[(issue_repo, issue_num)] = None
        except Exception as e:
            logger.debug("Timeline API failed (may not have access): %s", str(e))

        try:
            # Method 2: Parse PR title and body for issue references
            pr_data = self._api_get(f"/repos/{self.repo}/pulls/{self.pr_number}")
            pr_title = pr_data.get("title", "") or ""
            pr_body = pr_data.get("body", "") or ""

            # Combine title and body
            text = f"{pr_title}\n{pr_body}"

            # Remove HTML comments before parsing (like SWE-smith does)
            comments_pat = re.compile(r"(?s)<!--.*?-->")
            text = comments_pat.sub("", text)

            # Collect bare issue numbers (no repo specified) - will check both current and upstream
            bare_issue_numbers: set[int] = set()

            # Pattern 1: Full GitHub URLs - extract both repo and issue number
            url_pattern = r"https?://github\.com/([^/]+/[^/]+)/issues/(\d+)"
            for match in re.finditer(url_pattern, text, re.IGNORECASE):
                repo_from_url = match.group(1)
                issue_num = int(match.group(2))
                if issue_num != self.pr_number or repo_from_url != self.repo:
                    issue_refs[(repo_from_url, issue_num)] = None
                    logger.debug("  Found cross-repo URL reference: %s#%d", repo_from_url, issue_num)

            # Pattern 2: Cross-repo references like owner/repo#123
            cross_repo_pattern = r"(?<!\w)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)"
            for match in re.finditer(cross_repo_pattern, text):
                repo_from_ref = match.group(1)
                issue_num = int(match.group(2))
                # Skip if it looks like a URL fragment we already captured
                if not match.group(0).startswith("http"):
                    issue_refs[(repo_from_ref, issue_num)] = None
                    logger.debug("  Found cross-repo reference: %s#%d", repo_from_ref, issue_num)

            # Pattern 3: Bare #123 references (no repo specified)
            # Match with optional keywords
            bare_patterns = [
                r"(?:fix(?:es|ed)?|close(?:s|d)?|resolve(?:s|d)?)\s+#(\d+)",  # With keywords
                r"(?<![/\w])#(\d+)(?!\d)",  # Standalone #123 (not part of URL or word)
            ]
            for pattern in bare_patterns:
                for match in re.finditer(pattern, text, re.IGNORECASE):
                    issue_num = int(match.group(1))
                    if issue_num != self.pr_number:
                        bare_issue_numbers.add(issue_num)

            # For bare issue numbers, try current repo first, then upstream if in a fork
            for issue_num in bare_issue_numbers:
                issue_refs[(self.repo, issue_num)] = None
                if upstream_repo:
                    issue_refs[(upstream_repo, issue_num)] = None

        except Exception as e:
            logger.debug("Failed to parse PR title/body for issue refs: %s", str(e))

        # Fetch full issue data for each linked issue
        issues = []
        fetched_issues: set[tuple[str, int]] = set()  # Track which we successfully fetched

        for (repo, issue_num) in sorted(issue_refs.keys(), key=lambda x: (x[0], x[1])):
            # Skip if we already fetched this issue from another repo
            # (in case same issue number exists in both fork and upstream)
            if (repo, issue_num) in fetched_issues:
                continue

            try:
                issue_data = self._api_get(f"/repos/{repo}/issues/{issue_num}")
                # Skip pull requests (GitHub API returns PRs as issues too)
                if issue_data.get("pull_request"):
                    logger.debug("  Skipping PR #%d in %s (not an issue)", issue_num, repo)
                    continue

                issues.append(
                    {
                        "number": issue_data["number"],
                        "title": issue_data["title"],
                        "body": issue_data.get("body", ""),
                        "state": issue_data.get("state", ""),
                        "html_url": issue_data.get("html_url", ""),
                        "repo": repo,  # Include repo for clarity
                    }
                )
                fetched_issues.add((repo, issue_num))
                logger.debug("  Found linked issue %s#%d: %s", repo, issue_num, issue_data["title"])

                # If this was a bare reference and we found it in upstream, skip the fork version
                if upstream_repo and repo == upstream_repo:
                    fetched_issues.add((self.repo, issue_num))
            except Exception as e:
                logger.debug("  Failed to fetch issue %s#%d: %s", repo, issue_num, str(e))

        logger.debug("Collected %d linked issues", len(issues))
        return issues
