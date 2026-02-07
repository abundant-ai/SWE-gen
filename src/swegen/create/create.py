from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor.models.environment_type import EnvironmentType
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.traceback import install as rich_traceback_install

from swegen.config import CreateConfig
from swegen.tools.harbor_runner import parse_harbor_outcome, run_harbor_agent
from swegen.tools.validate_utils import ValidationError, run_nop_oracle

from . import MissingIssueError, PRToHarborPipeline, TrivialPRError
from .claude_code_runner import ClaudeCodeResult, run_claude_code_session
from .repo_cache import RepoCache

# -----------------------------------------------------------------------------
# Helper functions for run_reversal phases
# -----------------------------------------------------------------------------


def _display_header(console: Console, pipeline: PRToHarborPipeline, pr: int) -> None:
    """Display the initial header panel with repo and PR context."""
    console.print(Rule(Text("Task Generation", style="bold cyan")))
    info = Table(show_header=False, box=None)
    info.add_row("Repo", Text(pipeline.repo, style="bold"))
    info.add_row("PR", Text(str(pr), style="bold"))
    console.print(Panel(info, title="Context", expand=False))


def _check_linked_issues(
    console: Console,
    pipeline: PRToHarborPipeline,
    pr: int,
    require_issue: bool,
) -> list:
    """Check for linked issues and validate requirements.

    Returns list of linked issues.
    Raises MissingIssueError if required and none found.
    """
    linked_issues = []
    try:
        linked_issues = pipeline.pr_fetcher.fetch_linked_issues()
    except Exception as e:
        logging.getLogger("swegen").debug("Could not fetch linked issues: %s", str(e))

    if require_issue:
        if not linked_issues:
            console.print(
                Panel(
                    Text(
                        f"PR #{pr} has no linked issue. Use --no-require-issue to generate task from PR body/title instead.",
                        style="yellow",
                    ),
                    title="[yellow]Skipped (No Linked Issue)[/yellow]",
                    border_style="yellow",
                )
            )
            raise MissingIssueError(
                f"PR #{pr}: No linked issue found (use --no-require-issue to skip this check)"
            )
        else:
            console.print(f"[green]✓ Found {len(linked_issues)} linked issue(s)[/green]")
    else:
        if linked_issues:
            console.print(f"[dim]Found {len(linked_issues)} linked issue(s)[/dim]")
        else:
            console.print(
                "[yellow]No linked issue found - using PR body/title for instructions[/yellow]"
            )

    return linked_issues


def _check_dedupe(
    console: Console,
    repo_key: str,
    state_file: Path,
    force: bool,
) -> bool:
    """Check if task already exists in state file.

    Returns True if duplicate found and should skip, False otherwise.
    """
    if force or not state_file.exists():
        return False

    last_rec = None
    logger = logging.getLogger("swegen")
    with open(state_file) as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("key") == repo_key:
                    last_rec = rec
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.debug(f"Failed to parse state record line: {e}")
                continue

    if last_rec is not None:
        existing_harbor = last_rec.get("harbor")
        body = Table(show_header=False, box=None)
        body.add_row("harbor", Text(str(existing_harbor)))
        console.print(
            Panel(
                body,
                title=f"Duplicate key: [bold]{repo_key}[/bold]",
                subtitle="Use --force to regenerate",
                border_style="yellow",
            )
        )
        return True
    return False


def _display_validation_results(
    console: Console,
    results_rows: list[list[str]],
) -> tuple[bool, bool]:
    """Display validation results line by line and return failure flags.

    Args:
        console: Rich console for output
        results_rows: List of [phase, expected, actual, match] for each validation

    Returns:
        Tuple of (harbor_validation_failed, cc_validation_failed)
    """
    harbor_validation_failed = False
    cc_validation_failed = False

    for phase, expected, actual, match in results_rows:
        ok = match == "Yes"
        style = "green" if ok else "red"
        icon = "✓" if ok else "✗"
        console.print(Text(f"{icon} {phase}: expected {expected}, actual {actual}", style=style))
        if not ok:
            if "Harbor" in phase:
                harbor_validation_failed = True
            if "CC" in phase:
                cc_validation_failed = True

    return harbor_validation_failed, cc_validation_failed


def _build_validation_table(results_rows: list[list[str]]) -> Table | None:
    """Build the validation results table for the summary panel.

    Args:
        results_rows: List of [phase, expected, actual, match] for each validation

    Returns:
        Rich Table with validation results, or None if no results
    """
    if not results_rows:
        return None

    vt = Table(
        title="Validation Results", title_style="bold cyan", header_style="bold", show_lines=False
    )
    vt.add_column("Phase")
    vt.add_column("Expected")
    vt.add_column("Actual")
    vt.add_column("Match?")
    for phase, expected, actual, match in results_rows:
        vt.add_row(
            phase, expected, actual, Text(match, style=("green" if match == "Yes" else "red"))
        )
    return vt


