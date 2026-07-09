from __future__ import annotations

import logging
import shutil
from pathlib import Path

from swegen.config import PublishConfig

from .base import PublishAuthError, PublishContext, PublishError, PublishResult
from .body import render_commit_message, render_pr_body, render_pr_title
from .gh_api import GitHubAPI
from .git_ops import GitRepo, slug


def default_clone_dir(cfg: PublishConfig, source_repo: str, state_dir: Path) -> Path:
    """Where to clone the dataset repo.

    Keyed by *both* dataset and source repo: the dataset repo is the same across all
    containers, so keying only on it would make two farms colocated on one host fight
    over a single working tree.
    """
    if cfg.clone_dir is not None:
        return Path(cfg.clone_dir)
    return state_dir / "publish" / slug(cfg.repo) / slug(source_repo)


class GitHubPRSink:
    """Publishes each validated task as its own branch + pull request.

    Shares one clone with GitStateStore; the state branch lives in a linked worktree,
    so state pushes never disturb the task branch checked out in the main tree.
    """

    def __init__(
        self,
        cfg: PublishConfig,
        source_repo: str,
        *,
        state_dir: Path = Path(".swegen"),
        git: GitRepo | None = None,
    ) -> None:
        self.cfg = cfg
        self.source_repo = source_repo
        self.clone_dir = default_clone_dir(cfg, source_repo, state_dir)
        self.logger = logging.getLogger("swegen")
        self.api = GitHubAPI(cfg.token)
        self.git = git or GitRepo(
            self.clone_dir,
            token=cfg.token,
            author_name=cfg.author_name,
            author_email=cfg.author_email,
            dry_run=cfg.dry_run,
        )
        self._preflight_done = False

    # -- lifecycle -----------------------------------------------------------

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.cfg.repo}.git"

    def branch_for(self, task_id: str) -> str:
        return f"{self.cfg.branch_prefix}{task_id}"

    def preflight(self) -> None:
        """Verify write access and prepare the clone, before any task is generated.

        Called once at farm startup so a bad token fails in seconds rather than after
        the first hour-long Claude Code session.
        """
        if self._preflight_done:
            return

        repo_data = self.api.get_repo(self.cfg.repo)
        permissions = repo_data.get("permissions") or {}
        if not permissions.get("push"):
            raise PublishAuthError(
                f"Token lacks write access to {self.cfg.repo}. Needs a fine-grained PAT "
                f"with contents:write and pull_requests:write (or classic 'repo' scope)."
            )

        self.git.ensure_clone(self.clone_url)
        self.git.fetch(self.cfg.base_branch)
        self._preflight_done = True

    # -- publish -------------------------------------------------------------

    def publish(self, ctx: PublishContext) -> PublishResult:
        self.preflight()

        branch = self.branch_for(ctx.task_id)

        # Idempotency: a restarted sandbox may regenerate a task we already published.
        existing = None if self.cfg.dry_run else self.api.find_open_pr(self.cfg.repo, branch)
        if existing:
            self.logger.info(
                "Task %s already has an open PR: %s", ctx.task_id, existing["html_url"]
            )
            return PublishResult(published=True, pr_url=existing["html_url"], branch=branch)

        stale_branch = self.git.remote_branch_exists(branch)

        self.git.fetch(self.cfg.base_branch)
        self.git.checkout_fresh_branch(branch, f"origin/{self.cfg.base_branch}")

        dest = self.clone_dir / self.cfg.tasks_path / ctx.task_id
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ctx.task_dir, dest)

        rel_path = f"{self.cfg.tasks_path}/{ctx.task_id}"
        self.git.add(rel_path)
        if not self.git.commit(render_commit_message(ctx)):
            raise PublishError(
                f"Nothing to commit for {ctx.task_id}; task directory {ctx.task_dir} may be empty."
            )

        # A stale branch is one this same task left behind on an earlier attempt, so
        # overwriting it is safe. A fresh branch must never need force.
        self.git.push(f"{branch}:refs/heads/{branch}", allow_force=stale_branch)

        if self.cfg.dry_run:
            self.logger.info(
                "[dry-run] would open PR %r on %s (%s -> %s)",
                render_pr_title(ctx),
                self.cfg.repo,
                branch,
                self.cfg.base_branch,
            )
            return PublishResult(published=False, pr_url=None, branch=branch)

        pr = self.api.create_pr(
            repo=self.cfg.repo,
            title=render_pr_title(ctx),
            body=render_pr_body(ctx),
            head=branch,
            base=self.cfg.base_branch,
        )
        return PublishResult(published=True, pr_url=pr["html_url"], branch=branch)
