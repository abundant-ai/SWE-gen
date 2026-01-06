from __future__ import annotations

import logging
import os

from openai import OpenAI

from .utils import CombinedPRTaskEvaluation

MAX_LINKED_ISSUES = 5
MAX_ISSUE_BODY_LENGTH = 2500
MAX_PR_BODY_LENGTH = 2500
MIN_INSTRUCTION_LENGTH = 100
OPENAI_API_TIMEOUT = 90.0
MAX_COMPLETION_TOKENS = 4096
DEBUG_REASON_TRUNCATE_LENGTH = 100

COMBINED_SYSTEM_PROMPT = """You are evaluating GitHub pull requests and converting substantial ones into Harbor tasks.

Your job has TWO PHASES:

PHASE 1 - Evaluate Substantiality:
Determine if the PR is substantial enough to generate a coding task.

SKIP (is_substantial=false) if the PR is:
- Pure documentation updates including:
  * README, docs/, markdown files
  * docs_src/, doc_src/, examples/ (documentation example code)
  * tests/test_tutorial/, tests/test_docs/, test_examples/ (tests for documentation)
- Only dependency/package updates (requirements.txt, package.json, etc.)
- Simple typo or formatting fixes with no functional changes
- CI/config changes only (.github/workflows, .travis.yml, etc.)
- Version bumps or release commits
- Other trivial maintenance tasks
- Changes to only a single file (not substantial enough)
- Simple one-line fixes or trivial changes (even across multiple files)
- Purely cosmetic refactoring (renaming variables, reformatting, etc.)
- Adding simple logging or print statements without logic changes

KEEP (is_substantial=true) if the PR:
- Fixes a non-trivial bug with changes across MULTIPLE source files
- Adds or modifies functional tests AND implements corresponding source code changes
- Implements a feature or enhancement with changes to MULTIPLE source files
- Has meaningful behavioral changes affecting multiple components or modules
- Requires coordination between different parts of the codebase

CRITICAL REQUIREMENT for is_substantial=true:
The PR MUST modify multiple files (at least 2-3 meaningful source code files, not counting trivial changes).
Single-file changes are almost never substantial enough unless they involve major refactoring or complex logic.

PHASE 2 - Generate Task (ONLY if substantial):
If is_substantial=true, write a CONCISE bug report.

SOURCE PRIORITY (CRITICAL - follow this order strictly):
1. If linked issues exist, extract the bug description DIRECTLY from the issue content
2. Otherwise, infer the bug from PR title/description

CRITICAL INSTRUCTIONS:
- Write about the ACTUAL bug from the PR/issue you're evaluating
- If a linked issue exists, base your description on that issue's actual content

FORMAT RULES:
- Be concise - state what's broken, how to trigger it, expected behavior
- Include a brief code snippet if it helps show the bug
- DO NOT use sections like "Impact:", "Acceptance criteria:", "Notes:", "Additional considerations:"
- DO NOT write long bullet-point lists
- DO NOT pad with verbose explanations

CONTENT RULES:
- DO NOT mention file paths or function names (unless from the issue)
- DO NOT leak implementation details or where to fix
- Focus on the USER-VISIBLE problem, not internals
- Extract the problem description FROM THE PROVIDED PR/ISSUE DATA

TAGS:
Generate exactly 3 tags in this order:
1. Primary programming language (e.g., "python", "javascript", "typescript", "go", "rust", "java", "ruby", "cpp")
2. Tier/area: Choose ONE from: "backend", "frontend", "fullstack", "cli", "library", "framework"
3. Framework/library name (e.g., "fastapi", "django", "react", "nextjs", "axios", "express") OR a specific category (e.g., "http", "async", "testing")

Examples:
- FastAPI backend project: ["python", "backend", "fastapi"]
- Next.js frontend: ["typescript", "frontend", "nextjs"]
- Ripgrep CLI tool: ["rust", "cli", "regex"]

IMPORTANT: Generate exactly 3 tags.

If NOT substantial, set instruction to null and provide a brief reason.
"""


