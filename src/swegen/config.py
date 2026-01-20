from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from harbor.models.environment_type import EnvironmentType


@dataclass(frozen=True)
class CreateConfig:
    """Configuration for the create command (PR → Harbor task).

    The create command uses a language-agnostic pipeline that works
    for any repository. Claude Code analyzes the repo to detect language, runtime,
    build system, and test framework automatically.

    Attributes:
        repo: GitHub repository in "owner/repo" format or full URL
        pr: Pull request number
        output: Output directory for generated tasks (default: tasks/)
        cc_timeout: Timeout for Claude Code session in seconds
        validate: Run Harbor validations (NOP + Oracle)
        force: Bypass local dedupe and regenerate existing tasks
        state_dir: Directory for local state/cache
        use_cache: Reuse cached Dockerfiles/test.sh from previous tasks
        require_minimum_difficulty: Require 3+ source files for task
        min_source_files: Minimum number of source files required (default: 3)
        max_source_files: Maximum number of source files allowed to avoid large refactors (default: 10)
        require_issue: Require PR to have a linked issue (higher quality instructions)
        allow_unmerged: Allow processing unmerged PRs (for testing/preview, default: False)
        environment: Environment type for Harbor runs (docker, daytona, e2b, modal, runloop, gke)
        verbose: Increase output verbosity
        quiet: Reduce output verbosity
    """

    repo: str
    pr: int
    output: Path = field(default_factory=lambda: Path("tasks"))
    cc_timeout: int = 3200
    validate: bool = True
    force: bool = False
    state_dir: Path = field(default_factory=lambda: Path(".state"))
    use_cache: bool = True
    require_minimum_difficulty: bool = True
    min_source_files: int = 3
    max_source_files: int = 10
    require_issue: bool = True
    allow_unmerged: bool = False
    environment: EnvironmentType = EnvironmentType.DOCKER
    verbose: bool = False
    quiet: bool = False

    # Computed property for backward compatibility with old code
    @property
    def no_validate(self) -> bool:
        """Inverse of validate for backward compatibility."""
        return not self.validate


@dataclass(frozen=True)
class FarmConfig:
    """Configuration for the farm command (continuous PR processing).

    The farm command uses a language-agnostic pipeline that works
    for any repository. Claude Code analyzes the repo to detect language, runtime,
    build system, and test framework automatically.

    Attributes:
        repo: GitHub repository in "owner/repo" format
        output: Output directory for generated tasks (default: tasks/)
        state_dir: Directory for local state/cache
        force: Regenerate even if task already exists
        timeout: Timeout per PR in seconds
        cc_timeout: Timeout for Claude Code session in seconds
        api_delay: Delay between GitHub API calls in seconds
        task_delay: Delay between tasks in seconds
        reset: Reset state and start from beginning
        resume_from: Resume from date (ISO format or YYYY-MM-DD)
        dry_run: Only show what would run (no task generation)
        docker_prune_batch: Run docker cleanup after every N PRs (0 to disable)
        skip_list: Path to file with task IDs to skip
        no_cache: Disable reusing cached Dockerfiles/test.sh
        require_minimum_difficulty: Require 3+ source files for task
        min_source_files: Minimum number of source files required (default: 3)
        max_source_files: Maximum number of source files allowed to avoid large refactors (default: 10)
        environment: Environment type for Harbor runs (docker, daytona, e2b, modal, runloop, gke)
        verbose: Enable verbose output
        issue_only: Only process PRs that have linked issues (higher quality instructions)
        validate: Run Harbor validation after CC (useful when CC times out but task may be valid)
    """

    repo: str
    output: Path = field(default_factory=lambda: Path("tasks"))
    state_dir: Path = field(default_factory=lambda: Path(".state"))
    force: bool = True
    timeout: int = 300
    cc_timeout: int = 900
    api_delay: float = 0.5
    task_delay: int = 60
    reset: bool = False
    resume_from: str | None = None
    dry_run: bool = False
    docker_prune_batch: int = 5
    skip_list: str | None = None
    no_cache: bool = False
    require_minimum_difficulty: bool = True
    min_source_files: int = 3
    max_source_files: int = 10
    environment: EnvironmentType = EnvironmentType.DOCKER
    verbose: bool = False
    issue_only: bool = False
    validate: bool = True


@dataclass(frozen=True)
class ValidateConfig:
    """Configuration for the validate command.

    Attributes:
        path: Path to Harbor dataset root or specific task directory
        task: Task ID when path points to dataset root
        agent: Agent to run: both, nop, or oracle
        jobs_dir: Directory to store Harbor job artifacts
        timeout_multiplier: Multiply default timeouts
        environment: Environment type for Harbor runs (docker, daytona, e2b, modal, runloop, gke)
        verbose: Increase output verbosity
        quiet: Reduce output verbosity
        max_parallel: Maximum number of parallel validations (batch mode)
        show_passed: Show passed tasks in output (batch mode)
    """

    path: Path
    task: str | None = None
    agent: Literal["both", "nop", "oracle"] = "both"
    jobs_dir: Path = field(default_factory=lambda: Path(".state/harbor-jobs"))
    timeout_multiplier: float | None = None
    environment: EnvironmentType = EnvironmentType.DOCKER
    verbose: bool = False
    quiet: bool = False
    max_parallel: int = 8
    show_passed: bool = False
