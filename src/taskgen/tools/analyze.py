from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from .harbor_runner import harbor_cmd_base


@dataclass
class TrialOutcome:
    """Result of a single trial."""
    trial_name: str
    reward: Optional[float]
    exception_type: Optional[str]
    exception_message: Optional[str]
    trajectory_path: Optional[Path]
    test_output_path: Optional[Path]


@dataclass
class QualityCheckResult:
    """Result of static quality check."""
    passed: bool
    issues: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


@dataclass 
class DebugCheckResult:
    """Result of instruction sufficiency check."""
    outcome: str  # "PASS", "FAIL", "NOT_APPLICABLE"
    explanation: str


@dataclass
class VarianceAnalysis:
    """Analysis of solution variance across trials."""
    num_successes: int
    num_failures: int
    has_variance: bool
    variance_details: str


@dataclass
class AnalysisResult:
    """Complete analysis result for a task."""
    task_id: str
    # Quality check
    quality_check: Optional[QualityCheckResult]
    # Trial results
    trials_run: int
    success_rate: float
    trial_outcomes: list[TrialOutcome]
    # Instruction check
    instruction_check: Optional[DebugCheckResult]
    # Variance analysis
    variance_analysis: Optional[VarianceAnalysis]
    # Summary
    summary_path: Optional[Path]
    job_dir: Optional[Path]
    # Overall verdict
    is_good_task: bool
    verdict_reasons: list[str]


@dataclass
class AnalyzeArgs:
    """Arguments for the analyze command."""
    task_path: Path
    agent: str = "claude-code"
    model: str = "anthropic/claude-sonnet-4-20250514"
    n_trials: int = 3
    jobs_dir: Path = Path(".state/analyze-jobs")
    skip_quality_check: bool = False
    skip_summarize: bool = False
    analysis_model: str = "haiku"  # Harbor uses shorthand model names
    verbose: bool = False
    timeout_multiplier: float = 1.0


def run_analyze(args: AnalyzeArgs) -> AnalysisResult:
    """Main entry point for task analysis."""
    console = Console()
    
    # Resolve task path
    task_path = args.task_path.resolve()
    if not task_path.is_dir():
        console.print(f"[red]Error: Task path does not exist: {task_path}[/red]")
        raise SystemExit(1)
    
    task_id = task_path.name
    dataset_path = task_path.parent
    
    # Check task structure
    if not (task_path / "tests" / "test.sh").exists():
        console.print(f"[red]Error: Not a valid task (missing tests/test.sh): {task_path}[/red]")
        raise SystemExit(1)
    
    console.print(Panel.fit(
        f"[bold cyan]Analyzing Task: {task_id}[/bold cyan]\n"
        f"Agent: {args.agent} | Model: {args.model} | Trials: {args.n_trials}",
        title="Task Analysis"
    ))
    
    # Run analysis steps
    result = _run_analysis(args, task_id, task_path, dataset_path, console)
    
    # Print final report
    _print_report(result, console)
    
    return result


