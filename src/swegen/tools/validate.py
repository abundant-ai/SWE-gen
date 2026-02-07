from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from harbor.models.environment_type import EnvironmentType
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from .harbor_runner import parse_harbor_outcome, run_harbor_agent

DOCKER_CLEANUP_CMD = "docker system prune -af"


@dataclass
class ValidateArgs:
    path: Path
    task: str | None
    jobs_dir: Path
    agent: str  # "both" | "nop" | "oracle"
    timeout_multiplier: float | None = None
    verbose: bool = False
    quiet: bool = False
    environment: EnvironmentType = EnvironmentType.DOCKER
    max_parallel: int = 8
    show_passed: bool = False
    output_file: Path | None = None  # Write results to file as they complete
    docker_prune_batch: int = 5  # Run docker cleanup after every N tasks (0 to disable)


@dataclass
class ValidationResult:
    """Result of validating a single task."""

    task_id: str
    nop_reward: float | None
    oracle_reward: float | None
    nop_exit_code: int
    oracle_exit_code: int
    ruff_passed: bool | None
    ruff_summary: str | None
    passed: bool
    error: str | None = None


def run_validate(args: ValidateArgs) -> None:
    """Main entry point - routes to single or batch validation."""
    dataset_path, task_id, task_dir = _resolve_paths(args)

    if task_id is None:
        _run_batch_mode(args, dataset_path)
    else:
        _run_single_mode(args, dataset_path, task_id, task_dir)


def _resolve_paths(args: ValidateArgs) -> tuple[Path, str | None, Path | None]:
    """Resolve paths and determine if single or batch mode.

    Returns: (dataset_path, task_id, task_dir)
             task_id/task_dir are None for batch mode
    """
    path = args.path.resolve()

    if args.task:
        # Explicit task ID: single mode
        return path, args.task, path / args.task

    if path.is_dir() and (path / "tests" / "test.sh").exists():
        # Path is a task directory: single mode
        return path.parent, path.name, path

    if path.is_dir():
        # Check if directory contains tasks: batch mode
        tasks = [d for d in path.iterdir() if d.is_dir() and (d / "tests" / "test.sh").exists()]
        if tasks:
            return path, None, None
        raise SystemExit(
            f"No tasks found in directory: {path}\nExpected directories with tests/test.sh"
        )

    raise SystemExit(
        "Path must be:\n"
        "  1. A task directory (containing tests/test.sh), or\n"
        "  2. A dataset directory with multiple tasks"
    )


# ============================================================================
# SINGLE TASK MODE
# ============================================================================


def _run_single_mode(args: ValidateArgs, dataset_path: Path, task_id: str, task_dir: Path) -> None:
    """Validate a single task with traditional output."""
    console = Console()
    jobs_dir = args.jobs_dir.resolve()
    jobs_dir.mkdir(parents=True, exist_ok=True)

    # Run regular validation
    print("[validate] Running regular validation...")
    nop_reward, oracle_reward = _run_agents(
        task_id, dataset_path, jobs_dir, args.agent, args.timeout_multiplier, args.environment
    )

    ruff_passed, ruff_output = _run_ruff_check(task_dir)
    ruff_ok = True if ruff_passed is None else ruff_passed

    agent_passed = _check_passed(args.agent, nop_reward, oracle_reward)
    overall_passed = agent_passed and ruff_ok

    if args.agent in ("nop", "both"):
        nop_cell = f"{'PASS' if nop_reward == 0 else 'FAIL'} ({nop_reward})"
    else:
        nop_cell = "—"

    if args.agent in ("oracle", "both"):
        oracle_cell = f"{'PASS' if oracle_reward == 1 else 'FAIL'} ({oracle_reward})"
    else:
        oracle_cell = "—"

    if ruff_passed is None:
        ruff_cell = "SKIP"
    elif ruff_passed:
        ruff_cell = "PASS"
    else:
        summary = _ruff_summary(ruff_output)
        ruff_cell = f"FAIL ({summary})" if summary else "FAIL"

    summary = Table(title="Validation Summary", title_style="bold cyan", show_lines=True)
    summary.add_column("NOP", justify="center")
    summary.add_column("Oracle", justify="center")
    summary.add_column("Ruff", justify="center")
    summary.add_row(nop_cell, oracle_cell, ruff_cell)

    console.print("\n")
    console.print(summary)

    if ruff_passed is False and ruff_output:
        print("\n[validate] Ruff output:")
        print(ruff_output.rstrip())

    if not overall_passed:
        sys.exit(1)


