from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("swegen")


@dataclass
class TaskReference:
    """Reference to a successful task that can be reused."""

    repo: str
    task_id: str
    pr_number: int
    created_at: str | None = None


class TaskReferenceStore:
    """Stores references to successful tasks for reuse across PRs."""

    def __init__(self, reference_file: Path | None = None):
        """
        Initialize task reference store.

        Args:
            reference_file: Path to JSON file storing references (default: .swegen/task_references.json)
        """
        self.reference_file = reference_file or Path(".swegen/task_references.json")
        self.reference_file.parent.mkdir(parents=True, exist_ok=True)

    def _load_references(self) -> dict[str, TaskReference]:
        """Load all references from file."""
        if not self.reference_file.exists():
            return {}

        try:
            data = json.loads(self.reference_file.read_text())
            return {repo: TaskReference(**ref_data) for repo, ref_data in data.items()}
        except Exception as e:
            logger.warning(f"Failed to load task references: {e}")
            return {}

    def _save_references(self, references: dict[str, TaskReference]) -> None:
        """Save all references to file."""
        data = {repo: asdict(ref) for repo, ref in references.items()}
        self.reference_file.write_text(json.dumps(data, indent=2))

    def save(
        self,
        repo: str,
        task_id: str,
        pr_number: int,
    ) -> bool:
        """
        Save a reference to a successful task.

        Args:
            repo: Repository name (owner/repo)
            task_id: Task identifier of the successful task
            pr_number: PR number

        Returns:
            True if reference was saved successfully
        """
        try:
            # Create reference
            reference = TaskReference(
                repo=repo,
                task_id=task_id,
                pr_number=pr_number,
                created_at=datetime.now(UTC).isoformat(),
            )

            # Load, update, save
            references = self._load_references()
            references[repo] = reference
            self._save_references(references)

            logger.info(f"✓ Saved task reference for {repo} → {task_id}")
            return True

        except Exception as e:
            logger.warning(f"Failed to save task reference: {e}")
            return False

    def clear(self, repo: str, task_id: str) -> None:
        """Drop the reference for `repo` if it points at `task_id`.

        Used when a task directory is deleted (e.g. --cleanup-local): a reference points
        Claude Code at that directory, so a reference to a removed task would dangle.
        Non-fatal: a stale reference only degrades to from-scratch generation.
        """
        try:
            references = self._load_references()
            existing = references.get(repo)
            if existing is not None and existing.task_id == task_id:
                del references[repo]
                self._save_references(references)
        except Exception as e:
            logger.warning(f"Failed to clear task reference for {repo}: {e}")

    def get(
        self,
        repo: str,
        max_age_days: int = 180,
    ) -> TaskReference | None:
        """
        Get reference to a successful task for reuse.

        Args:
            repo: Repository name (owner/repo)
            max_age_days: Maximum age of reference in days (default: 180)

        Returns:
            TaskReference if valid reference exists, None otherwise
        """
        try:
            references = self._load_references()

            if repo not in references:
                logger.debug(f"No task reference found for {repo}")
                return None

            reference = references[repo]

            # Check age
            if reference.created_at:
                created = datetime.fromisoformat(reference.created_at)
                age_days = (datetime.now(UTC) - created).days
                if age_days > max_age_days:
                    logger.debug(f"Reference too old for {repo}: {age_days} days > {max_age_days}")
                    return None

            logger.info(
                f"✓ Found task reference for {repo} → {reference.task_id} "
                f"(from PR #{reference.pr_number})"
            )
            return reference

        except Exception as e:
            logger.warning(f"Failed to get task reference for {repo}: {e}")
            return None
