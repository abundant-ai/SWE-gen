from __future__ import annotations

import logging
from pathlib import Path

from harbor.models.environment_type import EnvironmentType
from harbor.models.task.task import Task

from .harbor_runner import parse_harbor_outcome, run_harbor_agent


class ValidationError(Exception):
    """Raised when Harbor validation fails (NOP or Oracle)."""

    pass


def validate_task_structure(task_dir: Path) -> bool:
    """Validate task structure using Harbor's Task model.

    This ensures the generated task has all required files and valid structure
    before running Harbor validation.

    Args:
        task_dir: Path to the task directory

    Returns:
        True if task is valid

    Raises:
        ValidationError: If task structure is invalid with details
    """
    logger = logging.getLogger("taskgen")

    try:
        # Use Harbor's Task model to validate structure
        task = Task(task_dir)

        # Verify required attributes are present
        if not task.instruction or len(task.instruction.strip()) < 10:
            raise ValidationError("Invalid instruction: too short or empty")

        if not task.config:
            raise ValidationError("Missing or invalid task.toml")

        # Verify required files exist
        paths = task.paths
        required_files = [
            (paths.instruction_path, "instruction.md"),
            (paths.config_path, "task.toml"),
            (paths.solve_path, "solution/solve.sh"),
            (paths.test_path, "tests/test.sh"),
        ]

        for file_path, name in required_files:
            if not file_path.exists():
                raise ValidationError(f"Missing required file: {name}")

        logger.debug(f"✓ Task structure validated: {task.name}")
        return True

    except ValidationError:
        raise
    except Exception as e:
        logger.error(f"Task validation failed: {e}")
        raise ValidationError(f"Task structure validation failed: {e}") from e


def run_nop_oracle(
    task_id: str,
    dataset_path: Path,
    jobs_dir: Path,
    timeout_multiplier: float | None = None,
    environment: EnvironmentType = EnvironmentType.DOCKER,
) -> tuple[int | None, int | None, dict[str, Path | None]]:
    """Run both NOP and Oracle validations sequentially.

    Validations are always run sequentially to avoid Docker conflict issues.
    NOP keeps the Docker image so Oracle can reuse it (much faster).
    Oracle deletes the image after running (cleanup).

    Args:
        task_id: Task identifier
        dataset_path: Harbor dataset root path
        jobs_dir: Jobs directory path
        timeout_multiplier: Optional timeout multiplier
        environment: Environment type (docker, daytona, e2b, modal, runloop, gke)

    Returns:
        Tuple of (nop_reward, oracle_reward, job_dirs) where:
        - nop_reward: 0 if tests fail on buggy code (expected), None if error
        - oracle_reward: 1 if tests pass after fix (expected), None if error
        - job_dirs: Dict mapping "nop"/"oracle" to job result paths
    """
    job_dirs: dict[str, Path | None] = {"nop": None, "oracle": None}

    # NOP: Keep image (delete_after=False) so Oracle can reuse it
    _, nop_result = run_harbor_agent(
        task_id=task_id,
        dataset_path=dataset_path,
        jobs_dir=jobs_dir,
        agent="nop",
        timeout_multiplier=timeout_multiplier,
        capture_output=True,
        delete_after=False,
        environment=environment,
    )
    nop_reward = parse_harbor_outcome(nop_result).reward
    job_dirs["nop"] = nop_result.parent if nop_result else None

    # Oracle: Delete image after running (cleanup)
    _, oracle_result = run_harbor_agent(
        task_id=task_id,
        dataset_path=dataset_path,
        jobs_dir=jobs_dir,
        agent="oracle",
        timeout_multiplier=timeout_multiplier,
        capture_output=True,
        delete_after=True,
        environment=environment,
    )
    oracle_reward = parse_harbor_outcome(oracle_result).reward
    job_dirs["oracle"] = oracle_result.parent if oracle_result else None

    return nop_reward, oracle_reward, job_dirs


def check_validation_passed(nop_reward: int | None, oracle_reward: int | None) -> bool:
    """Check if validation passed (NOP=0, Oracle=1)."""
    return nop_reward == 0 and oracle_reward == 1


# Re-export for convenience
__all__ = [
    "ValidationError",
    "validate_task_structure",
    "run_nop_oracle",
    "check_validation_passed",
    # Low-level (from harbor_runner)
    "run_harbor_agent",
    "parse_harbor_outcome",
]