def _handle_validation_failure(
    console: Console,
    harbor_validation_failed: bool,
    cc_validation_failed: bool,
    harbor_actually_ran: bool,
) -> None:
    """Handle validation failures, printing warnings and raising ValidationError if needed.

    Args:
        console: Rich console for output
        harbor_validation_failed: True if any Harbor validation failed
        cc_validation_failed: True if any CC validation failed
        harbor_actually_ran: True if Harbor validations were run (not skipped)

    Raises:
        ValidationError: If validation failed in a way that should stop processing
    """
    # CC failed but Harbor passed - acceptable with warning
    if cc_validation_failed and not harbor_validation_failed and harbor_actually_ran:
        console.print()
        console.print(
            Panel(
                Text(
                    "⚠ CC validation failed, but Harbor validation passed.\nThis is acceptable - Harbor is the authoritative test environment.",
                    style="yellow bold",
                ),
                title="[yellow]CC Validation Warning[/yellow]",
                border_style="yellow",
            )
        )

    # Determine overall validation failure:
    # - Harbor failed (authoritative) → fail
    # - CC failed AND Harbor was skipped → fail (no authoritative validation to fall back on)
    # - CC failed BUT Harbor passed → success (Harbor is authoritative)
    validation_failed = harbor_validation_failed or (
        cc_validation_failed and not harbor_actually_ran
    )

    if validation_failed:
        console.print()
        if cc_validation_failed and not harbor_actually_ran:
            # CC failed and Harbor was skipped - can't verify the task
            console.print(
                Panel(
                    Text(
                        "CC validation failed and Harbor validation was skipped.\nThe task cannot be verified. Run Harbor validation manually or re-run with --validate.",
                        style="red bold",
                    ),
                    title="[red]Validation Failed[/red]",
                    border_style="red",
                )
            )
            raise ValidationError("CC validation failed and Harbor validation was skipped")
        else:
            # Harbor validation failed
            console.print(
                Panel(
                    Text("Validation failed. Review the task files and logs.", style="red bold"),
                    title="[red]Validation Failed[/red]",
                    border_style="red",
                )
            )
            raise ValidationError("Harbor validation failed (NOP or Oracle did not pass)")


