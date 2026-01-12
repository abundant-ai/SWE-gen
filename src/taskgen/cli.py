from __future__ import annotations

from importlib.metadata import PackageNotFoundError as _PkgNotFound
from importlib.metadata import version as _pkg_version
from pathlib import Path

import typer
import json
from dotenv import load_dotenv
from harbor.models.environment_type import EnvironmentType
from rich.console import Console

from taskgen.config import CreateConfig, FarmConfig
from taskgen.create import MissingIssueError, TrivialPRError
from taskgen.create.create import run_reversal
from taskgen.farm import StreamFarmer
from taskgen.analyze import AnalyzeArgs, run_analyze, TrialClassifier, write_trial_analysis_files
from taskgen.tools.clean import run_clean
from taskgen.tools.validate import ValidateArgs, run_validate
from taskgen.tools.validate_utils import ValidationError

load_dotenv()

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Task generation CLI")


@app.callback(invoke_without_command=True)
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show taskgen version and exit",
        is_eager=True,
    ),
) -> None:
    if version:
        try:
            typer.echo(f"taskgen {_pkg_version('taskgen')}")
        except _PkgNotFound:
            typer.echo("taskgen (version unknown)")
        raise typer.Exit()


create_app = typer.Typer(
    no_args_is_help=True,
    invoke_without_command=True,
    add_completion=False,
    help="Create a Harbor task from a merged PR and validate",
)


