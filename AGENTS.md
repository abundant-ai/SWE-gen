# AGENTS.md - SWE-gen CLI

> Pipeline for converting merged GitHub pull requests into Harbor evaluation tasks.

## Overview

SWE-gen CLI automates the creation of [Harbor](https://github.com/laude-institute/harbor) tasks from real-world bug fixes in open-source repositories. The pipeline:

1. Takes a merged GitHub PR that fixes a bug
2. Reverses the PR to recreate the buggy state
3. Uses Claude Code to detect language and complete the task skeleton
4. Validates that tests fail on the buggy baseline (NOP agent)
5. Validates that tests pass after applying the fix (Oracle agent)
6. Produces a fully containerized, reproducible evaluation task

**Supported Languages:** Any language (Python, JavaScript, TypeScript, Go, Rust, Ruby, Java, etc.)

The pipeline is **language-agnostic** - Claude Code analyzes the repository to automatically detect the language, runtime, build system, and test framework.

## Installation

```bash
uv pip install -e .
```

**Requirements:**
- Python 3.12+
- Docker
- uv
- [Claude Code CLI](https://github.com/anthropics/claude-code)
- GitHub token (for API access)
- OpenAI API key (for PR evaluation)

**Environment variables (.env):**
```bash
export GITHUB_TOKEN=<token>
export OPENAI_API_KEY=<key>
export ANTHROPIC_API_KEY=<key>  # or Claude Code OAuth
```

## CLI Commands

Entry point: `swegen` (defined in `src/swegen/cli.py`)

### `swegen create`
Generate a single Harbor task from a merged PR.

```bash
swegen create --repo <owner/repo> --pr <number>
```

Key options:
- `--cc-timeout`: Timeout for Claude Code session in seconds (default: 3200)
- `--no-validate`: Skip Harbor validation
- `--no-require-issue`: Allow PRs without linked issues
- `--no-require-minimum-difficulty`: Skip 3+ file requirement
- `--no-cache`: Disable reusing cached artifacts from previous tasks
- `--force`: Bypass local dedupe and regenerate

### `swegen farm`
Continuously process PRs from a repository's entire history.

```bash
swegen farm fastapi/fastapi
swegen farm fastapi/fastapi --resume-from 2024-01-15
```

Key options:
- `--dry-run`: Preview without generation
- `--no-require-issue`: Allow PRs without linked issues (default requires issue)
- `--reset`: Start from beginning
- `--timeout`: Timeout per PR in seconds (default: 300)
- `--cc-timeout`: Claude Code session timeout (default: 3200)
- `--task-delay`: Delay between tasks in seconds (default: 60)
- `--no-validate`: Skip Harbor validation
- `--skip-list PATH`: Path to file with task IDs to skip (one per line)

### `swegen validate`
Validate existing Harbor tasks.

```bash
swegen validate tasks/<task_id>
swegen validate tasks/  # Batch mode
```

### `swegen analyze`
Run multiple agent trials and analyze task quality.

```bash
swegen analyze tasks/<task_id> -k 3 -a claude-code
swegen analyze tasks/<task_id> -k 5 -n 3  # run 5 trials, 3 concurrent
```

Key options:
- `-k, --n-trials`: Number of trials to run (default: 3)
- `-n, --n-concurrent`: Number of concurrent trials (default: 3)
- `--analysis-model`: Model for Claude Code classification (default: claude-sonnet-4-5)
- `--skip-baseline`: Skip baseline validation (nop/oracle)
- `--skip-classify`: Skip AI-powered trial classification

**Note**: For programmatic access to classification and verdict synthesis (e.g., CI integration), use the library directly:

```python
from swegen.analyze import classify_trial, compute_task_verdict

# Classify a single trial (simplest API)
classification = classify_trial("path/to/trial", "path/to/task")
print(classification.classification)  # GOOD_SUCCESS, BAD_FAILURE, etc.

# Compute verdict from multiple classifications
verdict = compute_task_verdict([classification1, classification2, ...])
print(verdict.is_good, verdict.primary_issue)
```

---

## Architecture

```
src/swegen/
├── cli.py                  # Typer CLI entry point
├── config.py               # Configuration dataclasses
├── create/                 # Core task generation logic
│   ├── orchestrator.py     # PRToHarborPipeline - main orchestrator
│   ├── create.py           # run_reversal() - CLI command implementation
│   ├── pr_fetcher.py       # GitHub API interactions
│   ├── repo_cache.py       # Local git repo caching
│   ├── task_skeleton.py    # Language-agnostic skeleton generation
│   ├── task_instruction.py # PR evaluation and instruction generation
│   ├── claude_code_runner.py   # Claude Code integration
│   ├── claude_code_utils.py    # Claude Code utilities
│   ├── task_reference.py   # Cache successful tasks for reuse
│   ├── diff_utils.py       # Git diff utilities
│   └── utils.py            # Utility functions and test file detection
├── analyze/                # Task quality analysis
│   ├── run.py              # run_analyze() - main analysis orchestrator
│   ├── classifier.py       # AI-powered failure classification
│   ├── models.py           # Pydantic models for analysis
│   ├── classify_prompt.txt # Prompt for failure classification
│   └── verdict_prompt.txt  # Prompt for solution verdict
├── farm/                   # Continuous PR farming
│   ├── stream_farm.py      # StreamFarmer - main farming loop
│   ├── farm_hand.py        # Per-PR processing logic
│   ├── fetcher.py          # StreamingPRFetcher - GitHub PR streaming
│   └── state.py            # StreamState - persistence for resumability
└── tools/                  # Utility tools
    ├── validate.py         # Harbor NOP/Oracle validation
    ├── harbor_runner.py    # Harbor CLI wrapper
    └── validate_utils.py   # Validation helpers
```

---

## Pipeline Flow

### Task Generation (`generate_task`)

The pipeline uses a **single flow** that works for any language:

1. **Fetch PR metadata** via GitHub API (`pr_fetcher.py`)
2. **Check multi-file requirement** - must modify 3+ source files
3. **Identify test files** - language-agnostic patterns (`tests/`, `test_*`, `*.test.*`, etc.)
4. **Clone repo** to local cache with proper SHA checkout
5. **Generate diffs** - `bug.patch` (reverts PR) and solution diff (the fix, saved as `fix.patch`)
6. **Evaluate PR** - LLM call (`task_instruction.py`) to check substantiality and generate instructions
7. **Generate skeleton files** (`task_skeleton.py`):
   - `environment/Dockerfile` - clones at HEAD, has TODOs for Claude Code
   - `environment/bug.patch` - reverts all PR changes
   - `tests/test.sh` - has TODOs for test command
   - `instruction.md` - bug description from linked issue or PR
   - `task.toml` - task metadata
   - `solution/fix.patch` - the actual fix
   - `solution/solve.sh` - applies fix.patch
8. **Run Claude Code** to complete skeleton:
   - Detect language and runtime
   - Fill in Dockerfile (runtime, packages, deps, build steps)
   - Fill in test.sh (correct test command for specific files)
   - Run Harbor validation and iterate until passing
9. **Save task reference** for future PRs from same repo

### Claude Code Integration

Claude Code is **required** for all tasks. It receives a detailed prompt with:
- Repository path and context
- Skeleton files with TODO markers
- Test file list
- Instructions for detection and validation

Claude Code:
1. Analyzes the repo to detect language, package manager, test framework
2. Fills in the Dockerfile TODOs (runtime, packages, deps, build, post-patch rebuild)
3. Fills in test.sh with the correct test command for specific files
4. Runs `harbor run --agent nop` and `harbor run --agent oracle`
5. Iterates until both pass (NOP=reward=0, Oracle=reward=1)

---

## Key Concepts

### Reversed Baseline Strategy

The core insight: instead of recreating the buggy state, we start at HEAD (fixed) and apply `bug.patch` to revert to the buggy state.

- Container clones at HEAD commit (with the fix)
- `bug.patch` reverts ALL PR changes to BASE state
- Agent sees the buggy codebase
- Oracle applies `fix.patch` to restore the fix
- Test files are extracted separately and copied at verification time

### Test File Handling

Test files are:
- **Excluded** from `bug.patch` and `fix.patch`
- **Extracted** from HEAD and stored in `task/tests/`
- **Copied** into the container at verification time via test.sh

This prevents agents from seeing/modifying tests.

### PR Evaluation

The `task_instruction.py` module uses OpenAI's structured outputs to evaluate PRs:
- **Substantiality Check** - Must modify multiple source files (configurable min/max), not just docs/CI/formatting
- **Instruction Generation** - Concise bug report extracted from linked issue (preferred) or PR description
- **Metadata** - Difficulty level, category, and tags for task classification

### Task References

Successful tasks are cached as references for future PRs from the same repo:
- Dockerfile and test.sh patterns are reused
- When processing a new PR, Claude Code can copy from the reference
- Significantly speeds up task generation after the first successful task

Reference prompts are simpler - Claude Code just adapts the existing pattern rather than analyzing from scratch.

---

## Farm Module

The farm system enables continuous processing of a repository's PR history:

### Components

- **StreamFarmer** (`stream_farm.py`) - Main orchestration class
  - Handles graceful shutdown (Ctrl+C)
  - Periodic Docker cleanup
  - Progress reporting

- **StreamingPRFetcher** (`fetcher.py`) - Streams PRs page-by-page
  - Filters by merge status, test changes, file count
  - Respects API rate limits

- **StreamState** (`state.py`) - Persistence for resumability
  - Tracks processed PRs, success/failure counts
  - Saves to `.swegen/stream_farm/<repo>.json`

- **farm_hand** (`farm_hand.py`) - Per-PR processing
  - Calls `run_reversal()` for each PR
  - Classifies failures (trivial, no issue, validation failed, etc.)

### Filtering

PRs are filtered by:
- Must be merged to primary branch
- Must include test changes
- Must modify minimum number of files (configurable with `--min-source-files` and `--max-source-files`)
- Must have linked issue by default (disable with `--no-require-issue`)
- Must pass LLM substantiality check (disable with `--no-require-minimum-difficulty`)

---

## Tools Module

### Validation (`validate.py`)

Runs Harbor NOP and Oracle agents:
- **NOP**: Does nothing, expects tests to fail (reward=0)
- **Oracle**: Applies fix.patch, expects tests to pass (reward=1)

Supports batch mode for validating multiple tasks in parallel.

### Analysis (`analyze/`)

Comprehensive task quality analysis module:
1. Static quality check (Harbor's checker)
2. Baseline validation (nop should fail, oracle should pass)
3. Multiple agent trials (default: 3, configurable concurrency)
4. AI-powered trial classification (identifies TASK vs AGENT problems)
5. Task verdict synthesis with actionable recommendations

**Classification System:**
- Uses Claude Code to analyze each trial's trajectory and test results
- Distinguishes between task problems (BAD_FAILURE/BAD_SUCCESS) and agent limitations (GOOD_FAILURE)
- Provides evidence, root cause analysis, and recommendations for task improvements
- Aggregates results across all trials to compute overall task verdict

Components:
- **run.py** - Main analysis orchestrator
- **classifier.py** - AI-powered failure classification using Claude Agent SDK
- **models.py** - Pydantic models for analysis results
- **classify_prompt.txt** - Prompt template for failure classification
- **verdict_prompt.txt** - Prompt template for solution verdict

### Harbor Runner (`harbor_runner.py`)

Wrapper around Harbor CLI:
- Finds `harbor` binary or uses `uv run harbor`
- Parses job results using Harbor's Pydantic models
- Manages job directories and cleanup

---

## Configuration

All configuration is done via dataclasses in `config.py`:

- **CreateConfig** - Single PR → task conversion
- **FarmConfig** - Continuous PR farming
- **ValidateConfig** - Task validation

Key defaults:
- Claude Code always used for task completion
- Minimum 3 source files required for task generation (configurable via `--min-source-files`)
- Maximum 10 source files to avoid large refactors (configurable via `--max-source-files`)
- Linked issue required for high-quality instructions (disable with `--no-require-issue`)
- Task references enabled by default for faster generation
- Harbor validation enabled by default (disable with `--no-validate`)
- Farm command: `--require-issue` defaults to True (only process PRs with linked issues)
- Farm command: `--cc-timeout` defaults to 3200 seconds (~53 minutes)

---

## State Management

State is persisted in `.swegen/`:

```
.swegen/
├── create.jsonl      # Processed PRs (deduplication)
├── stream_farm/        # Farm state per repo
│   └── <repo>.json
├── repos/              # Cached git repos
├── harbor-jobs/        # Harbor job artifacts
├── logs/               # Generation logs
└── task_references.json    # Successful task references
```

---

## Output Structure

Generated tasks follow Harbor's structure:

```
tasks/<owner>__<repo>-<number>/
├── environment/
│   ├── Dockerfile      # Builds container with buggy code
│   └── bug.patch       # Reverts PR to create buggy state
├── instruction.md      # Bug description for the agent
├── task.toml           # Task metadata (difficulty, tags, etc.)
├── solution/
│   ├── fix.patch       # The actual fix (excludes tests)
│   └── solve.sh        # Applies fix.patch
└── tests/
    ├── test.sh         # Runs the test suite (specific files only)
    └── *.*             # Extracted test files (copied at runtime)
```

---

## Error Handling

Common error types:
- **TrivialPRError** - PR doesn't meet minimum difficulty
- **MissingIssueError** - No linked issue (when required)
- **ValidationError** - Harbor NOP/Oracle validation failed
- **FileExistsError** - Task already exists (use `--force`)

Farm mode classifies failures for reporting:
- Trivial PR (skipped)
- No linked issue (skipped)
- Validation failed
- API rate limit exceeded
- Git checkout failed

---

## Dependencies

Key dependencies (from `pyproject.toml`):
- **PyGithub** - GitHub API
- **GitPython** - Git operations
- **typer** - CLI framework
- **rich** - Console output
- **docker** - Docker API
- **openai** - LLM evaluation
- **pydantic** - Data validation
- **requests** - HTTP client
- **harbor** - Harbor evaluation framework (git dependency)