def _run_agents(
    task_id: str,
    dataset_path: Path,
    jobs_dir: Path,
    agent: str,
    timeout_multiplier: float | None,
    environment: EnvironmentType = EnvironmentType.DOCKER,
) -> tuple[float | None, float | None]:
    """Run NOP and/or Oracle agents, return (nop_reward, oracle_reward)."""
    nop_reward = oracle_reward = None

    if agent in ("nop", "both"):
        # When running both, keep image for nop so oracle can reuse it
        delete_after = agent == "nop"  # Only delete if ONLY running nop
        code, job_result = run_harbor_agent(
            task_id,
            dataset_path,
            jobs_dir,
            "nop",
            timeout_multiplier,
            delete_after=delete_after,
            environment=environment,
        )
        nop_reward = parse_harbor_outcome(job_result).reward
        print(f"[validate] nop exit={code}, reward={nop_reward}")

    if agent in ("oracle", "both"):
        # Oracle always deletes (cleanup)
        code, job_result = run_harbor_agent(
            task_id,
            dataset_path,
            jobs_dir,
            "oracle",
            timeout_multiplier,
            delete_after=True,
            environment=environment,
        )
        oracle_reward = parse_harbor_outcome(job_result).reward
        print(f"[validate] oracle exit={code}, reward={oracle_reward}")

    return nop_reward, oracle_reward


# ============================================================================
# BATCH MODE
# ============================================================================


def _run_batch_mode(args: ValidateArgs, dataset_path: Path) -> None:
    """Validate all tasks in parallel with clean output."""
    console = Console()
    jobs_dir = args.jobs_dir.resolve()
    jobs_dir.mkdir(parents=True, exist_ok=True)

    # Find tasks
    task_dirs = [
        d for d in dataset_path.iterdir() if d.is_dir() and (d / "tests" / "test.sh").exists()
    ]
    if not task_dirs:
        console.print("[yellow]No tasks found[/yellow]")
        return

    console.print(f"[blue]Found {len(task_dirs)} task(s) to validate[/blue]")
    console.print(f"[blue]Parallel: {args.max_parallel} | Agent: {args.agent}[/blue]")
    if args.output_file:
        console.print(f"[blue]Output: {args.output_file}[/blue]")
    # Show docker prune setting for local docker
    if args.environment == EnvironmentType.DOCKER and args.docker_prune_batch > 0:
        console.print(f"[blue]Docker prune: every {args.docker_prune_batch} tasks[/blue]")
    console.print()

    # Run validations
    results = asyncio.run(
        _validate_batch(
            task_dirs,
            dataset_path,
            jobs_dir,
            args.agent,
            args.max_parallel,
            args.timeout_multiplier,
            args.environment,
            console,
            args.output_file,
            args.docker_prune_batch,
        )
    )

    # Print results
    _print_results(results, args.agent, args.show_passed, console)

    # Exit with failure if any tasks failed
    if not all(r.passed for r in results):
        sys.exit(1)


