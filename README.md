# SWE-gen

> Convert merged GitHub PRs into [Harbor](https://github.com/laude-institute/harbor) tasks automatically.

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)

## Overview

Automates creation of Harbor tasks from real-world bug fixes in open-source repositories. Works with **any programming language**: Claude Code analyzes the repo to detect language, runtime, build system, and test framework.

Each task reverses a merged PR to recreate the buggy state, validates tests fail on baseline, and pass after applying the fix. Fully containerized with all dependencies installed at build time.

## Quick Start

```bash
# Install
uv pip install -e .

# Generate a task from a merged PR
swegen create --repo axios/axios --pr 7150 --verbose

# Or farm all PRs from a repo
swegen farm fastapi/fastapi
```

## Installation

```bash
uv pip install -e .
```

**Requirements:**
- Python 3.12+
- Docker
- uv
- [Claude Code CLI](https://github.com/anthropics/claude-code)

**Secrets:** Create a `.env` file:

```bash
export GITHUB_TOKEN=<gh-token>
export OPENAI_API_KEY=<api-key>
export ANTHROPIC_API_KEY=<api-key>  # or CLAUDE_CODE_OAUTH_TOKEN
```

**Note:** Cloud sandbox environments (Daytona, E2B, Modal, etc.) require additional API keys.

## Usage

**Commands:**
- `swegen create` — Generate task from a merged PR (validates by default)
- `swegen farm` — Continuously process PRs from a repository
- `swegen validate` — Validate existing Harbor task (NOP + Oracle)
- `swegen analyze` — Deep analysis with agent trials to verify task quality

### Generate a Task

```bash
swegen create --repo <owner/repo> --pr <num>
```

<details>
<summary>Options</summary>

- `--output PATH` — Output directory for generated tasks (default: `tasks`)
- `--state-dir PATH` — State directory for cache/logs (default: `.state`)
- `--cc-timeout N` — Claude Code session timeout in seconds (default: 3200)
- `--env, -e TYPE` — Environment type: `docker`, `daytona`, `e2b`, `modal`, `runloop`, `gke` (default: `docker`)
- `--no-validate` — Skip Harbor validations
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
swegen farm fastapi/fastapi
swegen farm fastapi/fastapi --resume-from 2024-01-15
swegen farm fastapi/fastapi --reset
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
- `--issue-only` — Only process PRs with linked issues (default: True)
- `--no-require-minimum-difficulty` — Skip 3+ file and LLM checks
- `--min-source-files N` — Minimum number of source files required (default: 3, tests excluded)
- `--max-source-files N` — Maximum number of source files to avoid large refactors (default: 10, tests excluded)
- `--no-cache` — Disable cached artifacts
- `--docker-prune-batch N` — Run docker cleanup after every N PRs (default: 5, 0 to disable)
- `--skip-list PATH` — Path to file with task IDs to skip (one per line)
- `-v, --verbose`

</details>

### Validate Existing Tasks

Verify that a task passes NOP (baseline fails) and Oracle (solution succeeds) agents:

```bash
swegen validate tasks/<task_id>
```

<details>
<summary>Options</summary>

- `--task, -t ID` — Task ID when path points to dataset root
- `--agent TYPE` — `both`, `nop`, or `oracle` (default: `both`)
- `--jobs-dir PATH` — Directory to store Harbor job artifacts (default: `.state/harbor-jobs`)
- `--env, -e TYPE` — Environment type: `docker`, `daytona`, `e2b`, `modal`, `runloop`, `gke` (default: `docker`)
- `--timeout-multiplier N` — Multiply default timeouts
- `--max-parallel N` — Max parallel validations (default: 8)
- `--show-passed` — Show passed tasks in batch mode
- `--output, -o PATH` — Write results to file as they complete (batch mode only)
- `--docker-prune-batch N` — Run docker cleanup after every N tasks (default: 5, 0 to disable)
- `-v, --verbose` / `-q, --quiet`

</details>

### Analyze Task Quality

Run agent trials to verify a task is well-specified and solvable:

```bash
swegen analyze tasks/<task_id>
swegen analyze tasks/<task_id> -k 5 -a claude-code
```

<details>
<summary>Analysis Pipeline</summary>

1. Static quality check (Harbor's `tasks check`)
2. Baseline validation (nop should fail, oracle should pass)
3. Run N agent trials (default: 3 with Claude Code)
4. AI-powered trial classification (identifies TASK vs AGENT problems)
5. Task verdict synthesis with actionable recommendations

**Classification categories:**
- `GOOD_SUCCESS` — Agent solved it correctly
- `BAD_SUCCESS` — Agent cheated or tests too permissive
- `GOOD_FAILURE` — Agent failed due to its own limitations (expected for hard tasks)
- `BAD_FAILURE` — Agent failed due to task issues (underspecified, brittle tests, etc.)
- `HARNESS_ERROR` — Infrastructure problem

</details>

<details>
<summary>Options</summary>

- `-a, --agent TYPE` — Agent to run trials (default: `claude-code`)
- `-m, --model MODEL` — Model for agent trials (default: `anthropic/claude-sonnet-4-5`)
- `-k, --n-trials N` — Number of trials (default: 3)
- `-n, --n-concurrent N` — Number of concurrent trials (default: 3, 1=sequential)
- `--jobs-dir PATH` — Directory to store job artifacts (default: `.state/analyze-jobs`)
- `--analysis-model MODEL` — Model for Claude Code classification (default: `claude-sonnet-4-5`)
- `--env, -e TYPE` — Environment type: `docker`, `daytona`, `e2b`, `modal`, `runloop`, `gke` (default: `docker`)
- `--skip-quality-check` — Skip static quality check
- `--skip-baseline` — Skip baseline validation (nop/oracle)
- `--skip-classify` — Skip AI-powered classification
- `--save-to-dir` — Write trajectory-analysis.{md,json} to each trial directory (for CI integration)
- `--classification-timeout N` — Timeout per trial classification in seconds (default: 300)
- `--verdict-timeout N` — Timeout for verdict synthesis in seconds (default: 180)
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
swegen create --repo kludex/starlette --pr 2949

# a JavaScript task
swegen create --repo axios/axios --pr 7150

# Continuous farming
swegen farm colinhacks/zod

# Validate existing task
swegen validate examples/axios__axios-7150

# Analyze task quality with agent trials
swegen analyze examples/axios__axios-7150
```

## License

[Apache License 2.0](LICENSE)
