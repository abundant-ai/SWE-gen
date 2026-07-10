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
from swegen.publish import PublishError, build_state_store, build_task_sink

from .farm_hand import (
    PRCandidate,
    TaskResult,
    _now_utc,
    _run_reversal_for_pr,
    _slug,
)
from .fetcher import StreamingPRFetcher, load_skip_list
from .state import StreamState

DOCKER_CLEANUP_CMD = "docker system prune -af"


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
        self.tasks_root.mkdir(parents=True, exist_ok=True)

        # State file path (also the local mirror when publishing)
        self.state_file = config.state_dir / "stream_farm" / f"{_slug(repo)}.json"

        # Preflight the sink before any PR is processed: a bad token must fail in
        # seconds, not after the first hour-long Claude Code session.
        self.sink = build_task_sink(config.publish, repo, state_dir=config.state_dir)
        if config.publish is not None:
            self.console.print(
                f"[cyan]Publishing tasks to {config.publish.repo} "
                f"(base: {config.publish.base_branch})[/cyan]"
            )
            self.sink.preflight()

        self.state_store = build_state_store(
            config.publish, repo, self.state_file, state_dir=config.state_dir
        )

        # Load or create state
        if config.reset:
            self.state = StreamState(repo=repo)
            self.console.print("[yellow]State reset - starting fresh[/yellow]")
            if config.publish is not None:
                # --reset means "start from the beginning", and it does. But with a remote
                # state branch that discards a cursor shared across sandboxes, not just a
                # local file: every PR recorded there will be regenerated from scratch.
                self.console.print(
                    f"[bold yellow]WARNING: --reset will overwrite the durable state "
                    f"branch {self.state_store.branch} on {config.publish.repo} at the "
                    f"first save. Previously processed PRs will be regenerated.[/bold yellow]"
                )
        else:
            self.state = self.state_store.load(repo)

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
        self.aborted = False
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
            Exit code: 0 if any tasks succeeded, 1 otherwise. Aborting because publishing
            broke is always a failure, even if earlier tasks published fine.
        """
        self._print_header()

        # Start streaming and processing
        try:
            self._run_stream()
        except KeyboardInterrupt:
            self.console.print("\n[yellow]Interrupted by user[/yellow]")
        finally:
            self._finalize()

        if self.aborted:
            return 1
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
        if result.category == "publish_failed":
            # Do NOT consume the PR. The task is valid and only publishing failed, so the
            # next run must retry it rather than skip it. Publishing is idempotent: it
            # finds the stale branch, recommits, and opens the PR that never got created.
            self.state.mark_publish_failed(pr.number)
        elif result.status == "success" and self._publish_is_dry_run():
            # Also do NOT consume the PR. The task was generated and validated but nothing
            # reached the dataset repo, so a later real run must still farm this PR. The
            # local mirror persists, and without this a dry run would silently poison it.
            self.console.print(
                f"[cyan]DRY RUN: task built but not published; leaving PR #{pr.number} "
                f"unprocessed so a real run will farm it[/cyan]"
            )
        else:
            self.state.mark_processed(
                pr.number,
                pr.created_at,
                result.status == "success",
                task_id=result.task_id if result.status == "success" else None,
                category=result.category,
                message=result.message if result.category == "other" else None,
                pr_url=result.pr_url,
            )
        state_saved = self._save_state()

        # Show result
        self._print_result(result)

        # A publish failure or a failed state push ends the run. Checked together: both can
        # fail in the same iteration, and reporting only the first would tell the operator
        # farm state was preserved when it was not.
        if self._check_publish_health(result, state_saved):
            return

        # Rate limit protection: sleep between PRs
        self.console.print(f"[dim]Waiting {self.config.task_delay} seconds before next PR...[/dim]")
        time.sleep(self.config.task_delay)

        # Periodic summary
        if self.state.total_processed % 10 == 0:
            self._print_progress()

        # Docker cleanup after batch
        if self.config.docker_prune_batch > 0:
            if self.state.total_processed % self.config.docker_prune_batch == 0:
                self._prune_docker()

    def _publish_is_dry_run(self) -> bool:
        """True when publishing is configured but pushes and PR creation are suppressed."""
        return self.config.publish is not None and self.config.publish.dry_run

    def _abort(self, reason: str, detail: str = "", state_saved: bool = True) -> None:
        """Stop the run loudly, so an operator can reach the sandbox before reclamation.

        `state_saved` must reflect whether the durable state branch actually advanced. A
        publish failure and a state-push failure can happen in the same iteration, and
        telling the operator that state was preserved when it was not sends them into the
        next run expecting a resume that will not happen.
        """
        self.aborted = True
        self.shutdown_requested = True
        body = reason
        if detail:
            body += f"\n\n{detail}"

        if state_saved:
            body += (
                "\n\nFix the token or GitHub connectivity, then re-run. Farm state is "
                "preserved, so already-processed PRs will not be regenerated."
            )
        else:
            body += (
                "\n\nThe durable state branch was NOT updated: this run's progress exists "
                f"only in the local mirror ({self.state_file}). Recover it before this "
                "sandbox is reclaimed, or a re-run will regenerate the PRs processed here."
            )

        self.console.print(
            Panel(Text(body, style="red"), title="[red]Stopping[/red]", border_style="red")
        )

    def _check_publish_health(self, result: TaskResult, state_saved: bool) -> bool:
        """Stop the run on a publish failure or a failed state push. True = stop farming.

        Both are checked together because they can fail in the same iteration - a broken
        GitHub breaks the task push and the state push alike - and the operator needs to
        hear about both. No retry-and-continue: a rejected push cannot be distinguished
        from a broken remote, and continuing would spend a full Claude Code session per PR
        only to fail at the same wall. Stopping loudly is what lets an operator reach a
        Daytona sandbox and recover the task before it is reclaimed.
        """
        publish_failed = result.category == "publish_failed"
        if not publish_failed and state_saved:
            return False

        if publish_failed:
            reason = result.message
            detail = (
                f"The task passed every validation gate and was left in "
                f"{self.tasks_root}/{result.task_id} - it can be pushed by hand.\n"
                f"On an ephemeral sandbox, recover it before the sandbox is reclaimed.\n"
                f"PR #{result.pr_number} was left unprocessed, so a re-run will retry it."
            )
        else:
            reason = "Farm state could not be pushed to the state branch."
            detail = (
                "Continuing would leave the durable cursor stale, so a resumed sandbox "
                "would regenerate every PR processed from this point on."
            )

        self._abort(reason, detail, state_saved=state_saved)
        return True

    def _print_result(self, result: TaskResult) -> None:
        """Print the result of processing a PR.

        Args:
            result: The TaskResult to display
        """
        if result.status == "success":
            self.console.print(f"[green]✓ Success: {result.message}[/green]")
            if result.pr_url:
                self.console.print(f"[green]  PR: {result.pr_url}[/green]")
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
        """Run docker cleanup to free disk space.

        WARNING: `docker system prune -af` is global to the daemon. Two farm processes
        sharing a host will destroy each other's images and build cache mid-validation.
        One container per repo is the intended deployment; if you colocate farms, pass
        --docker-prune-batch 0 to all but one.
        """
        if shutil.which("docker") is None:
            self.console.print(
                "[yellow]Skipping docker prune (docker binary not found in PATH).[/yellow]"
            )
            return

        self.console.print(
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
                stdout = result.stdout.strip()
                if stdout:
                    # Show summary if available
                    lines = stdout.split("\n")
                    summary_lines = [
                        line
                        for line in lines
                        if "reclaimed" in line.lower()
                        or "deleted" in line.lower()
                        or "total" in line.lower()
                    ]
                    if summary_lines:
                        self.console.print(f"[dim]{summary_lines[0]}[/dim]")
                self.console.print("[green]Docker cleanup completed[/green]")
            else:
                self.console.print(f"[red]Docker cleanup failed (exit {result.returncode})[/red]")
                if result.stderr:
                    self.console.print(f"[red]{result.stderr.strip()}[/red]")
        except subprocess.TimeoutExpired:
            self.console.print("[red]Docker prune timed out after 600s[/red]")

    def _save_state(self) -> bool:
        """Persist state via the configured store. Returns False if it could not be saved.

        With publishing enabled this pushes to the state branch, so a sandbox killed
        mid-run resumes from the last completed PR. _finalize() runs from a finally
        block, which means a Daytona SIGTERM flushes state before exit.

        A failure here is as serious as a failed task publish: if the state branch stops
        advancing, a resumed sandbox redoes work it already paid for. The caller stops
        the run. The local mirror is still written, so nothing is lost from disk.
        """
        try:
            self.state_store.save(self.state)
            return True
        except PublishError as e:
            self.console.print(f"[red]Could not persist farm state: {e}[/red]")
            return False

    def _finalize(self) -> None:
        """Finalize the run and print summary.

        Runs from a finally block, so a Daytona SIGTERM flushes state before exit. A final
        state push that fails is recorded as an abort: the durable cursor is now behind the
        work actually done, and a resumed sandbox would redo it. Exiting 0 would tell a
        supervisor everything was fine.
        """
        if not self._save_state():
            self.aborted = True
            self.console.print(
                "[red]Final state push failed - the durable cursor is stale. "
                "Recover the local state mirror before this sandbox is reclaimed.[/red]"
            )
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
            if self.state.publish_failed_prs:
                table.add_row("  Publish Failed", str(len(self.state.publish_failed_prs)))
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
