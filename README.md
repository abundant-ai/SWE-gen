# TaskGen CLI

> Convert merged GitHub pull requests into [Harbor](https://github.com/laude-institute/harbor) tasks automatically.

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)

## Overview

Automates creation of Harbor tasks from real-world bug fixes in open-source repositories. The pipeline works with **any programming language**. Claude Code analyzes the repo to detect language, runtime, build system, and test framework.

Each task reverses a merged PR to recreate the buggy state, validates tests fail on baseline, and pass after applying the fix. Fully containerized with dependencies installed at build time.

## Quick Start

```bash
# Install
uv pip install -e .

# Generate a task from a merged PR
taskgen reversal --repo axios/axios --pr 7150

# Or farm all PRs from a repo
taskgen farm fastapi/fastapi
```

## Installation

```bash
uv pip install -e .
```

**Requirements:**
- Python 3.13+
- Docker
- uv
- [Claude Code CLI](https://github.com/anthropics/claude-code)

**Secrets:** Create a `.env` file:

```bash
export GITHUB_TOKEN=<gh-token>
export OPENAI_API_KEY=<api-key>
export ANTHROPIC_API_KEY=<api-key>  # or use Claude Code OAuth
```

**Note:** Cloud sandbox environments (Daytona, E2B, Modal, etc.) require additional API keys. 

## Usage

**Commands:**
- `taskgen reversal` — Generate task from a merged PR (validates by default)
- `taskgen farm` — Continuously process PRs from a repository
- `taskgen validate` — Validate existing Harbor task (NOP + Oracle)
- `taskgen analyze` — Deep analysis with agent trials to verify task quality
- `taskgen clean` — Remove .state artifacts

### Generate a Reversal Task

```bash
taskgen reversal --repo <owner/repo> --pr <num>
```

<details>
<summary>Options</summary>

- `--output PATH` — Output directory for generated tasks (default: `tasks`)
- `--state-dir PATH` — State directory for cache/logs (default: `.state`)
- `--cc-timeout N` — Claude Code session timeout in seconds (default: 3200)
- `--env, -e TYPE` — Environment type: `docker`, `daytona`, `e2b`, `modal`, `runloop`, `gke` (default: `docker`)
- `--no-validate` — Skip Harbor validations
- `--network-isolated` — Also run network-isolated validation
- `--force` — Bypass local dedupe and regenerate
- `--no-cache` — Disable cached artifacts from previous tasks
- `--no-require-minimum-difficulty` — Skip 3+ file and LLM substantiality checks
- `--min-source-files N` — Minimum number of source files required (default: 3, tests excluded)
- `--max-source-files N` — Maximum number of source files to avoid large refactors (default: 10, tests excluded)
- `--no-require-issue` — Allow PRs without linked issues (uses PR body/title for instructions)
- `-v, --verbose` / `-q, --quiet`

</details>

### Continuous PR Farming

Stream through entire PR history, process each immediately with automatic state persistence.

```bash
taskgen farm fastapi/fastapi
taskgen farm fastapi/fastapi --resume-from 2024-01-15
taskgen farm fastapi/fastapi --reset
```

**Features:** Page-by-page streaming, automatic resumption, graceful shutdown (Ctrl+C), quality filters (test changes + minimum difficulty)

<details>
<summary>Options</summary>

- `--output PATH` — Output directory for generated tasks (default: `tasks`)
- `--state-dir PATH` — State directory for cache/logs (default: `.state`)
- `--timeout N` — Timeout per PR in seconds (default: 300)
- `--cc-timeout N` — Claude Code session timeout (default: 3200)
- `--task-delay N` — Delay between tasks in seconds (default: 60)
- `--api-delay N` — Delay between GitHub API calls in seconds (default: 0.5)
- `--env, -e TYPE` — Environment type: `docker`, `daytona`, `e2b`, `modal`, `runloop`, `gke` (default: `docker`)
- `--resume-from DATE` — Resume from date or timestamp
- `--reset` — Reset state and start from beginning
- `--dry-run` — Preview without generation
- `--force` — Regenerate even if task already exists (default: true)
- `--no-validate` — Skip Harbor validation step
- `--network-isolated` — Also run network-isolated validation
- `--no-issue-only` — Allow PRs without linked issues (default: requires issues for higher quality instructions)
- `--no-require-minimum-difficulty` — Skip 3+ file and LLM checks
- `--min-source-files N` — Minimum number of source files required (default: 3, tests excluded)
- `--max-source-files N` — Maximum number of source files to avoid large refactors (default: 10, tests excluded)
- `--no-cache` — Disable cached artifacts
- `--docker-prune-batch N` — Run docker cleanup after every N PRs (default: 5, 0 to disable)
- `--skip-list PATH` — Path to file with task IDs to skip (one per line)
- `-v, --verbose`

</details>

### Validate Existing Tasks

```bash
taskgen validate tasks/<task_id>
```

<details>
<summary>Options</summary>

- `--task, -t ID` — Task ID when path points to dataset root
- `--agent TYPE` — `both`, `nop`, or `oracle` (default: `both`)
- `--jobs-dir PATH` — Directory to store Harbor job artifacts (default: `.state/harbor-jobs`)
- `--env, -e TYPE` — Environment type: `docker`, `daytona`, `e2b`, `modal`, `runloop`, `gke` (default: `docker`)
- `--network-isolated` — Also run network-isolated validation
- `--timeout-multiplier N` — Multiply default timeouts
- `--max-parallel N` — Max parallel validations (default: 8)
- `--show-passed` — Show passed tasks in batch mode
- `--output, -o PATH` — Write results to file as they complete (batch mode only)
- `-v, --verbose` / `-q, --quiet`

</details>

### Analyze Task Quality

Run agent trials to verify a task is well-specified and solvable:

```bash
taskgen analyze tasks/<task_id>
taskgen analyze tasks/<task_id> -k 5 -a claude-code
```

**Analysis Pipeline:**
1. Static quality check (Harbor's `tasks check`)
2. Run N agent trials (default: 3 with Claude Code)
3. AI-powered failure analysis (Harbor's `jobs summarize`)
4. Instruction sufficiency check (Harbor's `tasks debug`)
5. Solution variance analysis across successful trials

<details>
<summary>Options</summary>

- `-a, --agent TYPE` — Agent to run trials (default: `claude-code`)
- `-m, --model MODEL` — Model for agent trials (default: `anthropic/claude-sonnet-4-20250514`)
- `-k, --n-trials N` — Number of trials (default: 3)
- `--jobs-dir PATH` — Directory to store job artifacts (default: `.state/analyze-jobs`)
- `--analysis-model MODEL` — Model for analysis steps (default: `haiku`)
- `--skip-quality-check` — Skip static quality check
- `--skip-summarize` — Skip failure summarization
- `--timeout-multiplier N` — Multiply default timeouts
- `-v, --verbose`

</details>

## Task Requirements

<details>
<summary>Valid PR criteria</summary>

**Languages:** Any (Python, JavaScript, TypeScript, Go, Rust, Ruby, Java, etc.)

**Valid PRs must:**
- Be merged to primary branch with accessible fork
- Include test changes and corresponding fix
- Have a linked issue for high-quality instructions (bypass with `--no-require-issue`)
- Modify 3-10 source files (configurable with `--min-source-files` and `--max-source-files`, bypass with `--no-require-minimum-difficulty`)
- Pass LLM substantiality evaluation (bypass with `--no-require-minimum-difficulty`)
- Fail tests on reversed baseline, pass after applying fix
- Exclude documentation-only, formatting-only, or version-bump-only changes

</details>

## How It Works

<details>
<summary>Pipeline details</summary>

The pipeline uses a **language-agnostic approach**:

1. **Fetch & Analyze** — Get PR metadata via GitHub API, clone repo, identify test files
2. **Evaluate** — LLM evaluates PR substantiality and generates task instructions
3. **Generate Skeleton** — Create Dockerfile and test.sh with TODOs for Claude Code
4. **Claude Code Completion** — CC analyzes repo, detects language/runtime/build system, fills in skeleton
5. **Validation** — Run NOP (reward=0) and Oracle (reward=1) agents
6. **Iteration** — CC iterates until both agents pass

**Key Details:**
- Dockerfile clones at HEAD, then applies `bug.patch` to revert to buggy BASE state
- Test files stored in `task/tests/` and copied at runtime (prevents agent tampering)
- `fix.patch` (solution) excludes tests/CI, contains all other PR changes
- Dependencies installed at build time; runtime doesn't require internet access
- Successful tasks are cached as references to speed up future tasks from the same repo
- PR evaluation uses LLM to check substantiality and generate instructions

</details>

## Examples

```bash
# Generate a Python task
taskgen reversal --repo kludex/starlette --pr 2949

# a JavaScript task
taskgen reversal --repo axios/axios --pr 7150

# Continuous farming
taskgen farm colinhacks/zod

# Validate existing task
taskgen validate examples/axios__axios-7150

# Analyze task quality with agent trials
taskgen analyze examples/axios__axios-7150
```

## License

[Apache License 2.0](LICENSE)
