from __future__ import annotations

from pathlib import Path

from swegen.config import PublishConfig

from .base import (
    PublishAuthError,
    PublishContext,
    PublishError,
    PublishResult,
    StateStore,
    TaskSink,
)
from .git_ops import GitError, GitRepo, slug
from .github_pr import GitHubPRSink, default_clone_dir
from .null import NullSink

# NOTE: git_state/local_state are imported lazily inside build_state_store. They import
# swegen.farm.state, whose package __init__ pulls in StreamFarmer, which imports this
# module - importing them at module scope would be a circular import.


def build_task_sink(
    publish: PublishConfig | None,
    source_repo: str,
    state_dir: Path = Path(".swegen"),
) -> TaskSink:
    """Return the sink for `source_repo`. NullSink when publishing is disabled."""
    if publish is None:
        return NullSink()
    return GitHubPRSink(publish, source_repo, state_dir=state_dir)


def build_state_store(
    publish: PublishConfig | None,
    source_repo: str,
    local_state_file: Path,
    state_dir: Path = Path(".swegen"),
) -> StateStore:
    """Return the state store for `source_repo`.

    With publishing enabled, state is committed to a branch on the dataset repo and
    also mirrored locally for debugging.
    """
    from .git_state import GitStateStore
    from .local_state import LocalStateStore

    if publish is None:
        return LocalStateStore(local_state_file)
    return GitStateStore(
        publish,
        source_repo,
        clone_dir=default_clone_dir(publish, source_repo, state_dir),
        local_mirror=local_state_file,
    )


__all__ = [
    "GitError",
    "GitHubPRSink",
    "GitRepo",
    "NullSink",
    "PublishAuthError",
    "PublishContext",
    "PublishError",
    "PublishResult",
    "StateStore",
    "TaskSink",
    "build_state_store",
    "build_task_sink",
    "default_clone_dir",
    "slug",
]