def _run_analysis(
    args: AnalyzeArgs,
    task_id: str,
    task_path: Path,
    dataset_path: Path,
    console: Console,
) -> AnalysisResult:
    """Run all analysis steps."""
    
    verdict_reasons = []
    
    # Step 1: Static quality check
    quality_check = None
    if not args.skip_quality_check:
        console.print("\n[bold blue]Step 1/4: Static Quality Check[/bold blue]")
        quality_check = _run_quality_check(task_path, args.analysis_model, console)
        if not quality_check.passed:
            verdict_reasons.append(f"Quality check found {len(quality_check.issues)} issue(s)")
    else:
        console.print("\n[dim]Step 1/4: Static Quality Check (skipped)[/dim]")
    
    # Step 2: Run agent trials
    console.print(f"\n[bold blue]Step 2/4: Running {args.n_trials} Agent Trials[/bold blue]")
    job_dir, trial_outcomes = _run_agent_trials(
        args, task_id, dataset_path, console
    )
    
    successes = sum(1 for t in trial_outcomes if t.reward == 1)
    failures = sum(1 for t in trial_outcomes if t.reward is not None and t.reward != 1)
    errors = sum(1 for t in trial_outcomes if t.exception_type is not None)
    success_rate = successes / len(trial_outcomes) if trial_outcomes else 0.0
    
    console.print(f"  Results: {successes} passed, {failures} failed, {errors} errors")
    console.print(f"  Success rate: {success_rate:.1%}")
    
    if success_rate == 0:
        verdict_reasons.append("No trials succeeded - task may be unsolvable or mis-specified")
    elif success_rate < 0.5:
        verdict_reasons.append(f"Low success rate ({success_rate:.0%}) - task may have issues")
    
    # Step 3: Summarize failures (if any failures)
    summary_path = None
    if not args.skip_summarize and (failures > 0 or errors > 0) and job_dir:
        console.print("\n[bold blue]Step 3/4: Analyzing Failures[/bold blue]")
        summary_path = _run_job_summarize(job_dir, args.analysis_model, console)
    else:
        if failures == 0 and errors == 0:
            console.print("\n[dim]Step 3/4: Analyzing Failures (all passed, skipped)[/dim]")
        else:
            console.print("\n[dim]Step 3/4: Analyzing Failures (skipped)[/dim]")
    
    # Step 4: Check instruction sufficiency
    instruction_check = None
    if job_dir and (failures > 0 or errors > 0):
        console.print("\n[bold blue]Step 4/4: Instruction Sufficiency Check[/bold blue]")
        instruction_check = _run_debug_check(
            task_id, args.analysis_model, job_dir, console
        )
        if instruction_check and instruction_check.outcome == "FAIL":
            verdict_reasons.append(f"Instructions insufficient: {instruction_check.explanation[:100]}...")
    else:
        console.print("\n[dim]Step 4/4: Instruction Sufficiency Check (skipped - all passed)[/dim]")
    
    # Analyze solution variance
    variance_analysis = _analyze_variance(trial_outcomes, console)
    if variance_analysis and successes >= 2 and not variance_analysis.has_variance:
        # Note: This is informational, not necessarily a problem
        pass
    
    # Determine overall verdict
    is_good_task = (
        success_rate >= 0.5 and  # At least half succeed
        (quality_check is None or quality_check.passed) and
        (instruction_check is None or instruction_check.outcome != "FAIL")
    )
    
    if not verdict_reasons and is_good_task:
        verdict_reasons.append("Task passed all checks")
    
    return AnalysisResult(
        task_id=task_id,
        quality_check=quality_check,
        trials_run=len(trial_outcomes),
        success_rate=success_rate,
        trial_outcomes=trial_outcomes,
        instruction_check=instruction_check,
        variance_analysis=variance_analysis,
        summary_path=summary_path,
        job_dir=job_dir,
        is_good_task=is_good_task,
        verdict_reasons=verdict_reasons,
    )


def _run_quality_check(
    task_path: Path,
    model: str,
    console: Console,
) -> QualityCheckResult:
    """Run Harbor's static quality check on the task."""
    cmd = harbor_cmd_base() + [
        "tasks", "check",
        str(task_path),
        "-m", model,
    ]
    
    with console.status("[cyan]Running quality check..."):
        proc = subprocess.run(cmd, capture_output=True, text=True)
    
    # Parse output to extract issues
    issues = []
    details = {}
    
    # The quality checker outputs a table, we need to parse it
    # For now, we'll just check exit code and look for "fail" in output
    output = proc.stdout + proc.stderr
    
    # Look for failed checks in output
    fail_keywords = ["fail", "FAIL", "❌"]
    for line in output.split("\n"):
        for keyword in fail_keywords:
            if keyword in line and "passed" not in line.lower():
                # Extract the check name if possible
                clean_line = line.strip()
                if clean_line and "│" in clean_line:
                    parts = [p.strip() for p in clean_line.split("│")]
                    if len(parts) >= 2 and any(k in parts[1].lower() for k in ["fail"]):
                        issues.append(parts[0])
    
    passed = proc.returncode == 0 and len(issues) == 0
    
    if passed:
        console.print("  [green]✓ Quality check passed[/green]")
    else:
        console.print(f"  [yellow]⚠ Quality check found issues:[/yellow]")
        for issue in issues[:5]:  # Show first 5
            console.print(f"    - {issue}")
    
    return QualityCheckResult(passed=passed, issues=issues, details=details)


