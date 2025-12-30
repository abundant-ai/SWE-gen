from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from taskgen.config import FarmConfig, ReversalConfig
from taskgen.reversal import TrivialPRError, MissingIssueError, ValidationError
from taskgen.reversal.reversal import run_reversal
from taskgen.reversal.task_reference import TaskReferenceStore


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _slug(repo: str) -> str:
    """Convert repo to slug using SWEBench convention: owner/repo -> owner__repo"""
    return repo.replace("/", "__")


def _task_id(repo: str, pr_number: int) -> str:
    """Generate task ID using SWEBench convention: owner__repo-number"""
    return f"{_slug(repo)}-{pr_number}"


@dataclass
class PRCandidate:
    """A candidate PR for task generation."""
    number: int
    title: str
    created_at: str
    merged_at: str
    author: str
    files_changed: int
    additions: int
    deletions: int
    url: str


@dataclass
class TaskResult:
    """Result of processing a single PR into a task."""
    repo: str
    pr_number: int
    task_id: str
    status: str  # "success", "failed", or "dry-run"
    message: str
    duration_seconds: float
    timestamp: str


def _cleanup_task(task_id: str, tasks_root: Path, console: Console) -> None:
    removed_any = False
    paths = [
        tasks_root / task_id,
        Path("trash") / task_id,
    ]
    for path in paths:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed_any = True
    if removed_any:
        console.print(f"[dim]Cleaned up incomplete task directory: {task_id}[/dim]")


def _classify_failure(stderr: str) -> str:
    lowered = stderr.lower()
    if "trivial" in stderr:
        return "Trivial PR (skipped)"
    if "no linked issue" in lowered or "missingissueerror" in lowered:
        return "No linked issue (skipped)"
    if "validation failed" in lowered or "harbor validation" in lowered:
        return "Validation failed (NOP or Oracle)"
    if "task already exists" in lowered or "file exists" in lowered:
        return "Task already exists (skipped)"
    if "no test" in stderr:
        return "No tests detected"
    if "rate limit exceeded" in lowered and "github" in lowered:
        return "GitHub API rate limit exceeded (set GITHUB_TOKEN)"
    if "insufficient_quota" in lowered or "exceeded your current quota" in lowered:
        return "OpenAI API quota exceeded (check billing)"
    if "timed out" in lowered or "timeout" in lowered:
        return "Command timed out"
    if "cannot checkout commit" in lowered or "force-pushed or deleted" in lowered:
        return "Git commit not found (may be force-pushed or deleted)"
    if "git checkout" in lowered:
        return "Git checkout failed (repo cache may be corrupted)"
    return (stderr or "Unknown error").replace("\n", " ")


def _print_success(
    console: Console,
    pr: PRCandidate,
    task_id: str,
    harbor_dir: Path,
) -> None:
    console.print(
        Panel.fit(
            f"🎉 Successfully generated task\n[bold]{task_id}[/bold]\nHarbor: {harbor_dir}",
            title=f"PR #{pr.number}",
            border_style="green",
        )
    )


def _gate_task(
    task_id: str,
    tasks_root: Path,
) -> tuple[bool, str]:
    """
    Validate that the task directory exists.
    
    Returns:
        Tuple of (success, message)
    """
    task_dir = tasks_root / task_id
    if not task_dir.exists():
        return False, f"Task directory missing: {task_dir}"

    return True, f"Task generated successfully at {task_dir}"


def _run_reversal_for_pr(
    pr: PRCandidate,
    config: FarmConfig,
    tasks_root: Path,
    console: Console,
) -> TaskResult:
    start = time.time()
    task_id = _task_id(config.repo, pr.number)
    harbor_dir = tasks_root / task_id

    # Wrap everything in try-except to catch unexpected errors
    try:
        return _run_reversal_for_pr_impl(pr, config, tasks_root, console, task_id, harbor_dir, start)
    except Exception as e:
        # Catch any unexpected exception and return proper error
        import traceback
        error_msg = f"Unexpected error: {type(e).__name__}: {str(e)}"
        console.print(f"[red]✗ PR #{pr.number}: {error_msg}[/red]")
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
        _cleanup_task(task_id, tasks_root, console)
        return TaskResult(
            repo=config.repo,
            pr_number=pr.number,
            task_id=task_id,
            status="failed",
            message=error_msg,
            duration_seconds=round(time.time() - start, 2),
            timestamp=_now_utc().isoformat(),
        )


