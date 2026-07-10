from __future__ import annotations

import json
import logging
from pathlib import Path

from swegen.config import PublishConfig
from swegen.farm.state import StreamState

from .base import PublishError
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
        self._sync_worktree()
        self._ready = True

    def _sync_worktree(self) -> None:
        """Fast-forward the state worktree onto the remote branch.

        add_worktree() returns an existing worktree untouched, so a clone left over from an
        earlier run holds whatever snapshot it was last on. Reading that would resume from a
        stale cursor and silently drop processed_prs and publish_failed_prs recorded by a
        later run - exactly what the state branch exists to prevent.

        A hard reset is safe: the worktree only ever holds the state JSON, which is
        regenerated from the in-memory state on every save, and anything not yet pushed also
        lives in the local mirror.
        """
        if not self.git.remote_branch_exists(self.branch):
            return
        self.git.fetch(self.branch)
        self.git.git("reset", "--hard", f"origin/{self.branch}", cwd=self.worktree)

    # -- StateStore protocol -------------------------------------------------

    def _read_state_file(self, repo: str) -> StreamState | None:
        """Parse the state file in the worktree, or None if absent/unusable."""
        if not self.state_file.exists():
            return None

        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError) as e:
            self.logger.warning("Published state for %s is unreadable (%s)", repo, e)
            return None

        if data.get("repo") != repo:
            self.logger.warning("Published state is for %s, not %s", data.get("repo"), repo)
            return None

        return StreamState.from_dict(data)

    def _read_local_mirror(self, repo: str) -> StreamState | None:
        """Parse the local mirror, or None if absent/unusable."""
        if self.local_mirror is None or not Path(self.local_mirror).exists():
            return None
        try:
            data = json.loads(Path(self.local_mirror).read_text())
        except (OSError, ValueError) as e:
            self.logger.warning("Local state mirror is unreadable (%s); ignoring", e)
            return None
        if data.get("repo") != repo:
            return None
        return StreamState.from_dict(data)

    def load(self, repo: str) -> StreamState:
        """Resume from the published state, merged with any local mirror.

        The mirror is written before every push, so after a failed push it is ahead of the
        branch. But the branch can equally be ahead: another sandbox may have advanced it
        while this one's .swegen sat on a persistent volume. Reading only one side would
        forget the other's PRs and drop publish_failed_prs, which the fetcher needs to know
        a PR is still awaiting its PR.

        Sets are unioned whichever way the merge runs, so no PR is ever lost. The receiver
        only decides per-PR value collisions (task_pr_urls, successful_prs,
        other_failed_prs), so it must be whichever side was written more recently.
        """
        self._ensure_worktree()

        remote = self._read_state_file(repo)
        local = self._read_local_mirror(repo)

        if remote is None:
            if local is None:
                self.logger.info("No usable published state for %s; starting fresh", repo)
                return StreamState(repo=repo)
            self.logger.info("No published state for %s; resuming from local mirror", repo)
            return local

        if local is None:
            state = remote
        else:
            # merge_from gives the receiver precedence, so the fresher side receives. Ties
            # favour the mirror: it is written immediately before the push that produces the
            # branch commit, so equal timestamps mean equal content, and preferring it keeps
            # the failed-push recovery - the reason the mirror exists - working.
            local_is_fresher = (local.last_updated or "") >= (remote.last_updated or "")
            fresher, staler = (local, remote) if local_is_fresher else (remote, local)
            source = "local mirror" if local_is_fresher else "state branch"

            before = len(fresher.processed_prs)
            fresher.merge_from(staler)
            state = fresher
            recovered = len(state.processed_prs) - before
            self.logger.info(
                "Merged local mirror with the state branch (%s is newer): recovered %d PRs "
                "from the other side, %d pending publish failures",
                source,
                recovered,
                len(state.publish_failed_prs),
            )

        self.logger.info(
            "Resumed published state for %s: %d PRs processed, %d tasks published",
            repo,
            state.total_processed,
            state.successful,
        )
        return state

    def save(self, state: StreamState) -> None:
        """Commit and push state. Raises PublishError if it cannot be published.

        Not swallowed: if the state branch stops advancing, a reclaimed sandbox resumes
        from a stale cursor and repeats hours of Claude Code work on PRs already handled.
        That is exactly the failure this class exists to prevent, so the caller stops the
        run instead of farming blind.

        The local mirror is written first, so state survives on disk even when the push
        fails and can be recovered from the sandbox before it is reclaimed.
        """
        if self.local_mirror is not None:
            state.save(self.local_mirror)

        try:
            self._ensure_worktree()
            self._write_and_push(state)
        except PublishError:
            raise
        except Exception as e:
            raise PublishError(f"Could not publish farm state to {self.branch}: {e}") from e

    def _write_and_push(self, state: StreamState) -> None:
        rel_path = f"{self.cfg.state_path}/{slug(self.source_repo)}.json"
        refspec = f"{self.branch}:refs/heads/{self.branch}"

        self._commit_state(state, rel_path)
        try:
            self.git.push(refspec, cwd=self.worktree)
            return
        except Exception as e:
            # The merge recovery below only makes sense if the remote branch exists. When
            # it does not, the push failed for some other reason (network, auth, hook) and
            # `fetch origin <branch>` would fail too, masking the real error.
            if not self.git.remote_branch_exists(self.branch):
                raise
            self.logger.warning("State push rejected (%s); merging with remote and retrying", e)

        # The remote moved under us. Reset onto the remote tip, then MERGE the remote's
        # state into ours before recommitting. Blindly recommitting our in-memory snapshot
        # would discard whatever cursor the other writer just published, losing PRs it had
        # already processed. Merging is a union, so neither side forgets work.
        self.git.git("fetch", "origin", self.branch, cwd=self.worktree, network=True)
        self.git.git("reset", "--hard", f"origin/{self.branch}", cwd=self.worktree)

        remote_state = self._read_state_file(state.repo)
        if remote_state is not None:
            state.merge_from(remote_state)

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
