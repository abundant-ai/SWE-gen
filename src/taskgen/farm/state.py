from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set


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
    """
    repo: str
    processed_prs: Set[int] = None
    total_fetched: int = 0
    total_processed: int = 0
    successful: int = 0
    failed: int = 0
    last_pr_number: Optional[int] = None
    last_created_at: Optional[str] = None
    last_updated: Optional[str] = None
    skip_list_prs: Set[int] = None

    def __post_init__(self):
        if self.processed_prs is None:
            self.processed_prs = set()
        if self.skip_list_prs is None:
            self.skip_list_prs = set()

    def mark_processed(self, pr_number: int, created_at: str, success: bool) -> None:
        """Mark a PR as processed and update counters.
        
        Args:
            pr_number: The PR number that was processed
            created_at: ISO timestamp of when the PR was created
            success: Whether the task generation succeeded
        """
        self.processed_prs.add(pr_number)
        self.total_processed += 1
        if success:
            self.successful += 1
        else:
            self.failed += 1
        self.last_pr_number = pr_number
        self.last_created_at = created_at
        self.last_updated = datetime.now(timezone.utc).isoformat()

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

