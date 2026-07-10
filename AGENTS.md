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

### Publishing tasks (ephemeral sandboxes)

Both `create` and `farm` can publish each validated task to a **dataset repo** as its own
branch + PR, immediately. This is what makes farming safe on ephemeral sandboxes like
Daytona: nothing of value lives only on local disk.

```bash
export GIT_TOKEN=<token>   # contents:write + pull_requests:write on the dataset repo
swegen farm fastapi/fastapi --publish-repo abundant-ai/ots-tasks
```

Per validated task: a branch `task/<task_id>` is cut fresh from `main`, the task directory
is copied to `tasks/<task_id>`, committed as `Add task: <task_id>`, pushed, and opened as a
PR whose body links back to the source PR.

Farm state is committed to a **per-source-repo branch** `farm-state/<owner>__<repo>` on the
same dataset repo after every PR, so a fresh sandbox resumes exactly where the dead one
stopped rather than re-burning Claude Code and OpenAI calls on PRs it already rejected.
Per-repo branch names mean N containers farming N repos never contend.

Key options:
- `--publish-repo`: Dataset repo (`owner/repo`). Its presence enables publishing.
- `--publish-path`: Directory within the dataset repo (default: `tasks`)
- `--publish-base`: Branch task branches are cut from and PRs target (default: `main`)
- `--publish-branch-prefix`: Default `task/`
- `--publish-state-branch-prefix`: Default `farm-state/`
- `--publish-state-path`: Default `state`
- `--publish-clone-dir`: Default `<state-dir>/publish/<dataset_slug>/<source_slug>`
- `--publish-dry-run`: Clone, branch and commit locally; never push or open a PR
- `--cleanup-local`: Delete each local task copy once it is published to the dataset repo

**Contract with the dataset repo:** task PRs touch only `tasks/<task_id>/`, so PRs from
different source repos merge into `main` without conflict. Do **not** add a top-level
manifest or a README table of tasks — it would textually conflict on every merge. Generate
any such index in CI from the directory listing instead.

Tasks are staged with `git add --force`. SWE-gen's own `.gitignore` excludes `tasks/`
(it is the local output directory), and a dataset repo created from this template inherits
that rule — without `--force` the publish would fail on `git add` with "paths are ignored".
Only the one `tasks/<task_id>/` pathspec is ever staged.

**Publish failures stop the run — on the first one.** A bad token fails at preflight, before
any PR is processed. Preflight rejects only an *explicit* `push: false` from `GET /repos`;
fine-grained and app tokens may omit `permissions` entirely, so an absent field warns and
proceeds rather than blocking a token whose pushes would have succeeded.

Once farming is underway, any publish failure aborts: a rejected `git push` cannot be told
apart from a broken remote, and the next PR would spend a full Claude Code session only to
hit the same wall. A failed **state push** aborts for the same reason, including the final
push in `_finalize()` (which sets a nonzero exit code). Both are checked together, so an
abort that happens to hit both reports both — the panel never claims farm state was preserved
when it was not. Non-publish failures (trivial, no-issue, validation) never abort.

A **Claude rate/usage limit** also aborts (category `rate_limited`). Claude Code failures
whose error matches a rate-limit signature — notably `rate_limit_event`, the SDK message
that `claude-agent-sdk` fails to parse — are raised as `ClaudeRateLimitError` rather than
swallowed into a validation failure. Every task draws from the same limit, so continuing is
pointless until the token/account is swapped; the run stops and the source PR is left
unprocessed so a re-run with a fresh token farms it. The abort panel says to swap the token.
The PR is recorded in `claude_rate_limited_prs` (not merely skipped) and exempted from the
resume-time skip, exactly like `publish_failed_prs` — otherwise a PR sharing the resume
cursor's exact `created_at` would be dropped by the fetcher's `>=` and never retried.

Tasks that fail to publish are **kept on disk** (unlike other failures, which are cleaned
up): they passed every validation gate, so they are valid work that can be pushed by hand or
by a re-run.

The source PR is **not marked processed** on a publish failure, so a re-run retries it. This
is the recovery path for a push that succeeds and a `create_pr` that then fails: the retry
refreshes the branch this task left behind and opens the PR that never got created. Marking
it processed would strand a branch on the remote with no PR and no way to reach it again.
`StreamingPRFetcher` also exempts these PRs from the resume-time skip, so one sharing the
cursor's timestamp is retried rather than stranded.

That retry is **publish-only**: the farm republishes the task already on disk rather than
regenerating it. Regeneration runs with `force=True`, which `rmtree`s the task directory
before rebuilding — throwing away a validated task to redo an hour of Claude Code, and losing
it outright if the rebuild then fails. If the task is missing (a fresh sandbox), the retry
falls back to full generation.

When a state push fails, the local mirror has already been written, so nothing is lost from
disk — but the durable cursor is behind, and the abort panel says so and names the mirror.

If a state push is rejected because the remote moved, the loser **merges** rather than
overwrites: `StreamState.merge_from` unions the processed-PR sets and keeps the newer cursor
(newer skips less; `processed_prs` is the authoritative skip list). Blindly recommitting an
in-memory snapshot would erase whatever the other writer just published. One container per
repo means this should never trigger, but losing a durable cursor is not worth the gamble.

Counters (`successful` / `failed` / `total_processed`) are **derived** from the recorded sets
on every mutation, never incremented in place. A PR can move between categories — a publish
failure that later succeeds on retry — and a merge unions two states; incremental counting
drifts in both cases.