@create_app.callback()
def create_cmd(
    repo: str = typer.Option(..., help="GitHub repository (owner/repo or URL)"),
    pr: int = typer.Option(..., help="PR number"),
    output: Path = typer.Option(Path("tasks"), help="Output root", show_default=True),
    cc_timeout: int = typer.Option(
        3200, help="Timeout for CC session in seconds (~53 min default)", show_default=True
    ),
    validate: bool = typer.Option(
        True, help="Run Harbor validations; --no-validate skips validation"
    ),
    force: bool = typer.Option(False, help="Bypass local dedupe and regenerate"),
    state_dir: Path = typer.Option(
        Path(".state"), help="Local dedupe state dir", show_default=True
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Disable reusing cached Dockerfiles/test.sh from previous tasks"
    ),
    require_minimum_difficulty: bool = typer.Option(
        True,
        help="Require minimum difficulty (3+ source files); --no-require-minimum-difficulty to skip this check",
    ),
    min_source_files: int = typer.Option(
        3, help="Minimum number of source files required (tests excluded)", show_default=True
    ),
    max_source_files: int = typer.Option(
        10,
        help="Maximum number of source files to avoid large refactors (tests excluded)",
        show_default=True,
    ),
    require_issue: bool = typer.Option(
        True,
        help="Require PR to have a linked issue (higher quality instructions); --no-require-issue uses PR body/title instead",
    ),
    allow_unmerged: bool = typer.Option(
        False,
        help="Allow processing unmerged PRs (for testing/preview); --allow-unmerged to enable",
    ),
    environment: str = typer.Option(
        "docker",
        "-e",
        "--env",
        help="Environment type for Harbor runs (docker|daytona|e2b|modal|runloop|gke)",
        show_default=True,
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Increase output verbosity"),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="Reduce output verbosity"),
) -> None:
    config = CreateConfig(
        repo=repo,
        pr=pr,
        output=output,
        cc_timeout=cc_timeout,
        validate=validate,
        force=force,
        state_dir=state_dir,
        use_cache=not no_cache,
        require_minimum_difficulty=require_minimum_difficulty,
        min_source_files=min_source_files,
        max_source_files=max_source_files,
        require_issue=require_issue,
        allow_unmerged=allow_unmerged,
        environment=EnvironmentType(environment),
        verbose=verbose,
        quiet=quiet,
    )
    try:
        run_reversal(config)
    except (TrivialPRError, MissingIssueError, ValidationError, FileExistsError) as err:
        # These exceptions have already displayed user-friendly messages
        # Exit with error code but don't show traceback
        raise SystemExit(1) from err


app.add_typer(create_app, name="create")


@app.command(
    help="Remove local artifacts: .state/* runs/jobs/logs; options for ledgers/cache/tasks"
)
def clean(
    state_dir: Path = typer.Option(Path(".state"), help="State dir to clean", show_default=True),
    output: Path = typer.Option(Path("tasks"), help="Tasks output root", show_default=True),
    all: bool = typer.Option(False, "--all", help="Also remove ledgers, cache, and tasks outputs"),
    ledgers: bool = typer.Option(False, help="Also remove .state/create.jsonl"),
    cache: bool = typer.Option(False, help="Also remove .state/cache"),
    tasks: bool = typer.Option(False, help="Also remove tasks/"),
    dry_run: bool = typer.Option(False, help="Print what would be removed without deleting"),
) -> None:
    run_clean(
        state_dir=state_dir,
        output_root=output,
        all_=all,
        ledgers=ledgers,
        cache=cache,
        tasks=tasks,
        dry_run=dry_run,
    )


@app.command(help="Validate an existing Harbor task by running NOP and ORACLE")
def validate(
    path: Path = typer.Argument(
        ...,
        help="Path to Harbor dataset root, specific task directory, or task ID when used with dataset root",
    ),
    task: str
    | None = typer.Option(None, "--task", "-t", help="Task ID when --path points to dataset root"),
    agent: str = typer.Option("both", help="Agent to run: both|nop|oracle", show_default=True),
    jobs_dir: Path = typer.Option(
        Path(".state/harbor-jobs"),
        help="Directory to store Harbor job artifacts",
        show_default=True,
    ),
    timeout_multiplier: float
    | None = typer.Option(None, help="Multiply default timeouts (e.g., 3.0)"),
    environment: str = typer.Option(
        "docker",
        "-e",
        "--env",
        help="Environment type for Harbor runs (docker|daytona|e2b|modal|runloop|gke)",
        show_default=True,
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Increase output verbosity"),
    quiet: bool = typer.Option(False, "-q", "--quiet", help="Reduce output verbosity"),
    max_parallel: int = typer.Option(
        8, help="Maximum number of parallel validations (batch mode only)", show_default=True
    ),
    show_passed: bool = typer.Option(
        False,
        "--show-passed",
        help="Show passed tasks in output (batch mode: default shows only failures)",
    ),
    output: Path
    | None = typer.Option(
        None, "-o", "--output", help="Write results to file as they complete (batch mode only)"
    ),
) -> None:
    if agent not in ("both", "nop", "oracle"):
        raise typer.BadParameter("agent must be one of: both, nop, oracle")
    run_validate(
        ValidateArgs(
            path=path,
            task=task,
            jobs_dir=jobs_dir,
            agent=agent,
            timeout_multiplier=timeout_multiplier,
            verbose=verbose,
            quiet=quiet,
            environment=EnvironmentType(environment),
            max_parallel=max_parallel,
            show_passed=show_passed,
            output_file=output,
        )
    )


analyze_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Analyze task quality or classify trial outcomes",
)


@analyze_app.command(name="task", help="Analyze a task by running agent trials and classifying outcomes")
def analyze_task(
    path: Path = typer.Argument(..., help="Path to the task directory to analyze"),
    agent: str = typer.Option(
        "claude-code", "-a", "--agent", help="Agent to run trials with", show_default=True
    ),
    model: str = typer.Option(
        "anthropic/claude-sonnet-4-5",
        "-m",
        "--model",
        help="Model to use for agent trials",
        show_default=True,
    ),
    n_trials: int = typer.Option(
        3, "-k", "--n-trials", help="Number of trials to run", show_default=True
    ),
    n_concurrent: int = typer.Option(
        3, "-n", "--n-concurrent", help="Number of concurrent trials (1=sequential, 3-5 recommended)", show_default=True
    ),
    jobs_dir: Path = typer.Option(
        Path(".state/analyze-jobs"),
        "--jobs-dir",
        help="Directory to store job artifacts",
        show_default=True,
    ),
    skip_quality_check: bool = typer.Option(
        False, "--skip-quality-check", help="Skip static quality check"
    ),
    skip_baseline: bool = typer.Option(
        False, "--skip-baseline", help="Skip baseline validation (nop/oracle)"
    ),
    skip_classify: bool = typer.Option(
        False, "--skip-classify", help="Skip LLM classification of trial outcomes"
    ),
    analysis_model: str = typer.Option(
        "claude-sonnet-4-5",
        "--analysis-model",
        help="Model for Claude Code classification",
        show_default=True,
    ),
    timeout_multiplier: float = typer.Option(
        1.0, "--timeout-multiplier", help="Multiply default timeouts", show_default=True
    ),
    environment: str = typer.Option(
        "docker",
        "-e",
        "--env",
        help="Environment type for Harbor runs (docker|daytona|e2b|modal|runloop|gke)",
        show_default=True,
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Increase output verbosity"),
    classification_timeout: int = typer.Option(
        300,
        "--classification-timeout",
        help="Timeout per trial classification in seconds",
        show_default=True,
    ),
    verdict_timeout: int = typer.Option(
        180,
        "--verdict-timeout",
        help="Timeout for verdict synthesis in seconds",
        show_default=True,
    ),
    save_to_dir: bool = typer.Option(
        False,
        "--save-to-dir",
        help="Write trajectory-analysis.{md,json} to each trial directory",
    ),
) -> None:
    """
    Analyze a Harbor task to determine if it's well-specified.

    This command classifies trial outcomes to identify TASK PROBLEMS vs AGENT PROBLEMS:

    1. Static quality check (Harbor's tasks check)
    2. Baseline validation (nop should fail, oracle should pass)
    3. Run N agent trials (default: 3 with Claude Code)
    4. Classify each trial outcome:
       - GOOD_SUCCESS: Agent solved it correctly
       - BAD_SUCCESS: Agent cheated or tests too permissive
       - GOOD_FAILURE: Agent failed due to its own limitations
       - BAD_FAILURE: Agent failed due to task issues
       - HARNESS_ERROR: Infrastructure problem
    5. Compute task verdict with recommendations

    The goal is to identify tasks that need fixing before release.

    Flags match Harbor CLI conventions:
        -k / --n-trials: Total number of trials to run
        -n / --n-concurrent: Number of trials to run concurrently (parallelism)

    Examples:
        # Sequential (default)
        taskgen analyze task tasks/my-task -k 5

        # Parallel (3 trials at once)
        taskgen analyze task tasks/my-task -k 10 -n 3
    """
    run_analyze(
        AnalyzeArgs(
            task_path=path,
            agent=agent,
            model=model,
            n_trials=n_trials,
            n_concurrent=n_concurrent,
            jobs_dir=jobs_dir,
            skip_quality_check=skip_quality_check,
            skip_baseline=skip_baseline,
            skip_classify=skip_classify,
            analysis_model=analysis_model,
            environment=environment,
            timeout_multiplier=timeout_multiplier,
            verbose=verbose,
            classification_timeout=classification_timeout,
            verdict_timeout=verdict_timeout,
            save_to_dir=save_to_dir,
        )
    )


app.add_typer(analyze_app, name="analyze")


@app.command(help="Continuous PR farming - stream through entire PR history")
def farm(
    repo: str = typer.Argument(
        ..., help="GitHub repository in owner/name format (e.g., fastapi/fastapi)"
    ),
    output: Path = typer.Option(
        Path("tasks"), help="Output directory for generated tasks", show_default=True
    ),
    state_dir: Path = typer.Option(
        Path(".state"), help="State directory for cache/logs", show_default=True
    ),
    force: bool = typer.Option(True, help="Regenerate even if task already exists"),
    timeout: int = typer.Option(300, help="Timeout per PR in seconds", show_default=True),
    cc_timeout: int = typer.Option(
        3200, help="Timeout for Claude Code session in seconds (~53 min default)", show_default=True
    ),
    api_delay: float = typer.Option(
        0.5, help="Delay between GitHub API calls in seconds", show_default=True
    ),
    task_delay: int = typer.Option(60, help="Delay between tasks in seconds", show_default=True),
    reset: bool = typer.Option(False, "--reset", help="Reset state and start from beginning"),
    resume_from: str
    | None = typer.Option(
        None, help="Resume from date (e.g., '2024-01-15' or '2024-01-15T10:30:00Z')"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Only show what would run (no task generation)"
    ),
    docker_prune_batch: int = typer.Option(
        5, help="Run docker cleanup after every N PRs (0 to disable)", show_default=True
    ),
    skip_list: str
    | None = typer.Option(None, help="Path to file with task IDs to skip (one per line)"),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Disable reusing cached Dockerfiles/test.sh"
    ),
    require_minimum_difficulty: bool = typer.Option(
        True,
        help="Require minimum difficulty (3+ source files); --no-require-minimum-difficulty to skip this check",
    ),
    min_source_files: int = typer.Option(
        3, help="Minimum number of source files required (tests excluded)", show_default=True
    ),
    max_source_files: int = typer.Option(
        10,
        help="Maximum number of source files to avoid large refactors (tests excluded)",
        show_default=True,
    ),
    environment: str = typer.Option(
        "docker",
        "-e",
        "--env",
        help="Environment type for Harbor runs (docker|daytona|e2b|modal|runloop|gke)",
        show_default=True,
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable verbose output"),
    issue_only: bool = typer.Option(
        True,
        "--issue-only",
        help="Only process PRs with linked issues (higher quality instructions)",
    ),
    validate: bool = typer.Option(
        True, help="Run Harbor validation after CC; --no-validate to skip"
    ),
) -> None:
    """
    Continuously process merged GitHub PRs and convert them to Harbor tasks.
    Streams PRs page-by-page, processes them immediately, and maintains state for resumable operation.
    Uses a language-agnostic pipeline that works for any repository.
    """
    config = FarmConfig(
        repo=repo,
        output=output,
        state_dir=state_dir,
        force=force,
        timeout=timeout,
        cc_timeout=cc_timeout,
        api_delay=api_delay,
        task_delay=task_delay,
        reset=reset,
        resume_from=resume_from,
        dry_run=dry_run,
        docker_prune_batch=docker_prune_batch,
        skip_list=skip_list,
        no_cache=no_cache,
        require_minimum_difficulty=require_minimum_difficulty,
        min_source_files=min_source_files,
        max_source_files=max_source_files,
        environment=EnvironmentType(environment),
        verbose=verbose,
        issue_only=issue_only,
        validate=validate,
    )

    console = Console()
    farmer = StreamFarmer(config.repo, config, console)
    exit_code = farmer.run()
    raise typer.Exit(code=exit_code)
