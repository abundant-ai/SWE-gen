from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class StreamState:
    """State for resumable streaming PR processing.

    Tracks which PRs have been processed, success/failure counts,
    and the last processed PR for resume capability.

    Attributes:
        repo: Repository name in "owner/repo" format
        processed_prs: Set of PR numbers that have been processed
        total_fetched: Total PRs fetched from API
        total_processed: Total PRs processed (attempted)
        successful: Count of successfully generated tasks
        failed: Count of failed task generations
        last_pr_number: Last processed PR number
        last_created_at: ISO timestamp of last processed PR's creation time
        last_updated: ISO timestamp of last state update
        skip_list_prs: Set of PR numbers to skip (from external skip list)
        
        # Detailed categorization
        successful_prs: dict[int, str] = None  # PR# -> task_id
        task_pr_urls: dict[int, str] = None  # PR# -> URL of the published task PR
        publish_failed_prs: set[int] = None  # Task built but could not be published
        trivial_prs: set[int] = None  # Trivial PRs (too small/simple)
        no_issue_prs: set[int] = None  # PRs without linked issues
        no_tests_prs: set[int] = None  # PRs that don't modify tests
        validation_failed_prs: set[int] = None  # Failed Harbor validation
        already_exists_prs: set[int] = None  # Task already exists
        rate_limit_prs: set[int] = None  # GitHub API rate limit
        quota_exceeded_prs: set[int] = None  # OpenAI quota exceeded
        timeout_prs: set[int] = None  # Command timeouts
        git_error_prs: set[int] = None  # Git checkout/commit errors
        other_failed_prs: dict[int, str] = None  # PR# -> error message
    """

    repo: str
    processed_prs: set[int] = None
    total_fetched: int = 0
    total_processed: int = 0
    successful: int = 0
    failed: int = 0
    last_pr_number: int | None = None
    last_created_at: str | None = None
    last_updated: str | None = None
    # Death certificate for the most recent run: why it stopped. Written on every exit
    # path (completed / aborted / crashed / interrupted) and pushed with the state, so a
    # reclaimed sandbox still leaves an explanation behind. Without it, a crash and a
    # clean completion are indistinguishable - the state simply stops advancing.
    last_run: dict | None = None
    skip_list_prs: set[int] = None
    
    # Detailed categorization
    successful_prs: dict[int, str] = None  # PR# -> task_id
    task_pr_urls: dict[int, str] = None  # PR# -> URL of the published task PR
    publish_failed_prs: set[int] = None
    # Claude rate/usage limit aborted the run on this PR. Distinct from rate_limit_prs
    # (GitHub API limit, a consumed skip). Not consumed: exempted from the resume-time
    # skip so a re-run with a fresh token retries it, like publish_failed_prs.
    claude_rate_limited_prs: set[int] = None
    trivial_prs: set[int] = None
    no_issue_prs: set[int] = None
    no_tests_prs: set[int] = None
    validation_failed_prs: set[int] = None
    already_exists_prs: set[int] = None
    rate_limit_prs: set[int] = None
    quota_exceeded_prs: set[int] = None
    timeout_prs: set[int] = None
    git_error_prs: set[int] = None
    other_failed_prs: dict[int, str] = None

    def __post_init__(self):
        if self.processed_prs is None:
            self.processed_prs = set()
        if self.skip_list_prs is None:
            self.skip_list_prs = set()
        if self.successful_prs is None:
            self.successful_prs = {}
        if self.task_pr_urls is None:
            self.task_pr_urls = {}
        if self.publish_failed_prs is None:
            self.publish_failed_prs = set()
        if self.claude_rate_limited_prs is None:
            self.claude_rate_limited_prs = set()
        if self.trivial_prs is None:
            self.trivial_prs = set()
        if self.no_issue_prs is None:
            self.no_issue_prs = set()
        if self.no_tests_prs is None:
            self.no_tests_prs = set()
        if self.validation_failed_prs is None:
            self.validation_failed_prs = set()
        if self.already_exists_prs is None:
            self.already_exists_prs = set()
        if self.rate_limit_prs is None:
            self.rate_limit_prs = set()
        if self.quota_exceeded_prs is None:
            self.quota_exceeded_prs = set()
        if self.timeout_prs is None:
            self.timeout_prs = set()
        if self.git_error_prs is None:
            self.git_error_prs = set()
        if self.other_failed_prs is None:
            self.other_failed_prs = {}

    # Every set holding a per-PR failure/skip outcome. A PR belongs to at most one of
    # these, plus successful_prs and other_failed_prs, which are handled alongside.
    _FAILURE_SETS = (
        "trivial_prs",
        "no_issue_prs",
        "no_tests_prs",
        "validation_failed_prs",
        "already_exists_prs",
        "rate_limit_prs",
        "quota_exceeded_prs",
        "timeout_prs",
        "git_error_prs",
        "publish_failed_prs",
        "claude_rate_limited_prs",
    )

    def _clear_outcome(self, pr_number: int, *, keep_success: bool = False) -> None:
        """Remove `pr_number` from every outcome bucket.

        A PR has exactly one outcome. Recording a new one must evict the old, or the PR
        sits in two buckets at once and _recompute_counters counts it as both successful
        and failed. `keep_success` preserves successful_prs/task_pr_urls when the new
        outcome is itself a success, so a success re-recorded without a task_id does not
        erase the task_id from the first one.
        """
        for name in self._FAILURE_SETS:
            getattr(self, name).discard(pr_number)
        self.other_failed_prs.pop(pr_number, None)
        if not keep_success:
            self.successful_prs.pop(pr_number, None)
            self.task_pr_urls.pop(pr_number, None)

    def mark_processed(
        self, pr_number: int, created_at: str, success: bool, task_id: str = None,
        category: str = None, message: str = None, pr_url: str = None
    ) -> None:
        """Mark a PR as processed and update counters.

        Args:
            pr_number: The PR number that was processed
            created_at: ISO timestamp of when the PR was created
            success: Whether the task generation succeeded
            task_id: Task ID if successful (for tracking)
            category: Category of result (for detailed stats)
            message: Error/skip message (for other_failed category)
            pr_url: URL of the published task PR, if the task was published
        """
        self.processed_prs.add(pr_number)
        self.total_processed += 1

        # This outcome supersedes any earlier one - notably a pending publish failure that
        # a retry has now resolved. Leaving the PR in its old bucket would report a failure
        # that no longer exists, and double-count it against the new outcome.
        self._clear_outcome(pr_number, keep_success=success)

        if success:
            self.successful += 1
            if task_id:
                self.successful_prs[pr_number] = task_id
            if pr_url:
                self.task_pr_urls[pr_number] = pr_url
        else:
            self.failed += 1
            # Categorize the failure/skip
            if category == "trivial":
                self.trivial_prs.add(pr_number)
            elif category == "no_issue":
                self.no_issue_prs.add(pr_number)
            elif category == "no_tests":
                self.no_tests_prs.add(pr_number)
            elif category == "validation_failed":
                self.validation_failed_prs.add(pr_number)
            elif category == "already_exists":
                self.already_exists_prs.add(pr_number)
            elif category == "rate_limit":
                self.rate_limit_prs.add(pr_number)
            elif category == "quota_exceeded":
                self.quota_exceeded_prs.add(pr_number)
            elif category == "timeout":
                self.timeout_prs.add(pr_number)
            elif category == "git_error":
                self.git_error_prs.add(pr_number)
            else:
                # Other/unknown error
                self.other_failed_prs[pr_number] = message or "Unknown error"
        
        self.last_pr_number = pr_number
        self.last_created_at = created_at
        self.last_updated = datetime.now(UTC).isoformat()
        self._recompute_counters()

    def _failed_pr_numbers(self) -> set[int]:
        """Every PR with a recorded failure or skip, across all categories."""
        failed: set[int] = set(self.other_failed_prs)
        for name in self._FAILURE_SETS:
            failed |= getattr(self, name)
        return failed

    def _recompute_counters(self) -> None:
        """Derive counters from the recorded sets rather than incrementing blindly.

        Counters must be derived, not accumulated: a PR can move between categories (a
        publish failure that later succeeds on retry), and a merge can union two states.
        Incremental counting drifts in both cases and the durable state reports failures
        that no longer exist.
        """
        self.successful = len(self.successful_prs)
        self.failed = len(self._failed_pr_numbers())
        self.total_processed = len(self.processed_prs)

    def mark_publish_failed(self, pr_number: int) -> None:
        """Record a publish failure WITHOUT consuming the PR.

        Removes the PR from processed_prs (if present) and does not advance last_created_at:
        the task was generated and validated, and only the push/PR failed. Leaving the PR
        unprocessed means the next run retries it - publish-only, reusing the task already
        on disk rather than regenerating it - and the publish path is idempotent, so it
        refreshes the branch this task left behind and opens the PR that never got created.

        This is the recovery path for a push that succeeds and a create_pr that then
        fails, which would otherwise strand a branch on the remote with no PR and no way
        for the farm to reach it again.

        StreamingPRFetcher exempts these PRs from the resume-time skip, so a PR sharing the
        cursor's timestamp is still retried rather than stranded.
        """
        self._clear_outcome(pr_number)
        # A pending publish is by definition not "done". If the PR was previously recorded
        # as processed, remove it: the fetcher skips processed_prs BEFORE the publish_failed
        # exemption, so leaving it in both would make the PR skipped and never retried.
        self.processed_prs.discard(pr_number)
        self.publish_failed_prs.add(pr_number)
        self.last_updated = datetime.now(UTC).isoformat()
        self._recompute_counters()

    def mark_claude_rate_limited(self, pr_number: int) -> None:
        """Record that Claude's rate/usage limit aborted the run on this PR, without
        consuming it.

        Like mark_publish_failed: nothing was durably done, so the PR must not be counted
        as processed, and the fetcher exempts claude_rate_limited_prs from the resume-time
        skip so a re-run with a fresh token retries it even on a timestamp tie with the
        cursor. A later successful run clears it via mark_processed -> _clear_outcome.
        """
        self._clear_outcome(pr_number)
        self.processed_prs.discard(pr_number)
        self.claude_rate_limited_prs.add(pr_number)
        self.last_updated = datetime.now(UTC).isoformat()
        self._recompute_counters()

    def merge_from(self, other: StreamState) -> None:
        """Union `other` into this state, in place.

        THE RECEIVER WINS on key collisions, so call this on the FRESHER of the two states
        and pass the staler one. Sets are unioned either way - no PR is ever lost whichever
        way round it runs - but per-PR values (successful_prs, task_pr_urls,
        other_failed_prs) can genuinely differ, and a PR judged a failure by one side and a
        success by the other must end up with exactly one verdict.

        Two callers, both of which must pick the fresher receiver:
          * GitStateStore._write_and_push, when a push is rejected because the remote moved
            - rather than hard-overwriting a cursor another writer just published
          * GitStateStore.load, folding the local mirror and the state branch together

        last_created_at takes the NEWER of the two. The stream is created-descending and
        skips PRs created at or after the cursor, so a newer cursor skips less. Anything
        it re-visits is caught by processed_prs, which is the authoritative skip list. An
        older cursor could permanently skip PRs neither writer had processed.
        """
        if other.repo != self.repo:
            raise ValueError(f"Cannot merge state for {other.repo} into {self.repo}")

        # A PR carries exactly ONE outcome. Where both sides judged the same PR, the
        # receiver's verdict stands - it is the fresher state - so `other` may never add
        # that PR to a second, contradictory bucket. Snapshot our verdicts before merging.
        own_failed = self._failed_pr_numbers()
        own_verdicts = set(self.successful_prs) | own_failed

        self.processed_prs |= other.processed_prs
        self.skip_list_prs |= other.skip_list_prs
        for name in self._FAILURE_SETS:
            getattr(self, name).update(getattr(other, name) - own_verdicts)

        # Receiver wins on key collision; callers pass the staler state as `other`.
        self.successful_prs = {
            **{pr: t for pr, t in other.successful_prs.items() if pr not in own_failed},
            **self.successful_prs,
        }
        self.task_pr_urls = {**other.task_pr_urls, **self.task_pr_urls}
        self.other_failed_prs = {
            **{pr: m for pr, m in other.other_failed_prs.items() if pr not in own_verdicts},
            **self.other_failed_prs,
        }

        # Anything now recorded as successful cannot also be a failure. This scrubs the
        # staler side's failure verdict for PRs we later saw succeed; without it a PR sits
        # in two buckets and _recompute_counters scores it as both successful and failed.
        for pr_number in set(self.successful_prs):
            for name in self._FAILURE_SETS:
                getattr(self, name).discard(pr_number)
            self.other_failed_prs.pop(pr_number, None)

        # A PR still pending (publish failed, or Claude rate-limited) must stay UNCONSUMED,
        # even though the staler side recorded it as processed. Subtracting the other way
        # would let `other.processed_prs` swallow our pending retry and the PR would never
        # be farmed again.
        self.publish_failed_prs -= set(self.successful_prs)
        self.claude_rate_limited_prs -= set(self.successful_prs)
        self.processed_prs -= self.publish_failed_prs | self.claude_rate_limited_prs

        self.total_fetched = max(self.total_fetched, other.total_fetched)
        # Receiver is the fresher state, so its run report wins; fall back to the other's.
        if self.last_run is None:
            self.last_run = other.last_run
        if other.last_created_at and (
            not self.last_created_at or other.last_created_at > self.last_created_at
        ):
            self.last_created_at = other.last_created_at
            self.last_pr_number = other.last_pr_number

        self.last_updated = datetime.now(UTC).isoformat()
        self._recompute_counters()

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "repo": self.repo,
            "processed_prs": list(self.processed_prs),
            "total_fetched": self.total_fetched,
            "total_processed": self.total_processed,
            "successful": self.successful,
            "failed": self.failed,
            "last_pr_number": self.last_pr_number,
            "last_created_at": self.last_created_at,
            "last_updated": self.last_updated,
            "last_run": self.last_run,
            # Detailed breakdown
            "successful_prs": {str(k): v for k, v in self.successful_prs.items()},
            "task_pr_urls": {str(k): v for k, v in self.task_pr_urls.items()},
            "publish_failed_prs": list(self.publish_failed_prs),
            "claude_rate_limited_prs": list(self.claude_rate_limited_prs),
            "trivial_prs": list(self.trivial_prs),
            "no_issue_prs": list(self.no_issue_prs),
            "no_tests_prs": list(self.no_tests_prs),
            "validation_failed_prs": list(self.validation_failed_prs),
            "already_exists_prs": list(self.already_exists_prs),
            "rate_limit_prs": list(self.rate_limit_prs),
            "quota_exceeded_prs": list(self.quota_exceeded_prs),
            "timeout_prs": list(self.timeout_prs),
            "git_error_prs": list(self.git_error_prs),
            "other_failed_prs": {str(k): v for k, v in self.other_failed_prs.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> StreamState:
        """Load state from a dict.

        Counters are re-derived from the detailed sets rather than trusted as stored, so a
        state file written before counters became derived - or hand-edited - cannot make
        the summary panels report totals that contradict the recorded PRs.

        Args:
            data: Dict previously created by to_dict()

        Returns:
            StreamState instance
        """
        state = cls(
            repo=data["repo"],
            processed_prs=set(data.get("processed_prs", [])),
            total_fetched=data.get("total_fetched", 0),
            total_processed=data.get("total_processed", 0),
            successful=data.get("successful", 0),
            failed=data.get("failed", 0),
            last_pr_number=data.get("last_pr_number"),
            last_created_at=data.get("last_created_at"),
            last_updated=data.get("last_updated"),
            last_run=data.get("last_run"),
            # Detailed breakdown
            successful_prs={int(k): v for k, v in data.get("successful_prs", {}).items()},
            task_pr_urls={int(k): v for k, v in data.get("task_pr_urls", {}).items()},
            publish_failed_prs=set(data.get("publish_failed_prs", [])),
            claude_rate_limited_prs=set(data.get("claude_rate_limited_prs", [])),
            trivial_prs=set(data.get("trivial_prs", [])),
            no_issue_prs=set(data.get("no_issue_prs", [])),
            no_tests_prs=set(data.get("no_tests_prs", [])),
            validation_failed_prs=set(data.get("validation_failed_prs", [])),
            already_exists_prs=set(data.get("already_exists_prs", [])),
            rate_limit_prs=set(data.get("rate_limit_prs", [])),
            quota_exceeded_prs=set(data.get("quota_exceeded_prs", [])),
            timeout_prs=set(data.get("timeout_prs", [])),
            git_error_prs=set(data.get("git_error_prs", [])),
            other_failed_prs={int(k): v for k, v in data.get("other_failed_prs", {}).items()},
        )
        # Enforce the invariant on load, not just via merge_from/mark_*. A PR still pending
        # (publish failed, or Claude rate-limited) must not also be in processed_prs, or the
        # fetcher skips it as already-processed (that check runs before the resume-skip
        # exemption) and never retries. Guards against legacy state files and hand edits.
        state.processed_prs -= state.publish_failed_prs | state.claude_rate_limited_prs
        state._recompute_counters()
        return state

    def save(self, state_file: Path) -> None:
        """Save state to a JSON file.

        Args:
            state_file: Path to save state to
        """
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, state_file: Path, repo: str) -> StreamState:
        """Load state from file, or create new if not exists.

        Args:
            state_file: Path to state file
            repo: Repository name (used to verify state matches)

        Returns:
            StreamState instance (loaded or new)
        """
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                if data.get("repo") == repo:
                    return cls.from_dict(data)
            except Exception:
                pass
        return cls(repo=repo)