def _run_agent_trials(
    args: AnalyzeArgs,
    task_id: str,
    dataset_path: Path,
    console: Console,
) -> tuple[Optional[Path], list[TrialOutcome]]:
    """Run multiple agent trials on the task."""
    
    # Create unique job directory
    timestamp = int(time.time())
    jobs_parent = args.jobs_dir.resolve()
    jobs_parent.mkdir(parents=True, exist_ok=True)
    
    # Run Harbor with multiple attempts
    cmd = harbor_cmd_base() + [
        "run",
        "-p", str(dataset_path),
        "-t", task_id,
        "-a", args.agent,
        "-m", args.model,
        "-k", str(args.n_trials),  # n_attempts
        "-n", "1",  # n_concurrent (run one at a time for cleaner output)
        "--jobs-dir", str(jobs_parent),
        "--timeout-multiplier", str(args.timeout_multiplier),
    ]
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"[cyan]Running {args.n_trials} trials with {args.agent}...",
            total=None
        )
        
        proc = subprocess.run(cmd, capture_output=True, text=True)
        progress.update(task, completed=True)
    
    # Find the job directory that was created
    job_dirs = sorted(
        [d for d in jobs_parent.iterdir() if d.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    
    job_dir = job_dirs[0] if job_dirs else None
    
    # Parse trial results
    trial_outcomes = []
    if job_dir:
        trial_outcomes = _parse_trial_results(job_dir)
    
    return job_dir, trial_outcomes


def _parse_trial_results(job_dir: Path) -> list[TrialOutcome]:
    """Parse trial results from a job directory."""
    from harbor.models.trial.result import TrialResult
    from harbor.models.trial.paths import TrialPaths
    
    outcomes = []
    
    for trial_dir in job_dir.iterdir():
        if not trial_dir.is_dir():
            continue
        
        result_path = trial_dir / "result.json"
        if not result_path.exists():
            continue
        
        try:
            result = TrialResult.model_validate_json(result_path.read_text())
            
            reward = None
            if result.verifier_result and result.verifier_result.rewards:
                reward = result.verifier_result.rewards.get("reward")
            
            exception_type = None
            exception_message = None
            if result.exception_info:
                exception_type = result.exception_info.exception_type
                exception_message = result.exception_info.exception_message
            
            # Find trajectory path
            trajectory_path = trial_dir / "agent" / "trajectory.json"
            if not trajectory_path.exists():
                trajectory_path = None
            
            # Find test output path
            test_output_path = trial_dir / "verifier" / "test-stdout.txt"
            if not test_output_path.exists():
                test_output_path = None
            
            outcomes.append(TrialOutcome(
                trial_name=result.trial_name,
                reward=reward,
                exception_type=exception_type,
                exception_message=exception_message,
                trajectory_path=trajectory_path,
                test_output_path=test_output_path,
            ))
        except Exception as e:
            console = Console()
            console.print(f"[dim]Warning: Could not parse {result_path}: {e}[/dim]")
    
    return outcomes


def _run_job_summarize(
    job_dir: Path,
    model: str,
    console: Console,
) -> Optional[Path]:
    """Run Harbor's job summarizer on failed trials."""
    cmd = harbor_cmd_base() + [
        "jobs", "summarize",
        str(job_dir),
        "-m", model,
        "--failed",  # Only analyze failed trials
    ]
    
    with console.status("[cyan]Analyzing failed trials..."):
        proc = subprocess.run(cmd, capture_output=True, text=True)
    
    summary_path = job_dir / "summary.md"
    if summary_path.exists():
        console.print(f"  [green]✓ Summary written to: {summary_path}[/green]")
        return summary_path
    else:
        console.print("  [yellow]⚠ No summary generated[/yellow]")
        return None


def _run_debug_check(
    task_id: str,
    model: str,
    job_dir: Path,
    console: Console,
) -> Optional[DebugCheckResult]:
    """Run Harbor's debug checker to analyze instruction sufficiency."""
    cmd = harbor_cmd_base() + [
        "tasks", "debug",
        task_id,
        "-m", model,
        "--job-id", job_dir.name,
        "--jobs-dir", str(job_dir.parent),
    ]
    
    with console.status("[cyan]Checking instruction sufficiency..."):
        proc = subprocess.run(cmd, capture_output=True, text=True)
    
    # Parse output for outcome
    output = proc.stdout + proc.stderr
    
    outcome = "NOT_APPLICABLE"
    explanation = "Could not determine instruction sufficiency"
    
    # Look for PASS/FAIL in output
    if "PASS" in output:
        outcome = "PASS"
        explanation = "Instructions appear sufficient for the task"
    elif "FAIL" in output:
        outcome = "FAIL"
        # Try to extract explanation
        for line in output.split("\n"):
            if "insufficient" in line.lower() or "FAIL" in line:
                explanation = line.strip()
                break
    
    result = DebugCheckResult(outcome=outcome, explanation=explanation)
    
    if outcome == "PASS":
        console.print(f"  [green]✓ Instructions are sufficient[/green]")
    elif outcome == "FAIL":
        console.print(f"  [red]✗ Instructions may be insufficient[/red]")
        console.print(f"    {explanation[:100]}...")
    else:
        console.print(f"  [dim]? Could not determine ({outcome})[/dim]")
    
    return result


def _analyze_variance(
    trial_outcomes: list[TrialOutcome],
    console: Console,
) -> Optional[VarianceAnalysis]:
    """Analyze variance in successful solutions."""
    successes = [t for t in trial_outcomes if t.reward == 1]
    failures = [t for t in trial_outcomes if t.reward is not None and t.reward != 1]
    
    if len(successes) < 2:
        return VarianceAnalysis(
            num_successes=len(successes),
            num_failures=len(failures),
            has_variance=False,
            variance_details="Not enough successful trials to analyze variance"
        )
    
    # Compare trajectories if available
    trajectories = []
    for trial in successes:
        if trial.trajectory_path and trial.trajectory_path.exists():
            try:
                traj_data = json.loads(trial.trajectory_path.read_text())
                trajectories.append(traj_data)
            except Exception:
                pass
    
    if len(trajectories) < 2:
        return VarianceAnalysis(
            num_successes=len(successes),
            num_failures=len(failures),
            has_variance=True,  # Assume variance if we can't analyze
            variance_details="Could not load trajectories for comparison"
        )
    
    # Simple variance check: compare tool usage patterns
    tool_patterns = []
    for traj in trajectories:
        tools_used = set()
        steps = traj.get("steps", [])
        for step in steps:
            tool_calls = step.get("tool_calls", [])
            for tc in tool_calls:
                tools_used.add(tc.get("function_name", "unknown"))
        tool_patterns.append(frozenset(tools_used))
    
    # Check if patterns differ
    unique_patterns = set(tool_patterns)
    has_variance = len(unique_patterns) > 1
    
    if has_variance:
        details = f"Found {len(unique_patterns)} different solution approaches across {len(successes)} successes"
    else:
        details = f"All {len(successes)} successful solutions used the same approach"
    
    return VarianceAnalysis(
        num_successes=len(successes),
        num_failures=len(failures),
        has_variance=has_variance,
        variance_details=details,
    )


def _print_report(result: AnalysisResult, console: Console) -> None:
    """Print the final analysis report."""
    console.print("\n")
    
    # Overall verdict
    if result.is_good_task:
        verdict_style = "bold green"
        verdict_icon = "✅"
        verdict_text = "GOOD TASK"
    else:
        verdict_style = "bold red"
        verdict_icon = "❌"
        verdict_text = "NEEDS REVIEW"
    
    console.print(Panel.fit(
        f"[{verdict_style}]{verdict_icon} {verdict_text}[/{verdict_style}]",
        title=f"Analysis Result: {result.task_id}"
    ))
    
    # Summary table
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check", style="cyan")
    table.add_column("Result")
    table.add_column("Details")
    
    # Quality check row
    if result.quality_check:
        qc_status = "✅ Passed" if result.quality_check.passed else f"⚠️ {len(result.quality_check.issues)} issues"
        qc_style = "green" if result.quality_check.passed else "yellow"
        table.add_row(
            "Quality Check",
            f"[{qc_style}]{qc_status}[/{qc_style}]",
            ", ".join(result.quality_check.issues[:3]) if result.quality_check.issues else "All checks passed"
        )
    
    # Trials row
    trials_status = f"{result.success_rate:.0%} success rate"
    if result.success_rate >= 0.67:
        trials_style = "green"
        trials_icon = "✅"
    elif result.success_rate >= 0.33:
        trials_style = "yellow"
        trials_icon = "⚠️"
    else:
        trials_style = "red"
        trials_icon = "❌"
    
    successes = sum(1 for t in result.trial_outcomes if t.reward == 1)
    failures = sum(1 for t in result.trial_outcomes if t.reward is not None and t.reward != 1)
    errors = sum(1 for t in result.trial_outcomes if t.exception_type)
    
    table.add_row(
        f"Agent Trials ({result.trials_run})",
        f"[{trials_style}]{trials_icon} {trials_status}[/{trials_style}]",
        f"{successes} passed, {failures} failed, {errors} errors"
    )
    
    # Instruction check row
    if result.instruction_check:
        if result.instruction_check.outcome == "PASS":
            ic_status = "✅ Sufficient"
            ic_style = "green"
        elif result.instruction_check.outcome == "FAIL":
            ic_status = "❌ Insufficient"
            ic_style = "red"
        else:
            ic_status = "? Unknown"
            ic_style = "dim"
        
        table.add_row(
            "Instruction Check",
            f"[{ic_style}]{ic_status}[/{ic_style}]",
            result.instruction_check.explanation[:60] + "..." if len(result.instruction_check.explanation) > 60 else result.instruction_check.explanation
        )
    
    # Variance row
    if result.variance_analysis and result.variance_analysis.num_successes >= 2:
        if result.variance_analysis.has_variance:
            var_status = "✅ Multiple approaches"
            var_style = "green"
        else:
            var_status = "ℹ️ Single approach"
            var_style = "dim"
        table.add_row(
            "Solution Variance",
            f"[{var_style}]{var_status}[/{var_style}]",
            result.variance_analysis.variance_details
        )
    
    console.print(table)
    
    # Show failure details if there were failures
    failed_trials = [t for t in result.trial_outcomes if t.reward is not None and t.reward != 1]
    if failed_trials:
        console.print("\n[bold red]Failure Analysis:[/bold red]")
        failure_patterns = _extract_failure_patterns(failed_trials)
        if failure_patterns:
            for pattern in failure_patterns[:5]:  # Show top 5 patterns
                console.print(f"  • {pattern}")
        
        # Show test output snippet from first failure
        first_failure = failed_trials[0]
        if first_failure.test_output_path and first_failure.test_output_path.exists():
            test_output = first_failure.test_output_path.read_text()
            error_snippet = _extract_error_snippet(test_output)
            if error_snippet:
                console.print("\n[bold]Test Output (first failure):[/bold]")
                console.print(Panel(error_snippet, border_style="red", expand=False))
    
    # Show summary content if available
    if result.summary_path and result.summary_path.exists():
        summary_content = result.summary_path.read_text().strip()
        if summary_content and not summary_content.startswith("API Error"):
            console.print("\n[bold]AI Summary:[/bold]")
            # Truncate if too long
            if len(summary_content) > 1000:
                summary_content = summary_content[:1000] + "\n... (truncated)"
            console.print(Panel(summary_content, border_style="blue", expand=False))
    
    # Verdict reasons
    if result.verdict_reasons:
        console.print("\n[bold]Verdict:[/bold]")
        for reason in result.verdict_reasons:
            console.print(f"  • {reason}")
    
    # Job directory
    if result.job_dir:
        console.print(f"\n[dim]Job artifacts: {result.job_dir}[/dim]")
    if result.summary_path:
        console.print(f"[dim]Failure summary: {result.summary_path}[/dim]")


def _extract_failure_patterns(failed_trials: list[TrialOutcome]) -> list[str]:
    """Extract common failure patterns from test outputs."""
    patterns = []
    error_counts: dict[str, int] = {}
    
    for trial in failed_trials:
        if trial.test_output_path and trial.test_output_path.exists():
            try:
                content = trial.test_output_path.read_text()
                # Look for common error patterns
                for line in content.split('\n'):
                    line = line.strip()
                    # Match error lines
                    if any(x in line.lower() for x in ['error:', 'error ', 'failed', 'exception', 'assert']):
                        # Clean up the error message
                        if len(line) > 10 and len(line) < 200:
                            # Normalize the message
                            key = line[:100]
                            error_counts[key] = error_counts.get(key, 0) + 1
            except Exception:
                pass
    
    # Sort by frequency and return top patterns
    sorted_errors = sorted(error_counts.items(), key=lambda x: x[1], reverse=True)
    for error, count in sorted_errors[:5]:
        if count > 1:
            patterns.append(f"({count}x) {error}")
        else:
            patterns.append(error)
    
    return patterns


def _extract_error_snippet(test_output: str, max_lines: int = 40) -> str:
    """Extract the most relevant error snippet from test output."""
    lines = test_output.split('\n')
    
    # Strategy 1: Find numbered failure sections like "1)" or "  1)"
    error_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Match numbered failures like "1)", "2)", "  1)" - common in mocha/jest
        if stripped and (stripped.startswith('1)') or stripped.startswith('2)') or 
                        '1)' in stripped and 'failing' not in stripped.lower()):
            error_start = i
            break
    
    # Strategy 2: Find "X failing" summary and show what follows
    if error_start is None:
        for i, line in enumerate(lines):
            if 'failing' in line.lower() and any(c.isdigit() for c in line):
                error_start = max(0, i - 2)
                break
    
    # Strategy 3: Find assertion/error messages
    if error_start is None:
        for i, line in enumerate(lines):
            if any(x in line for x in ['AssertionError', 'Error:', 'FAILED', '✗']):
                error_start = max(0, i - 2)
                break
    
    # Strategy 4: Just take the last portion (test summary usually at end)
    if error_start is None:
        error_start = max(0, len(lines) - max_lines)
    
    # Extract snippet, but look for the end of the error section
    error_end = min(error_start + max_lines, len(lines))
    
    # Try to find a natural end point (empty line after stack trace)
    for i in range(error_start + 5, min(error_start + max_lines * 2, len(lines))):
        if i < len(lines) and not lines[i].strip():
            # Check if next line doesn't start with whitespace (end of stack)
            if i + 1 < len(lines) and lines[i + 1].strip() and not lines[i + 1].startswith(' '):
                error_end = i
                break
    
    snippet_lines = lines[error_start:error_end]
    snippet = '\n'.join(snippet_lines)
    
    # Truncate if still too long
    if len(snippet) > 2000:
        snippet = snippet[:2000] + "\n... (truncated)"
    
    return snippet.strip()