def _format_user_prompt(
    pr_title: str,
    pr_body: str,
    repo: str,
    changed_files: list[str],
    linked_issues: list[dict] | None = None,
    force_generate_instruction: bool = False,
) -> str:
    """Format user prompt for combined evaluation + task generation.

    Prioritizes linked issues and avoids leaking solution details (files, diff, commits).
    """
    # Calculate basic stats for evaluation (no file names - just counts)
    total = len(changed_files or [])
    tests = sum(1 for p in (changed_files or []) if "test" in (p or "").lower())
    docs = sum(
        1
        for p in (changed_files or [])
        if any(seg in (p or "").lower() for seg in ("docs/", "doc/"))
    )
    source_files = total - tests - docs

    # Modify ending instruction based on force_generate_instruction flag
    if force_generate_instruction:
        ending_instruction = (
            "\nIMPORTANT: Generate a detailed instruction for this PR regardless of complexity.\n"
            "You should ALWAYS set is_substantial=true and write a comprehensive bug report/task instruction.\n"
            "Even if the PR seems simple, treat it as a valid task and describe the problem that was fixed.\n"
            "Focus on writing a clear, detailed bug report with specifics about the issue that was resolved.\n"
            "DO NOT mention specific file paths or function names unless they appear in the issue."
        )
    else:
        ending_instruction = (
            "\nFirst, evaluate if this PR is substantial enough to generate a task.\n"
            "Remember: PRs with changes to only 1-2 files are usually too trivial unless they involve major complexity.\n"
            "Look for changes across multiple source files that demonstrate real cross-component coordination.\n"
            "If substantial, write a detailed bug report describing the PROBLEM (not the solution).\n"
            "DO NOT mention specific file paths or function names unless they appear in the issue.\n"
            "If not substantial, explain why briefly and set instruction to null."
        )

    # MODE 1: Linked issues exist - use ONLY issue content (preferred)
    if linked_issues and len(linked_issues) > 0:
        # Sort by body length (longer = more detail = more useful), take top N
        sorted_issues = sorted(
            linked_issues, key=lambda x: len(x.get("body", "") or ""), reverse=True
        )[:MAX_LINKED_ISSUES]

        issue_lines = []
        for issue in sorted_issues:
            issue_num = issue.get("number", "")
            issue_title = issue.get("title", "")
            issue_body = (issue.get("body", "") or "").strip()
            # Truncate issue body if too long
            if len(issue_body) > MAX_ISSUE_BODY_LENGTH:
                issue_body = issue_body[:MAX_ISSUE_BODY_LENGTH] + "\n...(truncated)"

            issue_lines.append(f"Issue #{issue_num}: {issue_title}")
            if issue_body:
                issue_lines.append(f"{issue_body}\n")

        issues_section = "\n".join(issue_lines)

        return (
            f"Repository: {repo}\n"
            f"PR Title: {pr_title}\n\n"
            f"Linked Issue(s) - USE THESE AS THE PRIMARY SOURCE:\n{issues_section}\n\n"
            f"Scope (for evaluation only): {source_files} source files, {tests} test files changed\n"
            + ending_instruction
        )

    # MODE 2: No linked issue - use PR title + body, but warn LLM about solution leakage
    pr_body_truncated = (pr_body or "").strip()
    if len(pr_body_truncated) > MAX_PR_BODY_LENGTH:
        pr_body_truncated = pr_body_truncated[:MAX_PR_BODY_LENGTH] + "\n...(truncated)"

    return (
        f"Repository: {repo}\n"
        f"PR Title: {pr_title}\n\n"
        + (f"PR Description:\n{pr_body_truncated}\n\n" if pr_body_truncated else "")
        + f"Scope (for evaluation only): {source_files} source files, {tests} test files changed\n\n"
        "WARNING: No linked issue found. The PR description may contain solution details.\n"
        "Extract ONLY the problem description. Ignore any mentions of:\n"
        "- What was changed/fixed/updated\n"
        "- Which files or functions were modified\n"
        "- Implementation approach or code changes\n"
        "Describe the PROBLEM that users would experience, not how it was fixed.\n"
        + ending_instruction
    )


