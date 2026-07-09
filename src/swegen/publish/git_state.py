from __future__ import annotations

import json
import logging
from pathlib import Path

from swegen.config import PublishConfig
from swegen.farm.state import StreamState

from .git_ops import GitRepo, slug


class GitStateStore:
    """Farm state committed to a per-source-repo branch on the dataset repo.

    This is what lets an ephemeral sandbox resume: without it a fresh container
    restarts from the newest PR and re-burns Claude Code and OpenAI calls on every PR
    it previously rejected, which is most of them.

    The branch is `<state_branch_prefix><source_slug>` (e.g. farm-state/fastapi__fastapi).
    Per-source-repo naming means N containers farming N repos never contend. The branch
    is orphaned and never merged, and lives in a linked worktree so state pushes do not
    disturb the task branch checked out in the main tree.

    Git refs are a directory namespace: `farm-state/fastapi__fastapi` cannot coexist with
    a bare `farm-state` branch. Only the prefixed form is ever created.
    """

    def __init__(
        self,
        cfg: PublishConfig,
        source_repo: str,
        clone_dir: Path,
        *,
        git: GitRepo | None = None,
        local_mirror: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.source_repo = source_repo
        self.clone_dir = Path(clone_dir)
        self.local_mirror = local_mirror
        self.logger = logging.getLogger("swegen")
        self.git = git or GitRepo(
            self.clone_dir,
            token=cfg.token,
            author_name=cfg.author_name,
            author_email=cfg.author_email,
            dry_run=cfg.dry_run,
        )
        self.branch = f"{cfg.state_branch_prefix}{slug(source_repo)}"
        self.worktree = self.clone_dir.parent / f"{self.clone_dir.name}-state"
        self._ready = False

    @property
    def state_file(self) -> Path:
        return self.worktree / self.cfg.state_path / f"{slug(self.source_repo)}.json"

    def _ensure_worktree(self) -> None:
        if self._ready:
            return
        self.git.ensure_clone(f"https://github.com/{self.cfg.repo}.git")
        self.git.add_worktree(self.worktree, self.branch, f"origin/{self.cfg.base_branch}")
        self._ready = True

    # -- StateStore protocol -------------------------------------------------

    def load(self, repo: str) -> StreamState:
        self._ensure_worktree()

        if not self.state_file.exists():
            self.logger.info("No published state for %s; starting fresh", repo)
            return StreamState(repo=repo)

        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError) as e:
            self.logger.warning(
                "Published state for %s is unreadable (%s); starting fresh", repo, e
            )
            return StreamState(repo=repo)

        if data.get("repo") != repo:
            self.logger.warning(
                "Published state is for %s, not %s; starting fresh", data.get("repo"), repo
            )
            return StreamState(repo=repo)

        state = StreamState.from_dict(data)
        self.logger.info(
            "Resumed published state for %s: %d PRs processed, %d tasks published",
            repo,
            state.total_processed,
            state.successful,
        )
        return state

    def save(self, state: StreamState) -> None:
        """Commit and push state.

        Non-fatal: a failed state push is retried on the next PR and again from
        _finalize(), so one transient failure should not abort a farm run. It is logged
        at warning level because silently losing state would cost hours of rework.
        """
        if self.local_mirror is not None:
            state.save(self.local_mirror)

        try:
            self._ensure_worktree()
            self._write_and_push(state)
        except Exception as e:
            self.logger.warning("Could not publish farm state to %s: %s", self.branch, e)

    def _write_and_push(self, state: StreamState) -> None:
        rel_path = f"{self.cfg.state_path}/{slug(self.source_repo)}.json"
        refspec = f"{self.branch}:refs/heads/{self.branch}"

        self._commit_state(state, rel_path)
        try:
            self.git.push(refspec, cwd=self.worktree)
            return
        except Exception as e:
            self.logger.warning("State push rejected (%s); rebasing onto remote and retrying", e)

        # The remote moved under us. Our state is a full snapshot, so reset onto the
        # remote tip and re-apply it as an additive commit rather than force-pushing.
        self.git.git("fetch", "origin", self.branch, cwd=self.worktree, network=True)
        self.git.git("reset", "--hard", f"origin/{self.branch}", cwd=self.worktree)
        self._commit_state(state, rel_path)
        self.git.push(refspec, cwd=self.worktree)

    def _commit_state(self, state: StreamState, rel_path: str) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state.to_dict(), indent=2))
        self.git.add(rel_path, cwd=self.worktree)
        self.git.commit(
            f"Update farm state: {self.source_repo} (PR #{state.last_pr_number})",
            cwd=self.worktree,
        )
