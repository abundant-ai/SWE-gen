from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class StreamState:
    """State for resumable streaming PR processing.

    Tracks which PRs have been processed, success/failure counts,
    and the last processed PR for resume capability.

    Attributes:
        repo: Repository name in "owner/repo" format
        processed_prs: Set of PR numbers that have been processed
        total_fetched: Total PRs fetched from API
        total_processed: Total PRs processed (attempted)
        successful: Count of successfully generated tasks
        failed: Count of failed task generations
        last_pr_number: Last processed PR number
        last_created_at: ISO timestamp of last processed PR's creation time
        last_updated: ISO timestamp of last state update
        skip_list_prs: Set of PR numbers to skip (from external skip list)
        
        # Detailed categorization
        successful_prs: dict[int, str] = None  # PR# -> task_id
        trivial_prs: set[int] = None  # Trivial PRs (too small/simple)
        no_issue_prs: set[int] = None  # PRs without linked issues
        no_tests_prs: set[int] = None  # PRs that don't modify tests
        validation_failed_prs: set[int] = None  # Failed Harbor validation
        already_exists_prs: set[int] = None  # Task already exists
        rate_limit_prs: set[int] = None  # GitHub API rate limit
        quota_exceeded_prs: set[int] = None  # OpenAI quota exceeded
        timeout_prs: set[int] = None  # Command timeouts
        git_error_prs: set[int] = None  # Git checkout/commit errors
        other_failed_prs: dict[int, str] = None  # PR# -> error message
    """

    repo: str
    processed_prs: set[int] = None
    total_fetched: int = 0
    total_processed: int = 0
    successful: int = 0
    failed: int = 0
    last_pr_number: int | None = None
    last_created_at: str | None = None
    last_updated: str | None = None
    skip_list_prs: set[int] = None
    
    # Detailed categorization
    successful_prs: dict[int, str] = None  # PR# -> task_id
    trivial_prs: set[int] = None
    no_issue_prs: set[int] = None
    no_tests_prs: set[int] = None
    validation_failed_prs: set[int] = None
    already_exists_prs: set[int] = None
    rate_limit_prs: set[int] = None
    quota_exceeded_prs: set[int] = None
    timeout_prs: set[int] = None
    git_error_prs: set[int] = None
    other_failed_prs: dict[int, str] = None

    def __post_init__(self):
        if self.processed_prs is None:
            self.processed_prs = set()
        if self.skip_list_prs is None:
            self.skip_list_prs = set()
        if self.successful_prs is None:
            self.successful_prs = {}
        if self.trivial_prs is None:
            self.trivial_prs = set()
        if self.no_issue_prs is None:
            self.no_issue_prs = set()
        if self.no_tests_prs is None:
            self.no_tests_prs = set()
        if self.validation_failed_prs is None:
            self.validation_failed_prs = set()
        if self.already_exists_prs is None:
            self.already_exists_prs = set()
        if self.rate_limit_prs is None:
            self.rate_limit_prs = set()
        if self.quota_exceeded_prs is None:
            self.quota_exceeded_prs = set()
        if self.timeout_prs is None:
            self.timeout_prs = set()
        if self.git_error_prs is None:
            self.git_error_prs = set()
        if self.other_failed_prs is None:
            self.other_failed_prs = {}

    def mark_processed(
        self, pr_number: int, created_at: str, success: bool, task_id: str = None, 
        category: str = None, message: str = None
    ) -> None:
        """Mark a PR as processed and update counters.

        Args:
            pr_number: The PR number that was processed
            created_at: ISO timestamp of when the PR was created
            success: Whether the task generation succeeded
            task_id: Task ID if successful (for tracking)
            category: Category of result (for detailed stats)
            message: Error/skip message (for other_failed category)
        """
        self.processed_prs.add(pr_number)
        self.total_processed += 1
        
        if success:
            self.successful += 1
            if task_id:
                self.successful_prs[pr_number] = task_id
        else:
            self.failed += 1
            # Categorize the failure/skip
            if category == "trivial":
                self.trivial_prs.add(pr_number)
            elif category == "no_issue":
                self.no_issue_prs.add(pr_number)
            elif category == "no_tests":
                self.no_tests_prs.add(pr_number)
            elif category == "validation_failed":
                self.validation_failed_prs.add(pr_number)
            elif category == "already_exists":
                self.already_exists_prs.add(pr_number)
            elif category == "rate_limit":
                self.rate_limit_prs.add(pr_number)
            elif category == "quota_exceeded":
                self.quota_exceeded_prs.add(pr_number)
            elif category == "timeout":
                self.timeout_prs.add(pr_number)
            elif category == "git_error":
                self.git_error_prs.add(pr_number)
            else:
                # Other/unknown error
                self.other_failed_prs[pr_number] = message or "Unknown error"
        
        self.last_pr_number = pr_number
        self.last_created_at = created_at
        self.last_updated = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "repo": self.repo,
            "processed_prs": list(self.processed_prs),
            "total_fetched": self.total_fetched,
            "total_processed": self.total_processed,
            "successful": self.successful,
            "failed": self.failed,
            "last_pr_number": self.last_pr_number,
            "last_created_at": self.last_created_at,
            "last_updated": self.last_updated,
            # Detailed breakdown
            "successful_prs": {str(k): v for k, v in self.successful_prs.items()},
            "trivial_prs": list(self.trivial_prs),
            "no_issue_prs": list(self.no_issue_prs),
            "no_tests_prs": list(self.no_tests_prs),
            "validation_failed_prs": list(self.validation_failed_prs),
            "already_exists_prs": list(self.already_exists_prs),
            "rate_limit_prs": list(self.rate_limit_prs),
            "quota_exceeded_prs": list(self.quota_exceeded_prs),
            "timeout_prs": list(self.timeout_prs),
            "git_error_prs": list(self.git_error_prs),
            "other_failed_prs": {str(k): v for k, v in self.other_failed_prs.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> StreamState:
        """Load state from a dict.

        Args:
            data: Dict previously created by to_dict()

        Returns:
            StreamState instance
        """
        return cls(
            repo=data["repo"],
            processed_prs=set(data.get("processed_prs", [])),
            total_fetched=data.get("total_fetched", 0),
            total_processed=data.get("total_processed", 0),
            successful=data.get("successful", 0),
            failed=data.get("failed", 0),
            last_pr_number=data.get("last_pr_number"),
            last_created_at=data.get("last_created_at"),
            last_updated=data.get("last_updated"),
            # Detailed breakdown
            successful_prs={int(k): v for k, v in data.get("successful_prs", {}).items()},
            trivial_prs=set(data.get("trivial_prs", [])),
            no_issue_prs=set(data.get("no_issue_prs", [])),
            no_tests_prs=set(data.get("no_tests_prs", [])),
            validation_failed_prs=set(data.get("validation_failed_prs", [])),
            already_exists_prs=set(data.get("already_exists_prs", [])),
            rate_limit_prs=set(data.get("rate_limit_prs", [])),
            quota_exceeded_prs=set(data.get("quota_exceeded_prs", [])),
            timeout_prs=set(data.get("timeout_prs", [])),
            git_error_prs=set(data.get("git_error_prs", [])),
            other_failed_prs={int(k): v for k, v in data.get("other_failed_prs", {}).items()},
        )

    def save(self, state_file: Path) -> None:
        """Save state to a JSON file.

        Args:
            state_file: Path to save state to
        """
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, state_file: Path, repo: str) -> StreamState:
        """Load state from file, or create new if not exists.

        Args:
            state_file: Path to state file
            repo: Repository name (used to verify state matches)

        Returns:
            StreamState instance (loaded or new)
        """
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                if data.get("repo") == repo:
                    return cls.from_dict(data)
            except Exception:
                pass
        return cls(repo=repo)