def evaluate_and_generate_task(
    metadata: dict,
    files: list[dict],
    repo: str,
    model: str = "gpt-5-mini",
    api_key: str | None = None,
    linked_issues: list[dict] | None = None,
    force_generate_instruction: bool = False,
) -> CombinedPRTaskEvaluation:
    """Evaluate PR substantiality and generate task description in one LLM call.

    Uses OpenAI's structured outputs with the parse() method for type-safe responses.

    Args:
        metadata: PR metadata dict
        files: List of changed files
        repo: Repository name
        model: OpenAI model to use
        api_key: Optional OpenAI API key
        linked_issues: Optional list of linked issue dicts (with 'title', 'body', 'number')
        force_generate_instruction: If True, always generate an instruction even if PR seems trivial

    Returns:
        CombinedPRTaskEvaluation with evaluation and task details

    Raises:
        RuntimeError: If API key is missing or LLM call fails
    """
    logger = logging.getLogger("taskgen")

    # Check API key
    if not (api_key or os.getenv("OPENAI_API_KEY")):
        raise RuntimeError("OPENAI_API_KEY not set")

    # Prepare prompt data
    # NOTE: We intentionally do NOT pass diff/commits to avoid leaking the solution
    pr_title = metadata.get("title", "")
    pr_body = metadata.get("body", "")
    changed_files = [f.get("filename", "") for f in files]

    user_prompt = _format_user_prompt(
        pr_title,
        pr_body,
        repo,
        changed_files,
        linked_issues=linked_issues,
        force_generate_instruction=force_generate_instruction,
    )

    client = OpenAI(
        api_key=api_key or os.getenv("OPENAI_API_KEY"),
        timeout=OPENAI_API_TIMEOUT,  # Longer timeout for reasoning models
    )

    try:
        # Use structured outputs with parse() method - type-safe!
        completion = client.beta.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": COMBINED_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format=CombinedPRTaskEvaluation,
            max_completion_tokens=MAX_COMPLETION_TOKENS,
            # reasoning_effort="low", # TODO: reasoning level?
        )

        result = completion.choices[0].message.parsed
        if result is None:
            raise RuntimeError("LLM returned no parsed result")

        logger.debug(
            f"Combined evaluation: is_substantial={result.is_substantial}, reason={result.reason[:DEBUG_REASON_TRUNCATE_LENGTH]}..."
        )

        # Post-process: validate tags if substantial
        if result.is_substantial:
            if len(result.tags) < 1:
                logger.error(f"❌ LLM generated only {len(result.tags)} tags")
                raise RuntimeError(f"LLM generated only {len(result.tags)} tags")

            # Validate instruction length
            if not result.instruction or len(result.instruction.strip()) < MIN_INSTRUCTION_LENGTH:
                logger.error(
                    f"❌ LLM generated instruction too short: {len(result.instruction) if result.instruction else 0} chars"
                )
                raise RuntimeError(
                    f"Instruction too short: {len(result.instruction) if result.instruction else 0} chars (need {MIN_INSTRUCTION_LENGTH}+)"
                )

            # Ensure defaults
            if not result.difficulty:
                result.difficulty = "medium"
            if not result.category:
                result.category = "bugfix"

        return result

    except Exception as exc:
        # Log the specific exception type for better debugging
        exc_type = type(exc).__name__
        logger.error(f"Combined LLM call failed ({exc_type}): {exc}")
        raise RuntimeError(f"Combined LLM call failed: {exc}") from exc
