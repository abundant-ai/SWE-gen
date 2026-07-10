from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class PublishError(RuntimeError):
    """A task could not be published.

    Usually the task was generated and validated and only the push or PR creation failed;
    it is also raised when a task recorded in state is missing from disk.

    Always ends the farm run. Continuing would spend a full Claude Code session on the
    next PR only to fail at the same wall, and a task that exists solely on an ephemeral
    sandbox is one reclaim away from being lost. Where the task directory exists it is
    preserved - never cleaned up - so an operator can publish it by hand, and a re-run
    retries the publish without regenerating it.
    """


class PublishAuthError(PublishError):
    """The publish token is missing, invalid, or lacks write access.

    Never retried - retrying would hammer a rejecting endpoint. Raised at preflight,
    before any PR is processed.
    """


@dataclass(frozen=True)
class PublishContext:
    """Everything a sink needs to publish one generated task."""

    task_id: str
    task_dir: Path
    source_repo: str
    source_pr: int
    source_pr_url: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PublishResult:
    """Outcome of publishing one task."""

    published: bool
    pr_url: str | None = None
    branch: str | None = None


class TaskSink(Protocol):
    """Destination for generated tasks.

    Implementations must be idempotent: publishing a task that was already published
    returns the existing result rather than raising, so a restarted sandbox that
    regenerates a task does not fail on it.
    """

    def preflight(self) -> None:
        """Verify the destination is reachable and writable. Raises PublishAuthError."""
        ...

    def publish(self, ctx: PublishContext) -> PublishResult:
        """Publish one task. Raises PublishError on unrecoverable failure."""
        ...


class StateStore(Protocol):
    """Persistence backend for farm state."""

    def load(self, repo: str):
        """Load StreamState for `repo`, or a fresh one if absent."""
        ...

    def save(self, state) -> None:
        """Persist StreamState. Must be safe to call after every PR."""
        ...