def _save_state_record(
    state_dir: Path,
    state_file: Path,
    repo_key: str,
    repo: str,
    pr: int,
    task_id: str,
    task_dir: Path,
) -> None:
    """Save a record of the generated task to the state file.

    This is non-fatal - errors are logged but do not stop execution.
    """
    logger = logging.getLogger("swegen")
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        rec = {
            "key": repo_key,
            "repo": repo,
            "pr": pr,
            "task_id": task_id,
            "harbor": str(task_dir.resolve()),
            "ts": datetime.now(UTC).isoformat(),
        }
        with open(state_file, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except (OSError, IOError, PermissionError, ValueError) as e:
        # Non-fatal; log but continue
        logger.warning(f"Failed to save state record for {repo_key}: {e}")
    except Exception as e:
        # Catch-all for unexpected errors, but still log them
        logger.warning(f"Unexpected error saving state record for {repo_key}: {e}", exc_info=True)


def _display_summary_panel(
    console: Console,
    repo: str,
    pr: int,
    task_id: str,
    task_dir: Path,
    gen_log_path: Path,
    validation_table: Table | None,
) -> None:
    """Display the summary panel with task and PR context."""
    # Count test files
    test_files_count = 0
    try:
        test_files = list((task_dir / "tests").glob("*.py"))
        if not test_files:
            test_files = list((task_dir / "tests").glob("*.js")) + list(
                (task_dir / "tests").glob("*.ts")
            )
        test_files_count = len(test_files)
    except Exception:
        pass

    def _short(sha: Any) -> str:
        s = str(sha or "-")
        return s[:7] if len(s) > 7 else s

    summary = Table(show_header=False, box=None)
    summary.add_row("Repo", Text(repo))
    summary.add_row("PR", Text(str(pr)))
    summary.add_row("Base", Text("-"))  # Not tracked in current implementation
    summary.add_row("Head", Text("-"))  # Not tracked in current implementation
    summary.add_row("Changed files", Text("-"))  # Not tracked in current implementation
    summary.add_row("Test files", Text(str(test_files_count)))
    summary.add_row("Task ID", Text(task_id, style="bold"))
    summary.add_row("Harbor task", Text(str(task_dir)))
    summary.add_row("Debug log", Text(str(gen_log_path)))

    content = Group(summary, validation_table) if validation_table is not None else summary
    console.print(Panel(content, title="Summary", border_style="green"))


def _display_logs_panel(
    console: Console,
    gen_log_path: Path,
    harbor_nop_job_dir: str | None,
    harbor_oracle_job_dir: str | None,
) -> None:
    """Display the logs panel with job directory paths."""
    logs = Table(show_header=False, box=None, expand=True)
    logs.add_column("Item", no_wrap=True)
    logs.add_column("Path", overflow="fold", no_wrap=False)
    logs.add_row("Harbor nop job", Text(harbor_nop_job_dir or "-", overflow="fold"))
    logs.add_row("Harbor oracle job", Text(harbor_oracle_job_dir or "-", overflow="fold"))
    logs.add_row("Generate log", Text(str(gen_log_path)))
    console.print(Panel(logs, title="Logs", border_style="magenta"))


def _display_next_steps_panel(
    console: Console,
    harbor_root: Path,
    task_id: str,
) -> None:
    """Display the next steps panel with recommended actions."""
    steps = Table(show_header=False, box=None)
    steps.add_row("1.", "Confirm validation results match expectations; review Logs for mismatches")
    steps.add_row("2.", "Review generated files (especially Dockerfile)")
    steps.add_row("3.", "Review instruction.md and task.toml")
    steps.add_row("4.", f"Harbor nop: harbor run --agent nop -p {harbor_root} -t {task_id}")
    steps.add_row("5.", f"Harbor oracle: harbor run --agent oracle -p {harbor_root} -t {task_id}")
    steps.add_row(
        "6.", f"Create a pull request including the new task under {harbor_root / task_id}"
    )
    console.print(Panel(steps, title="Next Steps", border_style="cyan"))


def _run_harbor_validations(
    task_id: str,
    harbor_root: Path,
    harbor_jobs: Path,
    console: Console,
    environment: EnvironmentType = EnvironmentType.DOCKER,
) -> tuple[list[list[str]], dict[str, str | None]]:
    """Run Harbor validations (nop + oracle) sequentially.

    Returns:
        Tuple of (results_rows, job_dirs) where:
        - results_rows: List of [phase, expected, actual, match] for each validation
        - job_dirs: Dict mapping agent names to job directory paths (as strings)
    """
    with console.status("Running harbor nop + oracle...", spinner="dots"):
        reward_nop, reward_oracle, job_paths = run_nop_oracle(
            task_id=task_id,
            dataset_path=harbor_root,
            jobs_dir=harbor_jobs,
            environment=environment,
        )

    # Convert paths to strings for job_dirs
    job_dirs = {
        "nop": str(job_paths["nop"]) if job_paths["nop"] else None,
        "oracle": str(job_paths["oracle"]) if job_paths["oracle"] else None,
    }

    # Build results rows
    results_rows = [
        [
            "Harbor nop",
            "reward=0",
            f"reward={reward_nop}" if reward_nop is not None else "reward=unknown",
            "Yes" if reward_nop == 0 else "No",
        ],
        [
            "Harbor oracle",
            "reward=1",
            f"reward={reward_oracle}" if reward_oracle is not None else "reward=unknown",
            "Yes" if reward_oracle == 1 else "No",
        ],
    ]

    return results_rows, job_dirs


def run_reversal(config: CreateConfig) -> None:
    """Convert a merged PR into a Harbor task.

    Args:
        config: Typed configuration with repo, PR number, and options.
    """
    rich_traceback_install(show_locals=False)
    console = Console()
    pipeline = PRToHarborPipeline(repo=config.repo, pr_number=config.pr)
    # Configure file logging for detailed generation logs
    logs_root = Path(config.state_dir) / "logs"
    logs_root.mkdir(parents=True, exist_ok=True)
    gen_log_path = logs_root / f"generate-{pipeline.task_id}.log"
    _configure_file_logger(gen_log_path)
    try:
        # Header
        _display_header(console, pipeline, config.pr)

        # Check for linked issues if required
        linked_issues = _check_linked_issues(console, pipeline, config.pr, config.require_issue)

        # Simple local dedupe: check-before
        # Lowercase repo for consistency (GitHub is case-insensitive, Docker requires lowercase)
        repo_key = f"{pipeline.repo.lower()}#{config.pr}"
        state_dir: Path = config.state_dir or Path(".swegen")
        state_file = state_dir / "create.jsonl"
        if _check_dedupe(console, repo_key, state_file, config.force):
            return

        harbor_root = config.output
        harbor_root.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()

        # CC detects language automatically and fills in the skeleton
        cc_result: ClaudeCodeResult | None = None

        try:
            # try: skeleton generation + CC
            verbose = config.verbose

            # Step 1a: Fetch PR metadata
            console.print("[dim]  → Fetching PR metadata...[/dim]")
            metadata = pipeline.pr_fetcher.fetch_pr_metadata(allow_unmerged=config.allow_unmerged)

            # Step 1b: Clone/update repo cache
            console.print(
                "[dim]  → Cloning/updating repo cache (may take a minute for first clone)...[/dim]"
            )
            repo_cache_dir = config.state_dir / "repos" if config.state_dir else None
            repo_cache = RepoCache(repo_cache_dir)
            repo_path = repo_cache.get_or_clone(
                repo=pipeline.repo,
                head_sha=metadata["head_sha"],
                repo_url=metadata["repo_url"],
            )
            console.print(f"[dim]    Repo at: {repo_path}[/dim]")

            # Step 1c: Generate skeleton files (includes LLM call for PR evaluation)
            console.print("[dim]  → Generating skeleton and evaluating...[/dim]")
            with console.status("Evaluating PR & writing skeleton...", spinner="dots"):
                (
                    task_dir,
                    _,
                    extracted_test_files,
                    task_reference,
                ) = pipeline.generate_task(
                    tasks_root=harbor_root,
                    overwrite=bool(config.force),
                    cache_dir=repo_cache_dir,
                    repo_path=repo_path,
                    metadata=metadata,
                    linked_issues=linked_issues,
                    run_cc=False,  # Run CC separately after skeleton
                    cc_timeout=config.cc_timeout,
                    verbose=verbose,
                    use_cache=config.use_cache,
                    state_dir=config.state_dir,
                    require_minimum_difficulty=config.require_minimum_difficulty,
                    min_source_files=config.min_source_files,
                    max_source_files=config.max_source_files,
                    environment=config.environment.value,
                    generate_task_name=config.generate_name,
                )

            skeleton_secs = time.perf_counter() - t0
            console.print(
                f"[green]✓ Skeleton generated in {skeleton_secs:.1f}s → {task_dir}[/green]"
            )
            console.print(f"  [dim]Test files: {len(extracted_test_files)}[/dim]")

            # Step 2: Run CC "make it work" session
            console.print()
            if task_reference:
                console.print(
                    Rule(
                        Text(
                            f"Claude Code: Adapt from PR #{task_reference.pr_number}",
                            style="bold magenta",
                        )
                    )
                )
                console.print(
                    f"[dim]Reference: {task_reference.task_id} | Timeout: {config.cc_timeout}s | Verbose: {str(verbose).lower()}[/dim]"
                )
            else:
                console.print(Rule(Text("Claude Code", style="bold magenta")))
                console.print(
                    f"[dim]Timeout: {config.cc_timeout}s | Verbose: {str(verbose).lower()}[/dim]"
                )
            console.print()

            cc_result = run_claude_code_session(
                repo=pipeline.repo,
                pr_number=pipeline.pr_number,
                repo_path=repo_path,
                task_dir=task_dir,
                task_id=pipeline.task_id,
                dataset_path=harbor_root,
                test_files=extracted_test_files,
                timeout=config.cc_timeout,
                verbose=verbose,
                reference_task_id=task_reference.task_id if task_reference else None,
                reference_pr=task_reference.pr_number if task_reference else None,
                head_sha=metadata.get("head_sha"),
                environment=config.environment.value,
            )

            gen_secs = time.perf_counter() - t0

            if cc_result and cc_result.success:
                console.print()
                console.print(f"[green]✓ Task generated and validated in {gen_secs:.1f}s[/green]")
            elif cc_result:
                console.print()
                console.print(
                    f"[yellow]⚠ CC session completed in {gen_secs:.1f}s (validation incomplete)[/yellow]"
                )
                if cc_result.error_message:
                    console.print(f"  [red]Error: {cc_result.error_message}[/red]")
            else:
                console.print(
                    f"[green]✓ Skeleton generated in {gen_secs:.1f}s → {task_dir}[/green]"
                )
        except TrivialPRError as e:
            # Skip trivial PRs gracefully
            console.print(
                Panel(
                    Text(str(e), style="yellow"),
                    title="[yellow]Skipped (Trivial PR)[/yellow]",
                    border_style="yellow",
                )
            )
            # Re-raise so calling code can handle appropriately
            raise
        except FileExistsError as e:
            # Task already exists
            console.print(
                Panel(
                    Text(str(e), style="yellow"),
                    title="[yellow]Task Already Exists[/yellow]",
                    border_style="yellow",
                )
            )
            # Re-raise so calling code can handle appropriately
            raise

        # Task ID from generated dir
        task_id = task_dir.name
        harbor_do = not config.no_validate

        # If CC already validated successfully, skip harbor validation
        if cc_result and cc_result.success:
            harbor_do = False
            console.print("[green]✓ Skipping harbor validation (CC already validated)[/green]")

        # Auto-validation unless skipped
        results_rows = []
        # Hold log paths for summary
        harbor_nop_job_dir = harbor_oracle_job_dir = None

        # If CC ran, add its results to the summary
        if cc_result:
            results_rows.append(
                [
                    "CC NOP",
                    "reward=0",
                    "reward=0" if cc_result.nop_passed else "failed",
                    "Yes" if cc_result.nop_passed else "No",
                ]
            )
            results_rows.append(
                [
                    "CC Oracle",
                    "reward=1",
                    "reward=1" if cc_result.oracle_passed else "failed",
                    "Yes" if cc_result.oracle_passed else "No",
                ]
            )

        if harbor_do:
            # Prepare harbor jobs directory
            harbor_jobs = (
                config.state_dir / "harbor-jobs"
                if isinstance(config.state_dir, Path)
                else Path(".swegen") / "harbor-jobs"
            )
            harbor_jobs = harbor_jobs.resolve()
            harbor_jobs.mkdir(parents=True, exist_ok=True)

            # Run validations serially to avoid Docker conflicts
            console.print(Rule(Text("Validations", style="bold blue")))

            validation_results, job_dirs = _run_harbor_validations(
                task_id, harbor_root, harbor_jobs, console, config.environment
            )
            results_rows.extend(validation_results)
            harbor_nop_job_dir = job_dirs.get("nop")
            harbor_oracle_job_dir = job_dirs.get("oracle")

        # Display validation results and check for failures
        harbor_validation_failed, cc_validation_failed = _display_validation_results(
            console, results_rows
        )
        validation_table = _build_validation_table(results_rows)

        # Handle validation failures (may raise ValidationError)
        harbor_actually_ran = any("Harbor" in row[0] for row in results_rows)
        _handle_validation_failure(
            console, harbor_validation_failed, cc_validation_failed, harbor_actually_ran
        )

        # Save state record (non-fatal if fails)
        _save_state_record(
            state_dir, state_file, repo_key, pipeline.repo, config.pr, task_id, task_dir
        )

        # Display final panels
        _display_summary_panel(
            console, pipeline.repo, config.pr, task_id, task_dir, gen_log_path, validation_table
        )
        _display_logs_panel(
            console,
            gen_log_path,
            harbor_nop_job_dir,
            harbor_oracle_job_dir,
        )
        _display_next_steps_panel(console, harbor_root, task_id)
    except (TrivialPRError, MissingIssueError, ValidationError, FileExistsError):
        # Re-raise these exceptions so caller can handle them
        raise
    except Exception as e:
        # Unexpected errors - print and re-raise for caller to handle
        console.print(Panel(Text(str(e)), title="Error", border_style="red"))
        traceback.print_exc()
        raise


def _run_harbor_with_status(
    task_id: str,
    harbor_root: Path,
    harbor_jobs_parent: Path,
    console: Console,
    phase: str,
    delete_after: bool = True,
    environment: EnvironmentType = EnvironmentType.DOCKER,
) -> Path | None:
    """Run harbor with a rich console status spinner.

    Thin wrapper around run_harbor_agent that adds console status feedback.

    Args:
        task_id: Task identifier
        harbor_root: Harbor dataset root path
        harbor_jobs_parent: Jobs directory path
        console: Rich console for output
        phase: Agent name ("nop" or "oracle")
        delete_after: If True, delete Docker image after run (default: True)
        environment: Environment type (docker, daytona, e2b, modal, runloop, gke)
    """
    with console.status(f"Running harbor {phase}...", spinner="dots"):
        _, job_result = run_harbor_agent(
            task_id=task_id,
            dataset_path=harbor_root,
            jobs_dir=harbor_jobs_parent,
            agent=phase,
            capture_output=True,
            delete_after=delete_after,
            environment=environment,
        )
    return job_result


def _configure_file_logger(path: Path) -> None:
    logger = logging.getLogger("swegen")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # Clear existing handlers
    logger.handlers = []
    fh = logging.FileHandler(path)
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
