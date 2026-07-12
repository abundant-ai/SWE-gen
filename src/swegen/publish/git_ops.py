from __future__ import annotations

import base64
import logging
import os
import subprocess
from pathlib import Path

from .base import PublishError


class GitError(PublishError):
    """A git command failed."""


def slug(repo: str) -> str:
    """owner/repo -> owner__repo (SWEBench convention, matches farm_hand._slug)."""
    return repo.replace("/", "__")


class GitRepo:
    """Thin subprocess wrapper around a clone of the dataset repo.

    Only ever drives one clone. Task branches are built in the main working tree;
    the state branch lives in a linked worktree (see `add_worktree`) so that pushing
    state after every PR never disturbs a task branch mid-build.

    NOTE: serial use only. StreamFarmer processes one PR at a time, so a single
    working tree with `checkout -B` per task is safe. Concurrent task generation in
    one process would need a worktree per task.
    """

    def __init__(
        self,
        path: Path,
        *,
        token: str | None = None,
        author_name: str = "",
        author_email: str = "",
        dry_run: bool = False,
    ) -> None:
        # Absolute, always. `ensure_clone` runs `git clone <url> <path>` from the parent
        # directory, so a relative path (the default state dir is `.swegen`) would be
        # resolved against that cwd and the clone would land one level nested. Every other
        # method then chdirs into a path that does not exist.
        self.path = Path(path).resolve()
        self.token = token
        self.author_name = author_name
        self.author_email = author_email
        self.dry_run = dry_run
        self.logger = logging.getLogger("swegen")

    # -- command plumbing ----------------------------------------------------

    def _auth_args(self) -> list[str]:
        """Auth header passed per-invocation so the token never lands in .git/config."""
        if not self.token:
            return []
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]

    def _identity_args(self) -> list[str]:
        args = []
        if self.author_name:
            args += ["-c", f"user.name={self.author_name}"]
        if self.author_email:
            args += ["-c", f"user.email={self.author_email}"]
        return args

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        network: bool = False,
        remote_write: bool = False,
        identity: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a git command.

        Args:
            network: prepend the auth header (needed for fetch/clone/push/ls-remote)
            remote_write: skipped entirely under dry_run (push)
            identity: pass -c user.name/-c user.email (needed for commit)
            check: raise GitError on nonzero exit
        """
        if remote_write and self.dry_run:
            self.logger.info("[dry-run] would run: git %s", " ".join(args))
            return subprocess.CompletedProcess(args, 0, "", "")

        cmd = ["git"]
        if network:
            cmd += self._auth_args()
        if identity:
            cmd += self._identity_args()
        cmd += args

        proc = subprocess.run(
            cmd,
            cwd=str(cwd or self.path),
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc

    def git(self, *args: str, **kwargs) -> str:
        """Run a git command and return stripped stdout."""
        return self._run(list(args), **kwargs).stdout.strip()

    # -- clone / fetch -------------------------------------------------------

    def ensure_clone(self, url: str) -> None:
        """Clone the repo if absent, otherwise fetch. Idempotent across restarts."""
        if (self.path / ".git").exists():
            self.logger.debug("Using existing publish clone: %s", self.path)
            self._run(["fetch", "origin", "--prune"], network=True)
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logger.info("Cloning dataset repo -> %s", self.path)
        self._run(
            ["clone", url, str(self.path)],
            cwd=self.path.parent,
            network=True,
        )

    def fetch(self, ref: str) -> None:
        self._run(["fetch", "origin", ref], network=True)

    def remote_branch_exists(self, branch: str) -> bool:
        out = self.git("ls-remote", "--heads", "origin", branch, network=True)
        return bool(out)

    # -- branches / commits --------------------------------------------------

    def checkout_fresh_branch(self, branch: str, base_ref: str) -> None:
        """Cut `branch` from `base_ref`, discarding any prior state on it.

        Every task branch is cut fresh from origin/<base> so branches never stack on
        one another and each PR carries a single-task diff.
        """
        self._run(["checkout", "-B", branch, base_ref])

    def add(self, pathspec: str, *, cwd: Path | None = None) -> None:
        """Stage `pathspec`, overriding .gitignore.

        --force is deliberate: SWE-gen's own .gitignore excludes `tasks/` because that is
        the local output directory, and a dataset repo created from this template inherits
        it. We are publishing generated tasks on purpose, so an ignore rule in the dataset
        repo must not silently drop them. Only the one pathspec we name is ever staged.
        """
        self._run(["add", "--all", "--force", pathspec], cwd=cwd)

    def has_staged_changes(self, *, cwd: Path | None = None) -> bool:
        proc = self._run(["diff", "--cached", "--quiet"], cwd=cwd, check=False)
        return proc.returncode != 0

    def commit(self, message: str, *, cwd: Path | None = None) -> bool:
        """Commit staged changes. Returns False if there was nothing to commit."""
        if not self.has_staged_changes(cwd=cwd):
            return False
        self._run(["commit", "-m", message], cwd=cwd, identity=True)
        return True

    def push(
        self,
        refspec: str,
        *,
        cwd: Path | None = None,
        allow_force: bool = False,
        force: bool = False,
    ) -> None:
        """Push `refspec` to origin.

        Tries a normal push first, and only falls back to --force-with-lease when the
        caller passes allow_force - true exactly when the branch already exists on the
        remote and belongs to this one task, either as a leftover from an earlier attempt
        or as the branch backing its open PR, which we cut fresh and refresh. Either way
        we say so in the log.

        `force` is an unconditional `git push --force`, used only to honor an explicit
        --reset of the durable state branch: the intent is to discard whatever is on the
        remote, so --force-with-lease (which aborts when the remote moved) is wrong here.
        """
        if force:
            self._run(
                ["push", "--force", "origin", refspec],
                cwd=cwd,
                network=True,
                remote_write=True,
            )
            return

        proc = self._run(
            ["push", "origin", refspec],
            cwd=cwd,
            network=True,
            remote_write=True,
            check=False,
        )
        if proc.returncode == 0:
            return

        if not allow_force:
            raise GitError(f"git push origin {refspec} failed: {proc.stderr.strip()}")

        self.logger.warning(
            "Normal push of %s was rejected (%s). The branch belongs to this task alone - "
            "a leftover from an earlier attempt, or the branch backing its open PR - so "
            "retrying with --force-with-lease.",
            refspec,
            proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no stderr",
        )
        self._run(
            ["push", "--force-with-lease", "origin", refspec],
            cwd=cwd,
            network=True,
            remote_write=True,
        )

    # -- worktrees -----------------------------------------------------------

    def add_worktree(self, wt_path: Path, branch: str, base_ref: str) -> Path:
        """Check `branch` out into a linked worktree at `wt_path`.

        Creates the branch as an orphan (empty history) if it does not exist on the
        remote - the state branch shares no history with the task branches and must
        never be merged into them.
        """
        wt_path = Path(wt_path)
        if (wt_path / ".git").exists():
            return wt_path

        wt_path.parent.mkdir(parents=True, exist_ok=True)

        if self.remote_branch_exists(branch):
            self.fetch(branch)
            self._run(["worktree", "add", "-B", branch, str(wt_path), f"origin/{branch}"])
            return wt_path

        # Orphan branch. `git worktree add --orphan` needs git >= 2.42, so build it
        # the portable way: detach onto base, then orphan the checkout and empty it.
        self._run(["worktree", "add", "--detach", str(wt_path), base_ref])
        self._run(["checkout", "--orphan", branch], cwd=wt_path)
        self._run(["rm", "-rf", "--quiet", "."], cwd=wt_path, check=False)
        return wt_path