`swegen create` publishes **before** writing its `create.jsonl` dedupe record, and the record
carries a `published` flag reflecting what actually reached the dataset repo — `false` under
`--publish-dry-run`, and `false` when the publish raised, in which case the task is *still*
recorded so a rerun's dedupe finds it and republishes rather than hitting `FileExistsError`
and needing `--force`, which would delete it. Records written before this flag existed are
read as published.

A **dedupe hit publishes the existing task** rather than returning early. A task on disk is
not proof it reached the dataset repo — it may predate publishing, or belong to a run whose
push failed. Returning silently would let the farm mark the source PR processed with no PR
ever opened. Publishing is idempotent, so a task already published just yields its PR URL.

An **already-open PR refreshes its branch**; it short-circuits only PR *creation*. The task
may have been regenerated (`--force`, `--reset`, a rerun after a failed `create.jsonl` write),
and returning early would leave the branch and PR on older content while the pipeline reported
success. If the task is already merged into the base branch byte-for-byte there is nothing to
commit, and publish reports success without pushing.

`GitStateStore.load` merges the **local mirror** with the state branch, and the side with the
newer `last_updated` is the merge receiver (ties favour the mirror). A failed push leaves the
mirror ahead of the branch; another sandbox can equally leave the branch ahead of a mirror
sitting on a persistent volume. Reading only one side would forget the other's PRs and drop
`publish_failed_prs`, which the fetcher needs to know a PR still awaits its PR. Sets union
whichever way the merge runs, so no PR is ever lost — the receiver only decides per-PR value
collisions (`task_pr_urls`, `successful_prs`, `other_failed_prs`).

**Dry runs record nothing.** `--dry-run` generates no task, and `--publish-dry-run` pushes
nothing, so neither consumes a source PR — a later real run must still farm it. The
prune/progress cadences are driven by a per-run `prs_seen` counter rather than
`state.total_processed`, which a dry run leaves at zero.

**`--cleanup-local`** deletes each local task directory once it is durably published (a real
branch/PR on the dataset repo), to free disk on constrained sandboxes. It never fires on a
dry run, a publish failure, or when publishing is disabled — the dataset repo must be the
surviving copy. Cleanup is the last step, after the state record and the task-reference save;
the farm suppresses `run_reversal`'s own cleanup (`cleanup_local=False` in the generated
config) so the task dir still exists for the success gate and the reference save, then cleans
up itself. A genuinely missing task dir (a pipeline bug, not a cleanup) still fails the gate.

**A task that exists on disk but not in the dataset repo is republished, not consumed.** If a
farm run hits `FileExistsError` (a validated task dir survives but `create.jsonl` can't
dedupe it — e.g. a `--publish-dry-run` built it, or `.swegen` was cleared) and publishing is
on, the farm republishes the existing task via `publish_existing_task` rather than marking the
PR `already_exists` and moving on unpublished. If that republish fails it becomes
`publish_failed` (task preserved), symmetric with the publish-only retry.

`--reset` with `--publish-repo` overwrites the durable state branch, not just a local file —
every PR recorded there gets regenerated. The farm prints a warning; the behavior is intended.
The overwrite is honored even if the first state push is rejected by a concurrent writer: a
reset run force-pushes rather than merging, or the merge would union the old PRs back in and
silently undo the reset. Only the first save forces; later saves in the same run merge, so a
legitimate concurrent writer is not clobbered on every PR.

`GIT_TOKEN` is separate from `GITHUB_TOKEN` (read-only, used to fetch source PRs) so a farm
run can read from anywhere while only ever writing to one repo. It falls back to
`GITHUB_TOKEN` if unset.

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
├── publish/                # Publish tasks + farm state to a dataset repo
│   ├── base.py             # TaskSink / StateStore protocols, PublishContext/Result
│   ├── git_ops.py          # GitRepo - subprocess git wrapper (clone, branch, worktree)
│   ├── gh_api.py           # GitHubAPI - REST client with bounded retries
│   ├── github_pr.py        # GitHubPRSink - branch + PR per task
│   ├── git_state.py        # GitStateStore - state on farm-state/<slug> branch
│   ├── local_state.py      # LocalStateStore - state on local disk (default)
│   ├── null.py             # NullSink - no publishing (default)
│   └── body.py             # PR title/body/commit message templates
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
- **PublishConfig** - Dataset repo, token, branch/path naming (None disables publishing)

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
├── publish/            # Clone of the dataset repo (when --publish-repo is set)
│   └── <dataset_slug>/<source_slug>/       # main tree: task branches
│   └── <dataset_slug>/<source_slug>-state/ # linked worktree: state branch
└── task_references.json    # Successful task references
```

All of `.swegen/` is lost when an ephemeral sandbox dies. With `--publish-repo`, tasks and
`StreamState` are mirrored to the dataset repo, which is the durable copy.

The task branch and the state branch share one clone but live in **separate working trees**
(`git worktree`), so the state push that follows every PR never disturbs the task branch
being built in the main tree.

Task publishing is serial-only: `StreamFarmer` processes one PR at a time, so a single
working tree with `checkout -B` per task is safe. Concurrent generation within one process
would need a worktree per task.

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

- **PublishError** - A task could not be published (push/PR failed, or a recorded task is missing from disk); farming stops
- **PublishAuthError** - Publish token missing, invalid, or lacking write access; raised at preflight, never retried (subclass of PublishError)

Farm mode classifies failures for reporting:
- Trivial PR (skipped)
- No linked issue (skipped)
- Validation failed
- API rate limit exceeded
- Git checkout failed
- Publish failed

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
