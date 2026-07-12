from __future__ import annotations

from .base import PublishContext

PR_TITLE_TEMPLATE = "Add task: {task_id}"

PR_BODY_TEMPLATE = """Generated from `{source_repo}`#{source_pr}

{source_pr_url}
"""


def render_pr_title(ctx: PublishContext) -> str:
    return PR_TITLE_TEMPLATE.format(task_id=ctx.task_id)


def render_pr_body(ctx: PublishContext) -> str:
    """Render the PR description.

    Deliberately minimal. Extra detail (difficulty, tags, validation rewards) is
    available on ctx.metadata and can be folded into the template here without the
    sink needing to change.
    """
    return PR_BODY_TEMPLATE.format(
        source_repo=ctx.source_repo,
        source_pr=ctx.source_pr,
        source_pr_url=ctx.source_pr_url,
    )


def render_commit_message(ctx: PublishContext) -> str:
    return f"Add task: {ctx.task_id}"
