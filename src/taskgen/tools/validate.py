from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from harbor.models.environment_type import EnvironmentType
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from .harbor_runner import run_harbor_agent, parse_harbor_reward
from .network_isolation import network_isolation


@dataclass
class ValidateArgs:
    path: Path
    task: Optional[str]
    jobs_dir: Path
    agent: str  # "both" | "nop" | "oracle"
    timeout_multiplier: Optional[float] = None
    verbose: bool = False
    quiet: bool = False
    network_isolated: bool = False
    environment: EnvironmentType = EnvironmentType.DOCKER
    max_parallel: int = 8
    show_passed: bool = False
    output_file: Optional[Path] = None  # Write results to file as they complete


@dataclass
class ValidationResult:
    """Result of validating a single task."""
    task_id: str
    nop_reward: Optional[float]
    oracle_reward: Optional[float]
    nop_exit_code: int
    oracle_exit_code: int
    passed: bool
    error: Optional[str] = None


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
        raise SystemExit(f"No tasks found in directory: {path}\nExpected directories with tests/test.sh")
    
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
    jobs_dir = args.jobs_dir.resolve()
    jobs_dir.mkdir(parents=True, exist_ok=True)

    # Run regular validation
    print("[validate] Running regular validation...")
    nop_reward, oracle_reward = _run_agents(task_id, dataset_path, jobs_dir, args.agent, args.timeout_multiplier, args.environment)
    
    # Check results
    if args.agent == "both":
        if nop_reward != 0 or oracle_reward != 1:
            print("\n[validate] FAILED: Harbor validation did not meet expectations")
            print(f"  NOP: expected reward=0, got reward={nop_reward}")
            print(f"  ORACLE: expected reward=1, got reward={oracle_reward}")
            sys.exit(1)
        else:
            print("\n[validate] PASSED: Harbor validation met expectations")
            print(f"  NOP: reward={nop_reward} ✓")
            print(f"  ORACLE: reward={oracle_reward} ✓")

    # Network-isolated validation if requested
    if args.network_isolated:
        _run_network_isolated(args, task_id, dataset_path, task_dir, jobs_dir)


def _run_agents(
    task_id: str,
    dataset_path: Path,
    jobs_dir: Path,
    agent: str,
    timeout_multiplier: Optional[float],
    environment: EnvironmentType = EnvironmentType.DOCKER,
) -> tuple[Optional[float], Optional[float]]:
    """Run NOP and/or Oracle agents, return (nop_reward, oracle_reward)."""
    nop_reward = oracle_reward = None
    
    if agent in ("nop", "both"):
        # When running both, keep image for nop so oracle can reuse it
        delete_after = (agent == "nop")  # Only delete if ONLY running nop
        code, job_result = run_harbor_agent(task_id, dataset_path, jobs_dir, "nop", timeout_multiplier, delete_after=delete_after, environment=environment)
        nop_reward = parse_harbor_reward(job_result)
        print(f"[validate] nop exit={code}, reward={nop_reward}")
    
    if agent in ("oracle", "both"):
        # Oracle always deletes (cleanup)
        code, job_result = run_harbor_agent(task_id, dataset_path, jobs_dir, "oracle", timeout_multiplier, delete_after=True, environment=environment)
        oracle_reward = parse_harbor_reward(job_result)
        print(f"[validate] oracle exit={code}, reward={oracle_reward}")
    
    return nop_reward, oracle_reward


def _run_network_isolated(
    args: ValidateArgs,
    task_id: str,
    dataset_path: Path,
    task_dir: Path,
    jobs_dir: Path,
) -> None:
    """Run network-isolated validation."""
    print("\n[validate] Running network-isolated validation...")
    
    with network_isolation(task_dir):
        nop_reward, oracle_reward = _run_agents(
            task_id, dataset_path, jobs_dir, args.agent, args.timeout_multiplier, args.environment
        )
    
    if args.agent == "both":
        if nop_reward != 0 or oracle_reward != 1:
            print("\n[validate] FAILED: Network-isolated validation did not meet expectations")
            print(f"  NOP-NO-NETWORK: expected reward=0, got reward={nop_reward}")
            print(f"  ORACLE-NO-NETWORK: expected reward=1, got reward={oracle_reward}")
            sys.exit(1)
        else:
            print("\n[validate] PASSED: Network-isolated validation met expectations")
            print(f"  NOP-NO-NETWORK: reward={nop_reward} ✓")
            print(f"  ORACLE-NO-NETWORK: reward={oracle_reward} ✓")


# ============================================================================
# BATCH MODE
# ============================================================================

