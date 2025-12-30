import logging
import os
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
import re


class GitHubPRFetcher:
    """Fetches PR metadata from GitHub API."""

    def __init__(self, repo: str, pr_number: int, github_token: Optional[str] = None):
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
        self.headers = {
            "Accept": "application/vnd.github.v3+json"
        }
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

    def _api_get(self, endpoint: str) -> Dict:
        """Make a GET request to GitHub API."""
        url = f"{self.api_base}{endpoint}"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def fetch_pr_metadata(self) -> Dict:
        """Fetch PR metadata from GitHub API."""
        logger = logging.getLogger("taskgen")
        logger.debug("Fetching PR #%s metadata from %s...", self.pr_number, self.repo)
        pr_data = self._api_get(f"/repos/{self.repo}/pulls/{self.pr_number}")

        if not pr_data.get("merged"):
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

    def fetch_pr_files(self) -> List[Dict]:
        """Fetch list of files changed in the PR."""
        logger = logging.getLogger("taskgen")
        logger.debug("Fetching changed files for PR #%s...", self.pr_number)
        files = self._api_get(f"/repos/{self.repo}/pulls/{self.pr_number}/files")
        logger.debug("Found %d changed files", len(files))
        for f in files:
            logger.debug("  %s %s", f['status'], f['filename'])

        return files

    def fetch_linked_issues(self) -> List[Dict]:
        """Fetch issues linked/referenced in the PR.
        
        Uses the BROADEST approach possible:
        1. GitHub Timeline API (catches manual links and cross-references)
        2. PR title parsing
        3. PR body parsing
        
        Returns a list of issue dictionaries with 'number', 'title', and 'body'.
        """
        logger = logging.getLogger("taskgen")
        logger.debug("Fetching linked issues for PR #%s...", self.pr_number)
        
        issues = []
        issue_numbers = set()
        
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
                        if issue_num and issue_num != self.pr_number:
                            issue_numbers.add(issue_num)
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
            comments_pat = re.compile(r'(?s)<!--.*?-->')
            text = comments_pat.sub("", text)
            
            # Match patterns - BROADEST approach (keywords optional, standalone #123 also matches)
            patterns = [
                r'(?:fix(?:es|ed)?|close(?:s|d)?|resolve(?:s|d)?)\s+#(\d+)',  # With keywords
                r'(?:fix(?:es|ed)?|close(?:s|d)?|resolve(?:s|d)?)\s+https?://github\.com/[^/]+/[^/]+/issues/(\d+)',  # Full URLs with keywords
                r'#(\d+)',  # Standalone #123 (no keyword required - broadest)
                r'https?://github\.com/[^/]+/[^/]+/issues/(\d+)',  # Full URLs without keywords
            ]
            
            for pattern in patterns:
                matches = re.finditer(pattern, text, re.IGNORECASE)
                for match in matches:
                    issue_num = int(match.group(1))
                    if issue_num != self.pr_number:  # Don't include the PR itself
                        issue_numbers.add(issue_num)
        except Exception as e:
            logger.debug("Failed to parse PR title/body for issue refs: %s", str(e))
        
        # Fetch full issue data for each linked issue
        for issue_num in sorted(issue_numbers):
            try:
                issue_data = self._api_get(f"/repos/{self.repo}/issues/{issue_num}")
                issues.append({
                    "number": issue_data["number"],
                    "title": issue_data["title"],
                    "body": issue_data.get("body", ""),
                    "state": issue_data.get("state", ""),
                    "html_url": issue_data.get("html_url", ""),
                })
                logger.debug("  Found linked issue #%d: %s", issue_num, issue_data["title"])
            except Exception as e:
                logger.debug("  Failed to fetch issue #%d: %s", issue_num, str(e))
        
        logger.debug("Collected %d linked issues", len(issues))
        return issues
