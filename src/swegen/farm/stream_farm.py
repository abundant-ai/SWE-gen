from __future__ import annotations

import json
import shutil
import signal
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from swegen.config import FarmConfig

from .farm_hand import (
    PRCandidate,
    TaskResult,
    _now_utc,
    _run_reversal_for_pr,
    _slug,
)
from .fetcher import StreamingPRFetcher, load_skip_list
from .state import StreamState

# Harbor names every task image "hb__{environment_name}" (see harbor's docker
# environment). Matching that prefix lets us reclaim the per-task images -- which are
# the ones that actually consume disk -- without touching base images (ubuntu, language
# runtimes) or any unrelated image on the host, both of which `docker system prune -af`
# would happily delete.
HARBOR_IMAGE_GLOB = "hb__*"


def docker_cleanup_cmds(build_cache_keep: str) -> list[str]:
    """Docker cleanup steps, in order.

    Containers are pruned before images so image removal is not blocked by stopped
    trial containers. The build cache is trimmed to a ceiling rather than emptied:
    layers are evicted least-recently-used first, so the base + runtime layers shared
    by every task survive and rebuilds stay warm, while the per-task layers (whose
    cache keys embed the task's head SHA, so they are never hit again) are reclaimed.
    """
    return [
        "docker container prune -f",
        f'docker image ls --filter "reference={HARBOR_IMAGE_GLOB}" -q | xargs -r docker rmi -f',
        "docker volume prune -f",
        f"docker builder prune -f --keep-storage {build_cache_keep}",
    ]

# Failure categories where the PR was rejected by a cheap filter, before the
# Claude Code session ran. These paths make only a couple of GitHub API calls and
# never touch the Anthropic API, so there is nothing to rate limit against and the
# inter-task delay is pure dead time.
SKIPPED_CATEGORIES = frozenset(
    {
        "trivial",
        "no_issue",
        "no_tests",
        "already_exists",
    }
)


