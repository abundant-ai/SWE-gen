# Harbor Task Checker - GitHub Action

Automatically check if PRs in your repository can become [Harbor](https://github.com/laude-institute/harbor) tasks for LLM training.

## Quick Start

Add `.github/workflows/harbor-check.yml` to your repository:

```yaml
name: Harbor Task Check

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  check:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: write
      pull-requests: read
    
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      
      - uses: abundant-ai/taskgen@v1
        id: harbor-check
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          # Optional: Enable full validation
          # claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}  # Preferred
          # anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}              # Or use API key
          # openai_api_key: ${{ secrets.OPENAI_API_KEY }}
      
      - uses: actions/upload-artifact@v4
        if: steps.harbor-check.outputs.eligible == 'true'
        with:
          name: ${{ steps.harbor-check.outputs.artifact_name }}
          path: /tmp/harbor-tasks/${{ steps.harbor-check.outputs.task_id }}
```

That's it! PRs will now show eligibility status in the Job Summary.

## What Makes a PR Eligible?

| Requirement | Why |
|-------------|-----|
| 3-10 source files modified | Multi-component fixes make better training data |
| Includes test changes | Tests validate the fix works |
| Substantial changes | Not just docs, formatting, or version bumps |

Most PRs (~90%) won't be eligible—and that's fine! The action explains why.

## Configuration

| Input | Default | Description |
|-------|---------|-------------|
| `claude_code_oauth_token` | - | OAuth token for Claude Code (preferred) |
| `anthropic_api_key` | - | API key for Claude Code (fallback) |
| `openai_api_key` | - | Enables LLM substantiality check |
| `skip_validation` | `false` | Skip Docker validation (faster) |
| `min_source_files` | `3` | Minimum source files required |
| `max_source_files` | `10` | Maximum source files allowed |

## Outputs

| Output | Description |
|--------|-------------|
| `eligible` | `true` or `false` |
| `reason` | Why the PR is/isn't eligible |
| `task_id` | Task ID like `owner__repo-123` |

## How Submission Works

1. PR passes validation → Job Summary shows "Submit to Harbor" button
2. Developer clicks button → Triggers workflow in taskgen repo
3. PR is created with the task → Maintainer reviews and merges

No write access to your repository is needed.

## Local Testing

```bash
pip install git+https://github.com/abundant-ai/taskgen.git
taskgen create --repo owner/repo --pr 123
```
