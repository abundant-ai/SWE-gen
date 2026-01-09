from __future__ import annotations

import logging
import shutil
from pathlib import Path

from harbor.models.task.paths import TaskPaths

from .claude_code_runner import ClaudeCodeResult, run_claude_code_session
from .diff_utils import extract_test_files, generate_diffs
from .pr_fetcher import GitHubPRFetcher
from .repo_cache import RepoCache
from .task_instruction import evaluate_and_generate_task
from .task_reference import TaskReference, TaskReferenceStore
from .task_skeleton import (
    SkeletonParams,
    generate_instruction_md,
    generate_task_toml,
    generate_dockerfile,
    generate_solve_sh,
    generate_test_sh,
)
from .utils import check_multi_file_requirement, identify_test_files


class TrivialPRError(Exception):
    """Raised when a PR is too trivial to generate a task from."""

    pass


class MissingIssueError(Exception):
    """Raised when a PR has no linked issue and require_issue is enabled."""

    pass


class PRToHarborPipeline:
    """Orchestrates the conversion of a GitHub PR into a Harbor-compatible task."""

    def __init__(self, repo: str, pr_number: int, github_token: str | None = None):
        """
        Initialize the pipeline.

        Args:
            repo: GitHub repo in format "owner/repo" or full URL
            pr_number: PR number
            github_token: Optional GitHub token for API access
        """
        self.pr_fetcher = GitHubPRFetcher(repo, pr_number, github_token)
        self.repo = self.pr_fetcher.repo
        self.pr_number = pr_number
        # Lowercase repo name for task_id (used in Docker image names which must be lowercase)
        # Format: owner__repo-number (SWEBench convention)
        repo_slug = self.repo.lower().replace("/", "__")
        self.task_id = f"{repo_slug}-{pr_number}"

    def create_task_scaffold(self, tasks_root: Path, overwrite: bool = False) -> Path:
        """
        Create task directory structure.

        Returns the task directory path.
        """
        logger = logging.getLogger("taskgen")
        logger.debug("Creating task scaffold...")

        task_dir = tasks_root / self.task_id

        # Check if task already exists
        if task_dir.exists():
            logger.debug(f"Task directory already exists: {task_dir}")
            if overwrite:
                logger.debug("Removing existing directory (forced)...")
                shutil.rmtree(task_dir)
            else:
                raise FileExistsError(f"Task already exists: {task_dir}\nUse --force to overwrite.")

        # Create the task directory
        logger.debug(f"Creating directory: {task_dir}")
        task_dir.mkdir(parents=True, exist_ok=True)

        return task_dir

    def generate_task(
        self,
        tasks_root: Path,
        overwrite: bool = False,
        cache_dir: Path | None = None,
        repo_path: Path | None = None,
        metadata: dict | None = None,
        linked_issues: list | None = None,
        run_cc: bool = True,
        cc_timeout: int = 3200,
        verbose: bool = True,
        use_cache: bool = True,
        state_dir: Path | None = None,
        require_minimum_difficulty: bool = True,
        min_source_files: int = 3,
        max_source_files: int = 10,
        environment: str = "docker",
    ) -> tuple[Path, ClaudeCodeResult | None, list[str], TaskReference | None]:
        """
        Generate a Harbor task using skeleton + Claude Code.

        This is the language-agnostic pipeline that works for any repository.
        Claude Code analyzes the repo to detect language, runtime, build system,
        and test framework, then fills in the skeleton accordingly.

        Flow:
        1. Clone/update repo to local cache
        2. Generate skeleton (language-agnostic Dockerfile, test.sh)
        3. Run Claude Code to detect language and fill in skeleton
        4. Validate with Harbor NOP/Oracle agents

        Args:
            tasks_root: Output root directory (Harbor tasks go here)
            overwrite: If True, remove existing task dir
            cache_dir: Directory for repo cache (default: .cache/repos)
            repo_path: Pre-cloned repo path (skips cloning if provided)
            metadata: Pre-fetched PR metadata (skips API call if provided)
            linked_issues: Pre-fetched linked issues (skips API call if provided)
            run_cc: If True, run CC to complete skeleton (default: True)
            cc_timeout: Timeout for CC session in seconds
            verbose: If True, stream CC output
            use_cache: If True, try to reuse cached artifacts from previous successful PRs
            state_dir: State directory for task references (default: .state)
            require_minimum_difficulty: If True, require 3+ source files modified
            min_source_files: Minimum number of source files required (default: 3)
            max_source_files: Maximum number of source files allowed to avoid large refactors (default: 10)

        Returns:
            Tuple of (task_dir, cc_result, extracted_test_files, task_reference)
            cc_result is None if run_cc=False
            task_reference is None if no cached reference exists or use_cache=False
        """
        logger = logging.getLogger("taskgen")
        logger.info("=" * 60)
        logger.info("Task Generation")
        logger.info("Repo: %s, PR: #%d", self.repo, self.pr_number)
        logger.info("=" * 60)

        # Initialize reference store with proper state directory
        reference_store = None
        if use_cache:
            reference_file = (state_dir / "task_references.json") if state_dir else None
            reference_store = TaskReferenceStore(reference_file=reference_file)

        # Step 1: Fetch PR metadata (use provided or fetch)
        if metadata is None:
            metadata = self.pr_fetcher.fetch_pr_metadata(allow_unmerged=self.config.allow_unmerged)

        # Fetch linked issues for better task descriptions (use provided or fetch)
        if linked_issues is None:
            linked_issues = []
            try:
                linked_issues = self.pr_fetcher.fetch_linked_issues()
                if linked_issues:
                    logger.info("Found %d linked issue(s)", len(linked_issues))
            except Exception as e:
                logger.debug("Could not fetch linked issues: %s", str(e))

        files = self.pr_fetcher.fetch_pr_files()

        # Step 2: Multi-file requirement check (fail fast before expensive operations)
        # Use generic language detection - CC will figure out the actual language
        if require_minimum_difficulty:
            passes, reason, source_count = check_multi_file_requirement(
                files, min_files=min_source_files, max_files=max_source_files
            )
            if not passes:
                logger.warning("Skipping PR - source file count out of range: %s", reason)
                raise TrivialPRError(f"PR #{self.pr_number}: {reason}")
            logger.info(
                "Multi-file check passed: %d source files (excluding tests, range: %d-%d)",
                source_count,
                min_source_files,
                max_source_files,
            )
        else:
            logger.info("Skipping minimum difficulty check (require_minimum_difficulty=False)")

        # Step 3: Identify test files (language-agnostic patterns)
        test_file_paths = identify_test_files(files)
        logger.info("Identified %d test files", len(test_file_paths))

        # Step 4: Clone/update repo to local cache (use provided or clone)
        if repo_path is None:
            repo_cache = RepoCache(cache_dir)
            repo_path = repo_cache.get_or_clone(
                repo=self.repo,
                head_sha=metadata["head_sha"],
                repo_url=metadata["repo_url"],
            )
        logger.info("Repo at: %s", repo_path)

        # Step 5: Create task scaffold
        task_dir = self.create_task_scaffold(tasks_root, overwrite=overwrite)
        paths = TaskPaths(task_dir)
        paths.environment_dir.mkdir(exist_ok=True)
        paths.solution_dir.mkdir(exist_ok=True)
        paths.tests_dir.mkdir(exist_ok=True)

        try:
            # Step 6: Try to get reference to previous successful task
            task_reference = None
            if reference_store:
                task_reference = reference_store.get(
                    repo=self.repo,
                    max_age_days=180,
                )
                if task_reference:
                    logger.info(
                        f"Found task reference: {task_reference.task_id} "
                        f"(from PR #{task_reference.pr_number}, created {task_reference.created_at[:10]})"
                    )

            # Step 7: Generate diffs from local repo (language-agnostic)
            solution_diff, bug_diff = generate_diffs(
                repo_path=repo_path,
                base_sha=metadata["base_sha"],
                head_sha=metadata["head_sha"],
                test_file_paths=test_file_paths,
            )

            # Step 8: Extract test files
            extracted_test_files = extract_test_files(
                repo_path=repo_path,
                test_file_paths=test_file_paths,
                head_sha=metadata["head_sha"],
                output_dir=task_dir,
            )

            # Step 8b: Read test file contents for instruction generation
            test_contents = {}
            test_dir = task_dir / "tests"
            if test_dir.exists():
                for test_file in test_dir.rglob("*"):
                    if test_file.is_file():
                        try:
                            # Read as text, skip binary files
                            content = test_file.read_text(encoding='utf-8', errors='ignore')
                            # Store with relative path from tests/ dir
                            rel_path = test_file.relative_to(test_dir)
                            test_contents[str(rel_path)] = content
                        except Exception as e:
                            logger.debug(f"Could not read test file {test_file}: {e}")

            # Step 9: Generate evaluation + instruction (uses LLM but not CC)
            logger.info("Evaluating PR and generating instruction...")
            try:
                combined_result = evaluate_and_generate_task(
                    metadata,
                    files,
                    self.repo,
                    linked_issues=linked_issues,
                    force_generate_instruction=(not require_minimum_difficulty),
                    test_contents=test_contents,
                )

                if not combined_result.is_substantial:
                    if require_minimum_difficulty:
                        logger.warning("Skipping trivial PR: %s", combined_result.reason)
                        shutil.rmtree(task_dir)
                        raise TrivialPRError(
                            f"PR #{self.pr_number} is too trivial: {combined_result.reason}"
                        )
                    else:
                        logger.warning(
                            "PR deemed trivial by LLM, but proceeding anyway: %s",
                            combined_result.reason,
                        )

                instruction_data = {
                    "instruction": combined_result.instruction,
                    "difficulty": combined_result.difficulty,
                    "category": combined_result.category,
                    "tags": combined_result.tags,
                }
            except TrivialPRError:
                raise
            except Exception:
                if task_dir.exists():
                    shutil.rmtree(task_dir)
                raise

            # Step 10: Write skeleton files
            logger.info("Writing skeleton task files...")

            # Create skeleton params
            skeleton_params = SkeletonParams(
                repo_url=metadata["repo_url"],
                head_sha=metadata["head_sha"],
                base_sha=metadata["base_sha"],
            )

            # bug.patch
            (paths.environment_dir / "bug.patch").write_text(bug_diff)

            # Dockerfile (with TODOs for CC)
            dockerfile = generate_dockerfile(skeleton_params)
            (paths.environment_dir / "Dockerfile").write_text(dockerfile)

            # test.sh (with TODOs for CC)
            test_sh_content = generate_test_sh(extracted_test_files)
            paths.test_path.write_text(test_sh_content)
            paths.test_path.chmod(0o755)

            # instruction.md and task.toml
            paths.instruction_path.write_text(generate_instruction_md(instruction_data))
            paths.config_path.write_text(generate_task_toml(instruction_data))

            # solution/fix.patch - the actual fix to apply
            (paths.solution_dir / "fix.patch").write_text(solution_diff)

            # solution/solve.sh - applies fix.patch (same for all languages)
            paths.solve_path.write_text(generate_solve_sh())
            paths.solve_path.chmod(0o755)

            logger.info("Skeleton generated: %s", task_dir)

            # Step 11: Run CC to complete skeleton and make harbor pass
            cc_result = None
            if run_cc:
                if task_reference:
                    logger.info(
                        f"Running CC with reference task {task_reference.task_id} "
                        f"from PR #{task_reference.pr_number} (should be much faster)..."
                    )
                else:
                    logger.info(
                        "Running CC session (will detect language automatically)..."
                    )

                cc_result = run_claude_code_session(
                    repo=self.repo,
                    pr_number=self.pr_number,
                    repo_path=repo_path,
                    task_dir=task_dir,
                    task_id=self.task_id,
                    dataset_path=tasks_root,
                    test_files=extracted_test_files,
                    timeout=cc_timeout,
                    verbose=verbose,
                    reference_task_id=task_reference.task_id if task_reference else None,
                    reference_pr=task_reference.pr_number if task_reference else None,
                    head_sha=metadata.get("head_sha"),
                    environment=environment,
                )

                if cc_result.success:
                    logger.info("✓ CC completed task successfully!")
                    # Save reference to this successful task for future PRs
                    if reference_store and not task_reference:
                        reference_store.save(
                            repo=self.repo,
                            task_id=self.task_id,
                            pr_number=self.pr_number,
                        )
                else:
                    logger.warning("✗ CC did not complete task: %s", cc_result.error_message)

            return task_dir, cc_result, extracted_test_files, task_reference

        except Exception:
            if task_dir.exists():
                shutil.rmtree(task_dir)
            raise
