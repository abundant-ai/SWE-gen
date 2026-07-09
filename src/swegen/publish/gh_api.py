from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .base import PublishAuthError, PublishError

API_BASE = "https://api.github.com"
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 60.0


class GitHubAPI:
    """Minimal GitHub REST client for publishing, with bounded retries.

    Uses `requests` directly, matching create/pr_fetcher.py rather than pulling in a
    second GitHub client style.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        self.logger = logging.getLogger("swegen")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _sleep_for(self, response: requests.Response, attempt: int) -> float:
        """Honor Retry-After when GitHub sends it, else exponential backoff."""
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
        return BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))

    def _is_auth_failure(self, response: requests.Response) -> bool:
        """401 always. 403 only when it is not a rate limit dressed up as a 403."""
        if response.status_code == 401:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get("Retry-After"):
            return False
        return response.headers.get("X-RateLimit-Remaining") != "0"

    def request(self, method: str, path: str, json: dict | None = None) -> Any:
        """Issue a request, retrying transient failures.

        Raises:
            PublishAuthError: the token is rejected (never retried).
            PublishError: retries were exhausted (5xx, rate limit, dead connection), or
                GitHub rejected the request outright (e.g. 422). Either way the caller
                stops farming; the two are distinguished only in the message.
        """
        url = f"{API_BASE}{path}"
        last_error = ""
        exhausted_retries = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.session.request(method, url, json=json, timeout=30)
            except requests.RequestException as e:
                last_error = str(e)
                if attempt == MAX_ATTEMPTS:
                    exhausted_retries = True
                    break
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                continue

            if response.status_code < 400:
                return response.json() if response.content else {}

            if self._is_auth_failure(response):
                raise PublishAuthError(
                    f"{method} {path} -> {response.status_code}: token is invalid or "
                    f"lacks write access. {response.text[:200]}"
                )

            retryable = response.status_code in (429, 500, 502, 503, 504) or (
                response.status_code == 403 and not self._is_auth_failure(response)
            )
            last_error = f"{response.status_code}: {response.text[:200]}"
            if not retryable:
                break
            if attempt == MAX_ATTEMPTS:
                exhausted_retries = True
                break

            delay = self._sleep_for(response, attempt)
            self.logger.warning(
                "GitHub %s %s -> %s, retrying in %.1fs (attempt %d/%d)",
                method,
                path,
                response.status_code,
                delay,
                attempt,
                MAX_ATTEMPTS,
            )
            time.sleep(delay)

        if exhausted_retries:
            raise PublishError(
                f"{method} {path} failed after {MAX_ATTEMPTS} attempts: {last_error}. "
                f"GitHub is unreachable or throttling."
            )
        raise PublishError(f"{method} {path} rejected: {last_error}")

    # -- endpoints used by the sink -----------------------------------------

    def get_repo(self, repo: str) -> dict:
        return self.request("GET", f"/repos/{repo}")

    def find_open_pr(self, repo: str, head_branch: str) -> dict | None:
        owner = repo.split("/")[0]
        prs = self.request("GET", f"/repos/{repo}/pulls?head={owner}:{head_branch}&state=open")
        return prs[0] if prs else None

    def create_pr(self, repo: str, title: str, body: str, head: str, base: str) -> dict:
        return self.request(
            "POST",
            f"/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