class StreamFarmer:
    """Manages continuous PR farming with streaming.

    Orchestrates the process of:
    1. Streaming PRs from GitHub (via StreamingPRFetcher)
    2. Processing each PR into a Harbor task (via farm_hand)
    3. Tracking state for resumability (via StreamState)
    4. Periodic cleanup and progress reporting

    Attributes:
        repo: Repository in "owner/repo" format
        config: FarmConfig with all settings
        console: Rich console for output
        tasks_root: Directory for generated tasks
        state: StreamState for tracking progress
        state_file: Path to state persistence file
        resume_from_time: ISO timestamp to resume from (if any)
        fetcher: StreamingPRFetcher instance
        results: List of TaskResult from this session
        shutdown_requested: Flag for graceful shutdown
    """

    def __init__(
        self,
        repo: str,
        config: FarmConfig,
        console: Console,
    ):
        self.repo = repo
        self.config = config
        self.console = console
        self.tasks_root = config.output
        self.tasks_root.mkdir(exist_ok=True)

        # State file path
        self.state_file = config.state_dir / "stream_farm" / f"{_slug(repo)}.json"

        # Load or create state
        if config.reset:
            self.state = StreamState(repo=repo)
            self.console.print("[yellow]State reset - starting fresh[/yellow]")
        else:
            self.state = StreamState.load(self.state_file, repo)

        # Load skip list if provided
        if config.skip_list:
            skip_list_path = Path(config.skip_list)
            skip_prs = load_skip_list(skip_list_path, repo)
            self.state.skip_list_prs = skip_prs
            if skip_prs:
                self.console.print(
                    f"[yellow]Loaded skip list: {len(skip_prs)} PRs to skip from {skip_list_path}[/yellow]"
                )

        # Determine resume time
        self.resume_from_time = self._determine_resume_time()

        # Create streaming fetcher (always require tests)
        self.fetcher = StreamingPRFetcher(
            repo=repo,
            console=console,
            state=self.state,
            min_files=config.min_source_files,  # Early approximate filter
            require_tests=True,  # Always require tests
            api_delay=config.api_delay,
        )

        # Results tracking
        self.results: list[TaskResult] = []

        # Graceful shutdown handling
        self.shutdown_requested = False
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _determine_resume_time(self) -> str | None:
        """Determine the resume time based on config and state.

        Returns:
            ISO timestamp string to resume from, or None to start fresh
        """
        if self.config.resume_from:
            # User specified a resume time - parse date or full timestamp
            resume_input = self.config.resume_from.strip()
            try:
                # Try to parse as date only (YYYY-MM-DD)
                if len(resume_input) == 10 and resume_input.count("-") == 2:
                    # Date only - convert to end of day (23:59:59) since we're working backwards
                    resume_date = datetime.strptime(resume_input, "%Y-%m-%d")
                    # Set to end of day in UTC
                    resume_dt = resume_date.replace(
                        hour=23, minute=59, second=59, microsecond=999999, tzinfo=UTC
                    )
                    self.console.print(
                        f"[yellow]Resuming from end of {resume_input} "
                        f"(processing PRs merged before this date)[/yellow]"
                    )
                    return resume_dt.isoformat()
                else:
                    # Full timestamp - validate it parses
                    datetime.fromisoformat(resume_input.replace("Z", "+00:00"))
                    return resume_input
            except ValueError as e:
                self.console.print(
                    f"[red]Error: Invalid --resume-from format: {resume_input}[/red]"
                )
                self.console.print("[yellow]Expected date like: 2024-01-15[/yellow]")
                self.console.print("[yellow]Or full timestamp like: 2024-01-15T10:30:00Z[/yellow]")
                raise ValueError(f"Invalid timestamp format: {e}") from e
        elif not self.config.reset and self.state.last_created_at:
            # Resume from last processed PR's creation time
            self.console.print(
                f"[yellow]Resuming from last processed PR (created at {self.state.last_created_at})[/yellow]"
            )
            return self.state.last_created_at

        return None

    def _handle_shutdown(self, signum, frame):
        """Handle graceful shutdown on interrupt."""
        self.console.print("\n[yellow]Shutdown requested... finishing current PR...[/yellow]")
        self.shutdown_requested = True

    def run(self) -> int:
        """Run the continuous farming process.

        Returns:
            Exit code: 0 if any tasks succeeded, 1 otherwise
        """
        self._print_header()

        # Start streaming and processing
        try:
            self._run_stream()
        except KeyboardInterrupt:
            self.console.print("\n[yellow]Interrupted by user[/yellow]")
        finally:
            self._finalize()

        return 0 if self.state.successful > 0 else 1

    def _print_header(self) -> None:
        """Print the farming header with settings."""
        self.console.print(Rule(Text(f"Stream Farming - {self.repo}", style="bold cyan")))

        # pipeline info
        self.console.print("[green]Only PRs that modify tests will be considered.[/green]")

        if self.config.require_issue:
            self.console.print(
                "[magenta]REQUIRE-ISSUE MODE - only PRs with linked issues will be processed[/magenta]"
            )

        if self.config.dry_run:
            self.console.print("[cyan]DRY RUN MODE - no tasks will be generated[/cyan]")

        self.console.print(
            f"[dim]Timeout: {self.config.timeout}s | " f"State: {self.state_file}[/dim]\n"
        )

    def _run_stream(self) -> None:
        """Process PRs synchronously: fetch one, process it, repeat."""
        self.console.print("[cyan]Streaming and processing PRs...[/cyan]\n")

        for pr in self.fetcher.stream_prs(resume_from_time=self.resume_from_time):
            if self.shutdown_requested:
                self.console.print("[yellow]Shutdown requested, stopping...[/yellow]")
                break

            self._process_pr(pr)

    def _process_pr(self, pr: PRCandidate) -> None:
        """Process a single PR candidate.

        Args:
            pr: The PR candidate to process
        """
        # Print PR header
        merged_dt = datetime.fromisoformat(pr.merged_at.replace("Z", "+00:00"))
        self.console.print(
            f"\n[bold cyan]═══ PR #{pr.number} ({self.state.total_processed + 1}) ═══[/bold cyan]"
        )
        self.console.print(f"[bold]{pr.title}[/bold]")
        self.console.print(
            f"[dim]Merged: {merged_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} | "
            f"Files: {pr.files_changed} | "
            f"+{pr.additions}/-{pr.deletions}[/dim]"
        )

        # Process this PR completely before moving to next
        result = _run_reversal_for_pr(pr, self.config, self.tasks_root, self.console)
        self.results.append(result)

        # Mark as processed with detailed tracking
        self.state.mark_processed(
            pr.number, 
            pr.created_at, 
            result.status == "success",
            task_id=result.task_id if result.status == "success" else None,
            category=result.category,
            message=result.message if result.category == "other" else None,
        )
        self._save_state()

        # Show result
        self._print_result(result)

        # Rate limit protection: only sleep after PRs that actually ran a CC session
        if not self._should_delay_after(result):
            self.console.print(
                f"[dim]Skipping delay ({result.category or result.status}, no CC session run)[/dim]"
            )
        elif self.config.task_delay > 0:
            self.console.print(
                f"[dim]Waiting {self.config.task_delay} seconds before next PR...[/dim]"
            )
            time.sleep(self.config.task_delay)

        # Periodic summary
        if self.state.total_processed % 10 == 0:
            self._print_progress()

        # Docker cleanup after batch
        if self.config.docker_prune_batch > 0:
            if self.state.total_processed % self.config.docker_prune_batch == 0:
                self._prune_docker()

    def _should_delay_after(self, result: TaskResult) -> bool:
        """Whether to sleep before the next PR.

        Only PRs that reached the Claude Code session need the delay. Dry runs and
        PRs rejected by a cheap filter (trivial, no linked issue, no tests, already
        exists) did no meaningful API work, so waiting on them is dead time.
        """
        if result.status == "dry-run":
            return False
        return result.category not in SKIPPED_CATEGORIES

    def _print_result(self, result: TaskResult) -> None:
        """Print the result of processing a PR.

        Args:
            result: The TaskResult to display
        """
        if result.status == "success":
            self.console.print(f"[green]✓ Success: {result.message}[/green]")
        elif result.status == "dry-run":
            self.console.print(f"[cyan]○ Dry-run: {result.message}[/cyan]")
        else:
            self.console.print(f"[red]✗ Failed: {result.message}[/red]")

    def _print_progress(self) -> None:
        """Print progress summary."""
        last_info = f"#{self.state.last_pr_number or 'N/A'}"
        if self.state.last_created_at:
            created_dt = datetime.fromisoformat(self.state.last_created_at.replace("Z", "+00:00"))
            last_info = f"#{self.state.last_pr_number} (created {created_dt.strftime('%Y-%m-%d')})"

        # Calculate top failure reasons
        failure_summary = []
        if len(self.state.trivial_prs) > 0:
            failure_summary.append(f"Trivial: {len(self.state.trivial_prs)}")
        if len(self.state.no_issue_prs) > 0:
            failure_summary.append(f"No Issue: {len(self.state.no_issue_prs)}")
        if len(self.state.validation_failed_prs) > 0:
            failure_summary.append(f"Validation: {len(self.state.validation_failed_prs)}")
        
        failure_text = ", ".join(failure_summary[:3]) if failure_summary else "None"
        success_rate = (self.state.successful / self.state.total_processed * 100) if self.state.total_processed > 0 else 0

        self.console.print(
            Panel(
                f"Processed: {self.state.total_processed}\n"
                f"✓ Success: {self.state.successful} ({success_rate:.1f}%)\n"
                f"✗ Failed: {self.state.failed}\n"
                f"Top failures: {failure_text}\n"
                f"Last PR: {last_info}",
                title="Progress",
                border_style="cyan",
            )
        )

    def _prune_docker(self) -> None:
        """Run docker cleanup to free disk space."""
        if shutil.which("docker") is None:
            self.console.print(
                "[yellow]Skipping docker prune (docker binary not found in PATH).[/yellow]"
            )
            return

        cmds = docker_cleanup_cmds(self.config.build_cache_keep)
        self.console.print(
            Panel(
                "Running docker cleanup:\n" + "\n".join(f"  {cmd}" for cmd in cmds),
                title="Disk cleanup",
                border_style="yellow",
            )
        )

        for cmd in cmds:
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            except subprocess.TimeoutExpired:
                self.console.print(f"[red]Docker cleanup timed out after 600s: {cmd}[/red]")
                continue

            if result.returncode != 0:
                # Keep going: a failed step (e.g. an image still in use) should not
                # prevent the remaining steps from reclaiming what they can.
                self.console.print(
                    f"[red]Docker cleanup step failed (exit {result.returncode}): {cmd}[/red]"
                )
                if result.stderr:
                    self.console.print(f"[red]{result.stderr.strip()}[/red]")
                continue

            # Show the reclaimed-space summary when docker reports one.
            summary_lines = [
                line
                for line in result.stdout.strip().split("\n")
                if "reclaimed" in line.lower() or "total" in line.lower()
            ]
            if summary_lines:
                self.console.print(f"[dim]{summary_lines[0]}[/dim]")

        self.console.print("[green]Docker cleanup completed[/green]")

    def _save_state(self) -> None:
        """Save state to file."""
        self.state.save(self.state_file)

    def _finalize(self) -> None:
        """Finalize the run and print summary."""
        self._save_state()
        self._save_log()

        self.console.print("\n")
        self.console.print(Rule(Text("Final Summary", style="bold magenta")))

        # Summary table
        table = Table(show_header=True, header_style="bold")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right")

        table.add_row("PRs Processed", str(self.state.total_processed))
        table.add_row("Successful", f"[green]{self.state.successful}[/green]")
        table.add_row("Failed", f"[red]{self.state.failed}[/red]")
        
        # Add detailed breakdown
        if self.state.failed > 0:
            table.add_row("", "")  # Spacer
            table.add_row("[bold]Failure Breakdown:[/bold]", "")
            if self.state.trivial_prs:
                table.add_row("  Trivial PRs", str(len(self.state.trivial_prs)))
            if self.state.no_issue_prs:
                table.add_row("  No Linked Issue", str(len(self.state.no_issue_prs)))
            if self.state.no_tests_prs:
                table.add_row("  No Tests", str(len(self.state.no_tests_prs)))
            if self.state.validation_failed_prs:
                table.add_row("  Validation Failed", str(len(self.state.validation_failed_prs)))
            if self.state.already_exists_prs:
                table.add_row("  Already Exists", str(len(self.state.already_exists_prs)))
            if self.state.rate_limit_prs:
                table.add_row("  Rate Limited", str(len(self.state.rate_limit_prs)))
            if self.state.quota_exceeded_prs:
                table.add_row("  Quota Exceeded", str(len(self.state.quota_exceeded_prs)))
            if self.state.timeout_prs:
                table.add_row("  Timeouts", str(len(self.state.timeout_prs)))
            if self.state.git_error_prs:
                table.add_row("  Git Errors", str(len(self.state.git_error_prs)))
            if self.state.other_failed_prs:
                table.add_row("  Other Errors", str(len(self.state.other_failed_prs)))

        self.console.print(table)

        if self.state.successful > 0:
            success_rate = (self.state.successful / self.state.total_processed) * 100
            self.console.print(
                f"\n[green]✓ Generated {self.state.successful} tasks successfully! "
                f"({success_rate:.1f}% success rate)[/green]"
            )
            self.console.print("[dim]Tasks located in: tasks/[/dim]")

        log_path = self._get_log_path()
        self.console.print(f"\n[dim]Detailed log: {log_path}[/dim]")
        self.console.print(f"[dim]State saved: {self.state_file}[/dim]")

    def _save_log(self) -> None:
        """Save results log to file."""
        log_path = self._get_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "repo": self.repo,
            "stats": self.state.to_dict(),
            "args": {
                "require_tests": True,
                "timeout": self.config.timeout,
            },
            "results": [asdict(r) for r in self.results],
        }

        log_path.write_text(json.dumps(payload, indent=2))

    def _get_log_path(self) -> Path:
        """Get the log file path.

        Returns:
            Path to the log file for this session
        """
        slug = _slug(self.repo).replace("-", "_")
        timestamp = datetime.fromisoformat(
            self.state.last_updated or _now_utc().isoformat()
        ).strftime("%Y%m%d_%H%M%S")
        return self.config.state_dir / "logs" / f"stream_farm_{slug}_{timestamp}.json"
