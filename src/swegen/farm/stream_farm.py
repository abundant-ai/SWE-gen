from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import time
import traceback
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
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

# Harbor names every task image "hb__{environment_name}" (see harbor's docker
# environment). Matching that prefix lets us reclaim the per-task images -- which are
# the ones that actually consume disk -- without touching base images (ubuntu, language
# runtimes) or any unrelated image on the host, both of which `docker system prune -af`
# would happily delete.
HARBOR_IMAGE_GLOB = "hb__*"

# The run report is committed to the state branch, so every free-text field is capped.
_TRACEBACK_TAIL_LINES = 40
_DETAIL_MAX_CHARS = 2000
_MESSAGE_MAX_CHARS = 500
_REASON_MAX_CHARS = 1000

# The final save carries the run report. If it never lands, the branch keeps the "running"
# marker and a crashed run is indistinguishable from an OOM kill - so retry it.
_FINAL_SAVE_ATTEMPTS = 3
_FINAL_SAVE_BACKOFF_SECONDS = 3.0


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
            config.publish, repo, self.state_file, state_dir=config.state_dir, reset=config.reset
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

        # PRs handled this run. Distinct from state.total_processed, which counts PRs
        # *consumed* - a dry run and a pending publish deliberately consume nothing, and
        # driving the prune/progress cadence off a counter that never moves would fire
        # them on every iteration.
        self.prs_seen = 0

        # Run-report bookkeeping. current_pr is set only while a PR is in flight: on a
        # crash, its absence says we died BETWEEN PRs, which points at the fetcher rather
        # than at task generation.
        self.run_started_at = _now_utc()
        self.current_pr: int | None = None

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

        Every exit path records a run report (a "death certificate") into the state before
        _finalize() pushes it, so the state branch always explains why the run stopped.
        Without it a crash and a clean completion are indistinguishable - the state simply
        stops advancing, which is useless when the sandbox that held the traceback is gone.

        Returns:
            Exit code: 0 only on a clean run that produced tasks. Aborting (publish/state
            failure, Claude rate limit) or crashing is always 1.
        """
        self._print_header()

        # Mark the run as in-flight and push it. This is what makes a SILENT death
        # detectable: if the sandbox is OOM-killed or reclaimed, nothing overwrites this,
        # so the state branch is left saying outcome="running" with a stale timestamp -
        # which is exactly the signature of "killed without getting to report".
        self._record_outcome("running", "Run in progress")
        if not self._save_state():
            # The state branch is unwritable (bad token, branch protection, GitHub down).
            # Stop NOW rather than farming: the first PR would spend a full Claude Code
            # session and only then hit the same failure in _process_pr and abort anyway.
            # Failing here costs zero sessions. The report still reaches the local mirror,
            # which GitStateStore writes before it pushes.
            self._abort(
                "Farm state could not be pushed to the state branch at startup.",
                "Nothing has been farmed yet. Fix the token or GitHub connectivity and "
                "re-run - stopping here avoids burning a Claude Code session on the first "
                "PR just to fail at the same wall.",
                state_saved=False,
            )
            self._finalize()
            return 1

        try:
            self._run_stream()
            self._record_stream_end()
        except KeyboardInterrupt:
            self.console.print("\n[yellow]Interrupted by user[/yellow]")
            self._record_outcome("interrupted", "Interrupted by user (SIGINT)")
        except Exception as e:
            # Anything the per-PR handler did not already catch - most commonly the
            # StreamingPRFetcher raising while fetching the next page (GitHub 5xx, network,
            # rate limit). Previously this escaped run() as a raw traceback, so the state
            # branch showed a healthy run that simply stopped, and the only explanation
            # died with the sandbox.
            self._record_crash(e)
        finally:
            # Pushes the state, run report included.
            self._finalize()

        if self.aborted:
            return 1
        return 0 if self.state.successful > 0 else 1

    def _record_stream_end(self) -> None:
        """Classify a stream that ended without raising. Three very different things.

        The generator returns identically whether it ran out of PRs, gave up on a GitHub
        API error, or was cut short by a shutdown signal - so each has to be teased apart
        explicitly, or a farm killed by a GitHub outage gets filed as a clean finish.
        """
        if self.aborted:
            # _abort() already wrote the report, with its own reason.
            return

        # stop_reason is checked BEFORE shutdown_requested on purpose. A signal can arrive
        # while a page fetch is failing, and the API failure is the substantive cause: it is
        # what a re-run has to overcome. Checking the signal first would drop stop_reason
        # AND file the run as `interrupted`, which does not set aborted - so run() could
        # return 0 on a stream failure, reporting a broken run as a healthy one.
        if self.fetcher.stop_reason:
            # The fetcher gave up on a page fetch and broke out. Not exhaustion, and not a
            # crash: the run stopped early and should be retried.
            self.aborted = True  # exit 1: this run did not finish its work
            reason = self.fetcher.stop_reason
            if self.shutdown_requested:
                reason = f"{reason} (a shutdown signal also arrived)"
            self._record_outcome("stream_failed", reason)
            self.console.print(
                Panel(
                    Text(
                        f"{reason}\n\n"
                        f"The PR stream stopped early - this is NOT an exhausted history. "
                        f"Re-run to continue from the cursor.",
                        style="red",
                    ),
                    title="[red]Stopping: PR stream failed[/red]",
                    border_style="red",
                )
            )
            return

        if self.shutdown_requested:
            # SIGINT/SIGTERM. _handle_shutdown only sets a flag - it never raises - so the
            # KeyboardInterrupt branch in run() does NOT fire for a signal, and without
            # this the report would be left saying "running": the silent-kill signature.
            # SIGTERM is exactly what a sandbox stop sends, so this must not look like a
            # crash.
            self._record_outcome("interrupted", "Shutdown requested (SIGINT/SIGTERM)")
            return

        self._record_outcome("completed", "PR stream exhausted")

    def _recent_results(self, limit: int = 5) -> list[dict]:
        """The last few PR outcomes, for context on what the farm was doing when it died."""
        recent = []
        for r in self.results[-limit:]:
            entry = {"pr": r.pr_number, "status": r.status, "category": r.category}
            if r.message:
                entry["message"] = r.message[:_MESSAGE_MAX_CHARS]
            if r.pr_url:
                entry["pr_url"] = r.pr_url
            recent.append(entry)
        return recent

    def _environment_report(self) -> dict:
        """Host / version / config context. All of it is needed to reproduce a crash."""

        def _ver(pkg: str) -> str | None:
            try:
                return _pkg_version(pkg)
            except PackageNotFoundError:
                return None

        pub = self.config.publish
        return {
            "host": platform.node(),
            # Set by Daytona inside the sandbox; lets a report be tied back to a container.
            "sandbox_id": os.environ.get("DAYTONA_SANDBOX_ID"),
            "versions": {
                "swegen": _ver("swegen"),
                "claude_agent_sdk": _ver("claude-agent-sdk"),
                "harbor": _ver("harbor"),
                "python": platform.python_version(),
            },
            "config": {
                "environment": str(self.config.environment),
                "cc_timeout": self.config.cc_timeout,
                "require_issue": self.config.require_issue,
                "docker_prune_batch": self.config.docker_prune_batch,
                "publish_repo": pub.repo if pub else None,
                "cleanup_local": pub.cleanup_local if pub else None,
                "dry_run": self.config.dry_run,
            },
        }

    def _record_outcome(
        self,
        outcome: str,
        reason: str,
        detail: str | None = None,
        error_type: str | None = None,
        traceback_tail: str | None = None,
    ) -> None:
        """Write the run report into the state, to be pushed on the next save.

        outcome: running | completed | aborted | crashed | interrupted

        Deliberately verbose: this is committed to the state branch and is often the ONLY
        surviving evidence once an ephemeral sandbox is gone. It stays bounded (a capped
        traceback tail, the last few results) so the branch does not bloat, and it is only
        written on run start and run exit - not per PR.
        """
        now = _now_utc()
        report: dict = {
            "outcome": outcome,
            "reason": reason[:_REASON_MAX_CHARS],
            "started_at": self.run_started_at.isoformat(),
            "ended_at": now.isoformat() if outcome != "running" else None,
            "duration_seconds": round((now - self.run_started_at).total_seconds(), 1),
            "prs_seen_this_run": self.prs_seen,
            # Set only while a PR is in flight. Its ABSENCE on a crash is itself a clue: we
            # died between PRs, which points at the fetcher rather than task generation.
            "pr_in_flight": self.current_pr,
            "last_pr_number": self.state.last_pr_number,
            "last_created_at": self.state.last_created_at,
            "counters": {
                "successful": self.state.successful,
                "failed": self.state.failed,
                "total_processed": self.state.total_processed,
            },
            "pending_retry": {
                "publish_failed": sorted(self.state.publish_failed_prs),
                "claude_rate_limited": sorted(self.state.claude_rate_limited_prs),
            },
            "recent_results": self._recent_results(),
            "environment": self._environment_report(),
        }
        if detail:
            report["detail"] = detail[:_DETAIL_MAX_CHARS]
        if error_type:
            report["error_type"] = error_type
        if traceback_tail:
            report["traceback"] = traceback_tail
        self.state.last_run = report

    def _record_crash(self, exc: BaseException) -> None:
        """Record an unhandled exception and show it, instead of dying with a bare trace."""
        tb = traceback.format_exc()
        # Keep the tail: the frames nearest the failure are the informative ones, and the
        # report is committed to a git branch, so it must stay bounded. format_exc()
        # includes chained causes ("during handling of...", "caused by"), which we want.
        tail = "\n".join(tb.strip().splitlines()[-_TRACEBACK_TAIL_LINES:])

        self.aborted = True
        self.shutdown_requested = True
        self._record_outcome(
            "crashed",
            f"{type(exc).__name__}: {exc}",
            error_type=type(exc).__name__,
            traceback_tail=tail,
        )

        where = (
            f"while processing PR #{self.current_pr}"
            if self.current_pr is not None
            else "between PRs (most likely fetching the next page of PRs)"
        )
        self.console.print(
            Panel(
                Text(
                    f"{type(exc).__name__}: {exc}\n\n"
                    f"Crashed {where}.\n"
                    f"The run report is being pushed to the state branch, so this is "
                    f"recoverable even if the sandbox is reclaimed.\n\n{tail}",
                    style="red",
                ),
                title="[red]Farm crashed[/red]",
                border_style="red",
            )
        )

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
            f"\n[bold cyan]═══ PR #{pr.number} ({self.prs_seen + 1}) ═══[/bold cyan]"
        )
        self.console.print(f"[bold]{pr.title}[/bold]")
        self.console.print(
            f"[dim]Merged: {merged_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} | "
            f"Files: {pr.files_changed} | "
            f"+{pr.additions}/-{pr.deletions}[/dim]"
        )

        # A PR left pending by an earlier publish failure already has a validated task on
        # disk. Republish it instead of regenerating - force=True would delete it first.
        publish_only = pr.number in self.state.publish_failed_prs

        # Held for the duration of this PR, and deliberately NOT cleared in a finally: if we
        # crash here it must survive into the run report. Cleared only once the PR is fully
        # handled (see the end of this method), so a crash with pr_in_flight=None means we
        # died between PRs - which points at the fetcher rather than at task generation.
        self.current_pr = pr.number

        result = _run_reversal_for_pr(
            pr, self.config, self.tasks_root, self.console, publish_only=publish_only
        )
        self.results.append(result)

        self.prs_seen += 1

        # Mark as processed with detailed tracking
        if result.category == "rate_limited":
            # Do NOT consume the PR. Claude hit a rate/usage limit - nothing was generated,
            # and a re-run with a fresh token must still farm this PR. Record it (not just
            # skip) so the fetcher exempts it from the resume-time skip; otherwise a PR
            # sharing the cursor's exact created_at would be dropped and never retried.
            # The run aborts below.
            self.state.mark_claude_rate_limited(pr.number)
        elif result.category == "publish_failed":
            # Do NOT consume the PR. The task is valid and only publishing failed, so the
            # next run must retry it rather than skip it. That retry is publish-only (see
            # publish_only above): publishing is idempotent, so it finds the branch this
            # task left behind, refreshes it, and opens the PR that never got created.
            self.state.mark_publish_failed(pr.number)
        elif result.status == "dry-run":
            # --dry-run generates nothing. Recording the PR would consume it AND file it
            # under other_failed_prs as "Unknown error" (success=False, category=None),
            # so a later real run would skip a PR that was never farmed.
            pass
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
        # farm state was preserved when it was not. current_pr stays set here on purpose:
        # the abort report should name the PR we stopped on.
        if self._check_publish_health(result, state_saved):
            return

        # This PR is fully handled. Anything that crashes from here to the next PR is the
        # fetcher's, and the report will say pr_in_flight=None.
        self.current_pr = None

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

        # Periodic summary. Driven by PRs seen this run, not PRs consumed: dry runs and
        # pending publishes consume nothing, and `0 % n == 0` would fire every iteration.
        if self.prs_seen % 10 == 0:
            self._print_progress()

        # Docker cleanup after batch
        if self.config.docker_prune_batch > 0:
            if self.prs_seen % self.config.docker_prune_batch == 0:
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

        # Land the reason in the run report too, so the state branch explains the abort
        # without anyone needing the console output (which dies with the sandbox).
        self._record_outcome("aborted", reason, detail=detail or None)

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
        """Stop the run on a fatal condition. True = stop farming.

        Fatal: a publish failure, a failed state push, or a Claude rate/usage limit. Each
        will recur on the next PR - a broken remote, or an exhausted token - so continuing
        would spend a full Claude Code session per PR only to fail at the same wall.
        Publish and state-push are checked together because a broken GitHub hits both in one
        iteration and the operator needs to hear about both. Stopping loudly is what lets an
        operator reach a Daytona sandbox before it is reclaimed.
        """
        rate_limited = result.category == "rate_limited"
        publish_failed = result.category == "publish_failed"
        if not rate_limited and not publish_failed and state_saved:
            return False

        if rate_limited:
            reason = result.message
            detail = (
                "Every task draws from the same limit, so this will not clear until the "
                "token or account is swapped. Re-run with a fresh token.\n"
                f"PR #{result.pr_number} was left unprocessed, so it will be farmed then."
            )
        elif publish_failed:
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

    def _should_delay_after(self, result: TaskResult) -> bool:
        """Whether to sleep before the next PR.

        Only PRs that reached the Claude Code session need the delay. Dry runs and
        PRs rejected by a cheap filter (trivial, no linked issue, no tests, already
        exists) did no meaningful API work, so waiting on them is dead time.

        Note "rate_limited" and "publish_failed" are deliberately absent from
        SKIPPED_CATEGORIES: both mean a CC session ran. They abort the run in
        _check_publish_health before the delay is reached anyway.
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
        # This is the most consequential save of the run: it carries the run report. If it
        # never lands, the state branch is left holding the "running" marker from startup -
        # which is our silent-kill signature - so a run that actually crashed or completed
        # would be indistinguishable from one that was OOM-killed. Worth retrying rather
        # than accepting on a single transient failure.
        if not self._save_state_with_retry():
            self.aborted = True
            self.console.print(
                "[red]Final state push failed after "
                f"{_FINAL_SAVE_ATTEMPTS} attempts - the durable cursor is stale and the "
                "state branch still says 'running'. The full run report IS in the local "
                f"mirror ({self.state_file}); recover it before this sandbox is "
                "reclaimed.[/red]"
            )
        self._save_log()

    def _save_state_with_retry(self) -> bool:
        """Save the state, retrying a transient push failure with backoff."""
        for attempt in range(1, _FINAL_SAVE_ATTEMPTS + 1):
            if self._save_state():
                return True
            if attempt == _FINAL_SAVE_ATTEMPTS:
                break
            delay = _FINAL_SAVE_BACKOFF_SECONDS * attempt
            self.console.print(
                f"[yellow]State push failed; retrying in {delay:.0f}s "
                f"(attempt {attempt}/{_FINAL_SAVE_ATTEMPTS})[/yellow]"
            )
            time.sleep(delay)
        return False

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
            if self.state.claude_rate_limited_prs:
                table.add_row(
                    "  Claude Rate-Limited", str(len(self.state.claude_rate_limited_prs))
                )
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
