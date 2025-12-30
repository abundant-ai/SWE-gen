from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


logger = logging.getLogger("taskgen")


@dataclass
class TaskReference:
    """Reference to a successful task that can be reused."""
    repo: str
    task_id: str
    pr_number: int
    created_at: Optional[str] = None


class TaskReferenceStore:
    """Stores references to successful tasks for reuse across PRs."""
    
    def __init__(self, reference_file: Optional[Path] = None):
        """
        Initialize task reference store.
        
        Args:
            reference_file: Path to JSON file storing references (default: .state/task_references.json)
        """
        self.reference_file = reference_file or Path(".state/task_references.json")
        self.reference_file.parent.mkdir(parents=True, exist_ok=True)
        
    def _load_references(self) -> dict[str, TaskReference]:
        """Load all references from file."""
        if not self.reference_file.exists():
            return {}
        
        try:
            data = json.loads(self.reference_file.read_text())
            return {
                repo: TaskReference(**ref_data)
                for repo, ref_data in data.items()
            }
        except Exception as e:
            logger.warning(f"Failed to load task references: {e}")
            return {}
    
    def _save_references(self, references: dict[str, TaskReference]) -> None:
        """Save all references to file."""
        data = {
            repo: asdict(ref)
            for repo, ref in references.items()
        }
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
                created_at=datetime.now(timezone.utc).isoformat(),
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
    
    def get(
        self,
        repo: str,
        max_age_days: int = 180,
    ) -> Optional[TaskReference]:
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
                age_days = (datetime.now(timezone.utc) - created).days
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