async def _validate_batch(
    task_dirs: list[Path],
    dataset_path: Path,
    jobs_dir: Path,
    agent: str,
    max_parallel: int,
    timeout_multiplier: float | None,
    environment: EnvironmentType,
    console: Console,
    output_file: Path | None = None,
    docker_prune_batch: int = 5,
) -> list[ValidationResult]:
    """Run validations in parallel with progress bar."""
    semaphore = asyncio.Semaphore(max_parallel)

    # Track completed count for docker pruning
    completed_count = 0
    prune_lock = asyncio.Lock()

    # Lock and file handle for sequential writes
    write_lock = asyncio.Lock()
    file_handle = None
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        file_handle = open(output_file, "w")
        # Write header
        file_handle.write(f"# Validation results - {len(task_dirs)} tasks\n")
        file_handle.write(
            "# Format: TASK_ID: NOP=<reward> ORACLE=<reward> RUFF=<PASS|FAIL|SKIP> <STATUS>\n\n"
        )
        file_handle.flush()

    async def write_result(result: ValidationResult) -> None:
        """Write a single result to file (thread-safe)."""
        if file_handle is None:
            return
        async with write_lock:
            line = _format_result_line(result, agent)
            file_handle.write(line + "\n")
            file_handle.flush()  # Ensure immediate write to disk

    async def validate_one(task_dir: Path) -> ValidationResult:
        async with semaphore:
            try:
                nop_reward = oracle_reward = None
                nop_code = oracle_code = 0
                ruff_passed, ruff_output = await asyncio.to_thread(_run_ruff_check, task_dir)
                summary = _ruff_summary(ruff_output)

                # Run NOP (capture_output=True to suppress Harbor's verbose output)
                if agent in ("nop", "both"):
                    # When running both, keep image for nop so oracle can reuse it
                    delete_after = agent == "nop"  # Only delete if ONLY running nop
                    nop_code, job = await asyncio.to_thread(
                        run_harbor_agent,
                        task_dir.name,
                        dataset_path,
                        jobs_dir,
                        "nop",
                        timeout_multiplier,
                        True,
                        delete_after,
                        environment,
                    )
                    nop_reward = parse_harbor_outcome(job).reward

                # Run Oracle (capture_output=True to suppress Harbor's verbose output)
                if agent in ("oracle", "both"):
                    # Oracle always deletes (cleanup)
                    oracle_code, job = await asyncio.to_thread(
                        run_harbor_agent,
                        task_dir.name,
                        dataset_path,
                        jobs_dir,
                        "oracle",
                        timeout_multiplier,
                        True,
                        True,
                        environment,
                    )
                    oracle_reward = parse_harbor_outcome(job).reward

                # Determine pass/fail
                agent_passed = _check_passed(agent, nop_reward, oracle_reward)
                ruff_ok = True if ruff_passed is None else ruff_passed
                passed = agent_passed and ruff_ok

                result = ValidationResult(
                    task_id=task_dir.name,
                    nop_reward=nop_reward,
                    oracle_reward=oracle_reward,
                    nop_exit_code=nop_code,
                    oracle_exit_code=oracle_code,
                    ruff_passed=ruff_passed,
                    ruff_summary=summary,
                    passed=passed,
                )
            except Exception as e:
                result = ValidationResult(
                    task_id=task_dir.name,
                    nop_reward=None,
                    oracle_reward=None,
                    nop_exit_code=-1,
                    oracle_exit_code=-1,
                    ruff_passed=None,
                    ruff_summary=None,
                    passed=False,
                    error=str(e),
                )

            # Write to file immediately
            await write_result(result)
            return result

    async def maybe_prune_docker(count: int) -> None:
        """Run docker prune if conditions are met (local docker only, every N tasks)."""
        if environment != EnvironmentType.DOCKER:
            return
        if docker_prune_batch <= 0:
            return
        if count % docker_prune_batch != 0:
            return

        async with prune_lock:
            await asyncio.to_thread(_prune_docker, console)

    # Run with progress bar
    results = []
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task_prog = progress.add_task("[cyan]Validating tasks...", total=len(task_dirs))

            for coro in asyncio.as_completed([validate_one(d) for d in task_dirs]):
                results.append(await coro)
                progress.update(task_prog, advance=1)

                # Docker cleanup after batch (local docker only)
                completed_count = len(results)
                await maybe_prune_docker(completed_count)
    finally:
        if file_handle:
            # Write summary at end
            passed = sum(1 for r in results if r.passed and not r.error)
            failed = sum(1 for r in results if not r.passed and not r.error)
            errors = sum(1 for r in results if r.error)
            file_handle.write(f"\n# Summary: {passed} passed, {failed} failed, {errors} errors\n")
            file_handle.close()

    return results


def _format_result_line(result: ValidationResult, agent: str) -> str:
    """Format a single result as a text line."""
    parts = [result.task_id + ":"]

    if agent in ("nop", "both"):
        if result.nop_reward is not None:
            parts.append(f"NOP={result.nop_reward}")
        else:
            parts.append("NOP=ERROR")

    if agent in ("oracle", "both"):
        if result.oracle_reward is not None:
            parts.append(f"ORACLE={result.oracle_reward}")
        else:
            parts.append("ORACLE=ERROR")

    if result.ruff_passed is True:
        parts.append("RUFF=PASS")
    elif result.ruff_passed is False:
        parts.append("RUFF=FAIL")
    else:
        parts.append("RUFF=SKIP")

    if result.error:
        parts.append(f"ERROR: {result.error}")
    elif result.passed:
        parts.append("PASS")
    else:
        parts.append("FAIL")

    return " ".join(parts)


def _check_passed(agent: str, nop_reward: float | None, oracle_reward: float | None) -> bool:
    """Check if validation passed based on agent type and rewards."""
    if agent == "both":
        return nop_reward == 0 and oracle_reward == 1
    elif agent == "nop":
        return nop_reward == 0
    elif agent == "oracle":
        return oracle_reward == 1
    return False


def _print_results(
    results: list[ValidationResult], agent: str, show_passed: bool, console: Console
) -> None:
    """Print results table (failures only by default) and summary."""
    passed = [r for r in results if r.passed and not r.error]
    failed = [r for r in results if not r.passed and not r.error]
    errors = [r for r in results if r.error]

    # Show table if there are failures/errors or if show_passed requested
    if failed or errors or show_passed:
        table = Table(
            title="Validation Failures" if not show_passed else "Validation Results",
            title_style="bold cyan",
            show_lines=True,
        )
        table.add_column("Task ID", style="cyan")

        if agent in ("nop", "both"):
            table.add_column("NOP", justify="center")
        if agent in ("oracle", "both"):
            table.add_column("Oracle", justify="center")

        table.add_column("Ruff", justify="center")
        table.add_column("Status", justify="center")
        table.add_column("Notes")

        # Show errors, then failures, then passed (if requested)
        for result in sorted(
            errors + failed + (passed if show_passed else []), key=lambda r: r.task_id
        ):
            _add_result_row(table, result, agent)

        console.print("\n")
        console.print(table)

    # Always show summary
    console.print("\n[bold]Summary:[/bold]")
    console.print(f"  ✅ Passed: {len(passed)}")
    console.print(f"  ❌ Failed: {len(failed)}")
    console.print(f"  ⚠️  Errors: {len(errors)}")
    console.print(f"  📊 Total: {len(results)}")

    if not failed and not errors:
        console.print(f"\n[bold green]🎉 All {len(passed)} task(s) passed validation![/bold green]")