def _run_batch_mode(args: ValidateArgs, dataset_path: Path) -> None:
    """Validate all tasks in parallel with clean output."""
    console = Console()
    jobs_dir = args.jobs_dir.resolve()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    
    # Find tasks
    task_dirs = [d for d in dataset_path.iterdir() if d.is_dir() and (d / "tests" / "test.sh").exists()]
    if not task_dirs:
        console.print("[yellow]No tasks found[/yellow]")
        return
    
    console.print(f"[blue]Found {len(task_dirs)} task(s) to validate[/blue]")
    console.print(f"[blue]Parallel: {args.max_parallel} | Agent: {args.agent}[/blue]")
    if args.output_file:
        console.print(f"[blue]Output: {args.output_file}[/blue]")
    console.print()
    
    # Run validations
    results = asyncio.run(_validate_batch(
        task_dirs, dataset_path, jobs_dir, args.agent, 
        args.max_parallel, args.timeout_multiplier, args.environment, console,
        args.output_file
    ))
    
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
    timeout_multiplier: Optional[float],
    environment: EnvironmentType,
    console: Console,
    output_file: Optional[Path] = None,
) -> list[ValidationResult]:
    """Run validations in parallel with progress bar."""
    semaphore = asyncio.Semaphore(max_parallel)
    
    # Lock and file handle for sequential writes
    write_lock = asyncio.Lock()
    file_handle = None
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        file_handle = open(output_file, "w")
        # Write header
        file_handle.write(f"# Validation results - {len(task_dirs)} tasks\n")
        file_handle.write(f"# Format: TASK_ID: NOP=<reward> ORACLE=<reward> <STATUS>\n\n")
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
                
                # Run NOP (capture_output=True to suppress Harbor's verbose output)
                if agent in ("nop", "both"):
                    # When running both, keep image for nop so oracle can reuse it
                    delete_after = (agent == "nop")  # Only delete if ONLY running nop
                    nop_code, job = await asyncio.to_thread(
                        run_harbor_agent, task_dir.name, dataset_path, jobs_dir, "nop", timeout_multiplier, True, delete_after, environment
                    )
                    nop_reward = parse_harbor_reward(job)
                
                # Run Oracle (capture_output=True to suppress Harbor's verbose output)
                if agent in ("oracle", "both"):
                    # Oracle always deletes (cleanup)
                    oracle_code, job = await asyncio.to_thread(
                        run_harbor_agent, task_dir.name, dataset_path, jobs_dir, "oracle", timeout_multiplier, True, True, environment
                    )
                    oracle_reward = parse_harbor_reward(job)
                
                # Determine pass/fail
                passed = _check_passed(agent, nop_reward, oracle_reward)
                
                result = ValidationResult(
                    task_id=task_dir.name,
                    nop_reward=nop_reward,
                    oracle_reward=oracle_reward,
                    nop_exit_code=nop_code,
                    oracle_exit_code=oracle_code,
                    passed=passed,
                )
            except Exception as e:
                result = ValidationResult(
                    task_id=task_dir.name,
                    nop_reward=None,
                    oracle_reward=None,
                    nop_exit_code=-1,
                    oracle_exit_code=-1,
                    passed=False,
                    error=str(e),
                )
            
            # Write to file immediately
            await write_result(result)
            return result
    
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
    
    if result.error:
        parts.append(f"ERROR: {result.error}")
    elif result.passed:
        parts.append("PASS")
    else:
        parts.append("FAIL")
    
    return " ".join(parts)


def _check_passed(agent: str, nop_reward: Optional[float], oracle_reward: Optional[float]) -> bool:
    """Check if validation passed based on agent type and rewards."""
    if agent == "both":
        return nop_reward == 0 and oracle_reward == 1
    elif agent == "nop":
        return nop_reward == 0
    elif agent == "oracle":
        return oracle_reward == 1
    return False


def _print_results(results: list[ValidationResult], agent: str, show_passed: bool, console: Console) -> None:
    """Print results table (failures only by default) and summary."""
    passed = [r for r in results if r.passed and not r.error]
    failed = [r for r in results if not r.passed and not r.error]
    errors = [r for r in results if r.error]
    
    # Show table if there are failures/errors or if show_passed requested
    if failed or errors or show_passed:
        table = Table(
            title="Validation Failures" if not show_passed else "Validation Results",
            title_style="bold cyan",
            show_lines=True
        )
        table.add_column("Task ID", style="cyan")
        
        if agent in ("nop", "both"):
            table.add_column("NOP", justify="center")
        if agent in ("oracle", "both"):
            table.add_column("Oracle", justify="center")
        
        table.add_column("Status", justify="center")
        table.add_column("Notes")
        
        # Show errors, then failures, then passed (if requested)
        for result in sorted(errors + failed + (passed if show_passed else []), key=lambda r: r.task_id):
            _add_result_row(table, result, agent)
        
        console.print("\n")
        console.print(table)
    
    # Always show summary
    console.print(f"\n[bold]Summary:[/bold]")
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
        row.extend(["❌ ERROR", result.error])
        table.add_row(*row, style="red")
        return
    
    if result.passed:
        # Passed row (only shown if show_passed=True)
        if agent in ("nop", "both"):
            row.append(f"✓ ({result.nop_reward})" if result.nop_reward is not None else "—")
        if agent in ("oracle", "both"):
            row.append(f"✓ ({result.oracle_reward})" if result.oracle_reward is not None else "—")
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
    
    row.extend(["❌ FAIL", "; ".join(notes)])
    table.add_row(*row, style="red")
