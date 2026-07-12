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
        # Resolved: git commands run with cwd set to this directory, and the default
        # state dir is relative.
        self.clone_dir = default_clone_dir(cfg, source_repo, state_dir).resolve()
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
        """Verify the dataset repo is reachable and writable, before any task is generated.

        Called once at farm startup so a bad token fails in seconds rather than after the
        first hour-long Claude Code session.

        Only an EXPLICIT `push: false` is treated as a rejection. Some tokens - notably
        fine-grained PATs and app installation tokens - omit or under-report `permissions`
        on GET /repos, and refusing those would block a token whose pushes and PRs would
        have succeeded. When the field is absent we warn and let the first push decide:
        a publish failure aborts the run and preserves the task, so the cost of being
        wrong is one Claude Code session, not a lost task.
        """
        if self._preflight_done:
            return

        repo_data = self.api.get_repo(self.cfg.repo)
        permissions = repo_data.get("permissions")

        if isinstance(permissions, dict) and "push" in permissions:
            if not permissions["push"]:
                raise PublishAuthError(
                    f"Token lacks write access to {self.cfg.repo}. Needs a fine-grained PAT "
                    f"with contents:write and pull_requests:write (or classic 'repo' scope)."
                )
        else:
            self.logger.warning(
                "GitHub did not report push permission for %s (common for fine-grained and "
                "app tokens). Proceeding; a write failure will surface on the first publish.",
                self.cfg.repo,
            )

        self.git.ensure_clone(self.clone_url)
        self.git.fetch(self.cfg.base_branch)
        self._preflight_done = True

    # -- publish -------------------------------------------------------------

    def publish(self, ctx: PublishContext) -> PublishResult:
        """Publish `ctx` to its own branch, opening a PR if one is not already open.

        An existing open PR short-circuits only PR *creation*, never the branch update.
        The caller may have regenerated the task (--force, --reset, a rerun after a failed
        create.jsonl write), and returning early would leave the branch and PR on older
        content while the pipeline reported success.
        """
        self.preflight()

        branch = self.branch_for(ctx.task_id)

        # Idempotency: a restarted sandbox may regenerate a task we already published.
        existing = None if self.cfg.dry_run else self.api.find_open_pr(self.cfg.repo, branch)
        if existing:
            self.logger.info(
                "Task %s already has an open PR (%s); refreshing its branch",
                ctx.task_id,
                existing["html_url"],
            )

        stale_branch = self.git.remote_branch_exists(branch)

        self.git.fetch(self.cfg.base_branch)
        self.git.checkout_fresh_branch(branch, f"origin/{self.cfg.base_branch}")

        dest = self.clone_dir / self.cfg.tasks_path / ctx.task_id
        # Filesystem errors here (disk full, permissions) must surface as PublishError, not
        # a bare OSError. A bare OSError reads to the farm as a generic pipeline failure,
        # which cleans up (deletes) the validated task - but this is a publish failure, and
        # the task must be kept on disk for a re-run. git ops already raise GitError
        # (a PublishError); only these copies were unguarded.
        try:
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(ctx.task_dir, dest)
        except OSError as e:
            raise PublishError(
                f"Could not stage task {ctx.task_id} into the clone ({dest}): {e}"
            ) from e

        rel_path = f"{self.cfg.tasks_path}/{ctx.task_id}"
        self.git.add(rel_path)
        if not self.git.commit(render_commit_message(ctx)):
            # The branch is cut from the base, so an empty diff means the task is already
            # merged there byte-for-byte. There is no new commit to push.
            if not any(dest.iterdir()):
                raise PublishError(f"Task directory {ctx.task_dir} is empty; nothing to publish.")
            # A stale remote branch (and any open PR on it) can still point at an older
            # commit. Our branch now equals base, so force it up: the branch reflects that
            # the task is in base, rather than leaving the PR showing outdated content while
            # we report published=True.
            if stale_branch:
                self.git.push(f"{branch}:refs/heads/{branch}", allow_force=True)
            self.logger.info(
                "Task %s is already present in %s; branch synced, nothing new to publish",
                ctx.task_id,
                self.cfg.base_branch,
            )
            pr_url = existing["html_url"] if existing else None
            return PublishResult(published=True, pr_url=pr_url, branch=branch)

        # A stale branch belongs to this same task from an earlier attempt (or backs the
        # open PR we are refreshing), so overwriting it is safe. A fresh branch never
        # needs force.
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

        if existing:
            # Branch refreshed above; the open PR now carries the regenerated task.
            return PublishResult(published=True, pr_url=existing["html_url"], branch=branch)

        pr = self.api.create_pr(
            repo=self.cfg.repo,
            title=render_pr_title(ctx),
            body=render_pr_body(ctx),
            head=branch,
            base=self.cfg.base_branch,
        )
        return PublishResult(published=True, pr_url=pr["html_url"], branch=branch)