def _run_reversal_for_pr_impl(
    pr: PRCandidate,
    config: FarmConfig,
    tasks_root: Path,
    console: Console,
    task_id: str,
    harbor_dir: Path,
    start: float,
) -> TaskResult:
    if config.dry_run:
        console.print(
            f"[cyan]DRY RUN[/cyan] would generate task for PR #{pr.number} -> {task_id}"
        )
        return TaskResult(
            repo=config.repo,
            pr_number=pr.number,
            task_id=task_id,
            status="dry-run",
            message="Dry run (skipped actual execution)",
            duration_seconds=0.0,
            timestamp=_now_utc().isoformat(),
        )

    # Build ReversalConfig for run_reversal (universal pipeline)
    reversal_config = ReversalConfig(
        repo=config.repo,
        pr=pr.number,
        output=config.output,
        cc_timeout=config.cc_timeout,
        validate=config.validate,  # Run Harbor validation if --validate flag is set
        network_isolated=config.network_isolated,
        force=config.force,
        state_dir=config.state_dir,
        verbose=config.verbose,
        quiet=False,
        use_cache=not config.no_cache,
        require_minimum_difficulty=config.require_minimum_difficulty,
        min_source_files=config.min_source_files,
        max_source_files=config.max_source_files,
        require_issue=config.issue_only,  # Pass through issue_only flag
        environment=config.environment,  # Pass through environment type
    )
    
    console.print(f"[dim]Generating task for PR #{pr.number} using pipeline directly...[/dim]")

    # Capture any errors from the pipeline
    success = False
    error_msg = ""
    
    try:
        # Call the pipeline directly instead of using subprocess
        run_reversal(reversal_config)
        success = True
    except TrivialPRError as e:
        # Trivial PR - not an error, just skip it
        error_msg = str(e)
        success = False
    except MissingIssueError as e:
        # No linked issue - not an error, just skip it
        error_msg = str(e)
        success = False
    except ValidationError as e:
        # Validation failed - not an error, just skip it
        error_msg = str(e)
        success = False
    except FileExistsError as e:
        # Task already exists - skip it
        error_msg = f"Task already exists: {str(e)}"
        success = False
    except Exception as e:
        # Other errors
        import traceback
        error_msg = f"{type(e).__name__}: {str(e)}"
        if config.verbose:
            console.print(f"[red]{traceback.format_exc()}[/red]")
        success = False

    if success:
        if not harbor_dir.exists():
            # Check for trivial PR (should have been caught by TrivialPRError)
            if "trivial" in error_msg.lower():
                failure_reason = "Trivial PR (skipped)"
            else:
                failure_reason = "Pipeline reported success but Harbor task directory was not created."
            _cleanup_task(task_id, tasks_root, console)
            console.print(f"[red]✗ PR #{pr.number}: {failure_reason}[/red]")
            return TaskResult(
                repo=config.repo,
                pr_number=pr.number,
                task_id=task_id,
                status="failed",
                message=failure_reason,
                duration_seconds=round(time.time() - start, 2),
                timestamp=_now_utc().isoformat(),
            )

        # Task is already in Harbor format (reversal now generates directly to Harbor)
        duration = time.time() - start
        gate_ok, gate_msg = _gate_task(task_id, tasks_root)
        if gate_ok:
            _print_success(console, pr, task_id, harbor_dir)
            
            # Save task reference for future PRs (universal pipeline)
            try:
                reference_store = TaskReferenceStore()
                reference_store.save(
                    repo=config.repo,
                    task_id=task_id,
                    pr_number=pr.number,
                )
            except Exception as e:
                console.print(f"[yellow]Warning: Could not save task reference: {e}[/yellow]")
            
            return TaskResult(
                repo=config.repo,
                pr_number=pr.number,
                task_id=task_id,
                status="success",
                message=gate_msg,
                duration_seconds=round(duration, 2),
                timestamp=_now_utc().isoformat(),
            )
        
        # Gate failed
        failure_reason = gate_msg
        _cleanup_task(task_id, tasks_root, console)
        console.print(f"[red]✗ PR #{pr.number}: {failure_reason}[/red]")
        return TaskResult(
            repo=config.repo,
            pr_number=pr.number,
            task_id=task_id,
            status="failed",
            message=failure_reason,
            duration_seconds=round(duration, 2),
            timestamp=_now_utc().isoformat(),
        )

    # Pipeline failed
    failure_reason = _classify_failure(error_msg)
    _cleanup_task(task_id, tasks_root, console)
    console.print(
        f"[red]✗ PR #{pr.number}: {failure_reason}[/red]"
    )
    return TaskResult(
        repo=config.repo,
        pr_number=pr.number,
        task_id=task_id,
        status="failed",
        message=failure_reason,
        duration_seconds=round(time.time() - start, 2),
        timestamp=_now_utc().isoformat(),
    )