def _add_result_row(table: Table, result: ValidationResult, agent: str) -> None:
    """Add a single result row to the table."""
    row = [result.task_id]

    if result.error:
        # Error row
        if agent in ("nop", "both"):
            row.append("?")
        if agent in ("oracle", "both"):
            row.append("?")
        row.append("?")
        row.extend(["❌ ERROR", result.error])
        table.add_row(*row, style="red")
        return

    if result.passed:
        # Passed row (only shown if show_passed=True)
        if agent in ("nop", "both"):
            row.append(f"✓ ({result.nop_reward})" if result.nop_reward is not None else "—")
        if agent in ("oracle", "both"):
            row.append(f"✓ ({result.oracle_reward})" if result.oracle_reward is not None else "—")
        if result.ruff_passed is True:
            row.append("✓")
        elif result.ruff_passed is False:
            row.append("✗")
        else:
            row.append("—")
        row.extend(["✅ PASS", ""])
        table.add_row(*row, style="green")
        return

    # Failed row
    notes = []

    if agent in ("nop", "both"):
        if result.nop_reward is not None:
            row.append(f"{'✓' if result.nop_reward == 0 else '✗'} ({result.nop_reward})")
            if result.nop_reward != 0:
                notes.append(f"NOP expected 0, got {result.nop_reward}")
        else:
            row.append("—")

    if agent in ("oracle", "both"):
        if result.oracle_reward is not None:
            row.append(f"{'✓' if result.oracle_reward == 1 else '✗'} ({result.oracle_reward})")
            if result.oracle_reward != 1:
                notes.append(f"Oracle expected 1, got {result.oracle_reward}")
        else:
            row.append("—")

    if result.ruff_passed is True:
        row.append("✓")
    elif result.ruff_passed is False:
        row.append("✗")
        if result.ruff_summary:
            notes.append(f"Ruff: {result.ruff_summary}")
    else:
        row.append("—")

    row.extend(["❌ FAIL", "; ".join(notes)])
    table.add_row(*row, style="red")


def _prune_docker(console: Console) -> None:
    """Run docker cleanup to free disk space."""
    if shutil.which("docker") is None:
        console.print("[yellow]Skipping docker prune (docker binary not found in PATH).[/yellow]")
        return

    console.print(
        Panel(
            f"Running docker cleanup: {DOCKER_CLEANUP_CMD}",
            title="Disk cleanup",
            border_style="yellow",
        )
    )

    try:
        result = subprocess.run(
            DOCKER_CLEANUP_CMD,
            shell=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            console.print("[green]Docker cleanup completed[/green]")
        else:
            console.print(f"[yellow]Docker cleanup returned code {result.returncode}[/yellow]")
    except subprocess.TimeoutExpired:
        console.print("[yellow]Docker cleanup timed out after 10 minutes[/yellow]")
    except Exception as e:
        console.print(f"[yellow]Docker cleanup failed: {e}[/yellow]")


def _run_ruff_check(task_dir: Path) -> tuple[bool | None, str | None]:
    """Run `ruff check` on the task directory (best-effort)."""
    cmd: list[str]
    if shutil.which("ruff") is not None:
        cmd = ["ruff"]
    else:
        cmd = [sys.executable, "-m", "ruff"]

    try:
        try:
            path_arg = str(task_dir.relative_to(Path.cwd()))
        except ValueError:
            path_arg = str(task_dir)

        proc = subprocess.run(
            cmd + ["check", path_arg],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None, None

    output = (proc.stdout or "") + (proc.stderr or "")
    output = _strip_ansi(output)
    if proc.returncode != 0 and "No module named ruff" in output:
        return None, None
    return proc.returncode == 0, output or None


def _ruff_summary(output: str | None) -> str:
    """Compact single-line summary for ruff output."""
    if not output:
        return ""

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("Found ") and "error" in line:
            return line
    if lines:
        return lines[-1]
    return ""


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI color escape sequences from text."""
    return _ANSI_ESCAPE_RE.sub("", text)
