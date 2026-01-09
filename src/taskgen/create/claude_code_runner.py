from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    TextBlock,
    query,
)

from taskgen.create.claude_code_utils import Colors, print_sdk_message
from taskgen.tools.harbor_runner import parse_harbor_outcome


@dataclass
class ClaudeCodeResult:
    """Result of the CC session."""

    success: bool
    nop_passed: bool  # reward=0 (tests fail on buggy code)
    oracle_passed: bool  # reward=1 (tests pass after fix)
    error_message: str | None = None
    cc_output: str | None = None


# The prompt for CC when using a reference task (much simpler task)
CC_REFERENCE_PROMPT = """
## Your Task: Fill In Skeleton Using Reference Task as Example

**GREAT NEWS**: We have a working task from PR #{reference_pr} (task: `{reference_task_id}`)!

Your job is MUCH SIMPLER than usual:
1. **Look at the reference task** to see what was added (runtime, packages, env vars, build steps, test command)
2. **Fill in your skeleton's TODOs** with the same things
3. **Update test file paths** to match this PR
4. **Run harbor validation** to confirm it works

## Context

**Repository**: {repo} (cloned at `{repo_path}`)
**Current PR**: #{pr_number}
**Reference Task**: `{reference_task_id}` (from PR #{reference_pr}, tested and validated)
**Current Task Directory**: `{task_dir}` ← Your skeleton (CORRECT hashes already!)
**Reference Task Directory**: `{reference_task_dir}` ← Working example to learn from
**Dataset Path**: `{dataset_path}`

## Test Files for This PR

{test_files_list}

## What's Already Done

✓ Skeleton Dockerfile with CORRECT git SHAs ({head_sha}) and basic structure
✓ Skeleton test.sh with TODO for test command
✓ bug.patch and fix.patch are ready
✓ instruction.md and task.toml are ready
✓ Reference task has working Dockerfile and test.sh as examples

## IMPORTANT: Your Skeleton Already Has Correct Hashes!

**DO NOT copy files from reference and replace hashes** - that's error-prone!

Instead:
1. Read `{task_dir}/environment/Dockerfile` - it has TODO comments
2. Read `{reference_task_dir}/environment/Dockerfile` - see what was filled in
3. Add the same things to YOUR skeleton's TODO sections

The skeleton already has:
✓ Correct git clone URL
✓ Correct HEAD SHA ({head_sha})
✓ Basic apt packages (git, curl, patch, build-essential)
✓ Correct bug.patch application

## Your Process

### Step 1: Compare Reference Dockerfile to Your Skeleton

Read both files:
```bash
# Your skeleton (has TODO comments to fill in)
cat {task_dir}/environment/Dockerfile

# Reference (shows what was filled in for a similar PR)
cat {reference_task_dir}/environment/Dockerfile
```

Look for what the reference added beyond the basic skeleton:
- Language runtime installation (Python, Node.js, Go, Rust, Ruby, Java, etc.)
- Additional system packages (python3-dev, libssl-dev, etc.)
- Package manager setup
- Environment variables (CI=true, NODE_ENV=test, etc.)
- Dependency installation commands
- Build steps
- Post-patch rebuild steps

### Step 2: Fill In Your Skeleton's TODOs

**CRITICAL: Always use Ubuntu base image**
- The skeleton Dockerfile starts with `FROM ubuntu:24.04` - **DO NOT change this**
- **NEVER** use language-specific base images (node:XX, python:XX, golang:XX)
- Install language runtimes via apt-get or official installers

Add the same things from the reference to your skeleton. For example:

**If reference has:**
```dockerfile
# Install Python
RUN apt-get update && apt-get install -y \\
    python3 python3-pip python3-venv python3-dev \\
    && rm -rf /var/lib/apt/lists/*
```

**Then replace your TODO:**
```dockerfile
# TODO: Install language runtime
```

**With the same installation commands.**

**DO NOT just copy the entire reference file** - the git SHAs would be wrong!
**DO fill in the TODOs** using the reference as a guide.

### Step 3: Fill In test.sh Test Command

Read both test files:
```bash
# Your skeleton (has TODO for test command)
cat {task_dir}/tests/test.sh

# Reference (shows what test command worked)
cat {reference_task_dir}/tests/test.sh
```

**CRITICAL**: Update the test command to run ONLY the test files for THIS PR!

**Current test files for THIS PR**:
{test_files_list}

The reference test.sh will show you the test runner pattern.
**Copy the pattern but update the file paths** to match this PR's test files.

**DO NOT use**:
- `npm test`, `pytest`, `go test ./...` without specific paths ❌ (runs entire suite)
- Any command without specific file paths ❌

Replace the TODO placeholder with the actual test command running THIS PR's test files.

### Step 4: Run Harbor Validation

For each validation attempt, increment the run number (-1, -2, -3, etc.):

```bash
# Test NOP - should get reward=0
harbor run --agent nop -p {dataset_path} -t {task_id} --jobs-dir {jobs_dir}/{task_id}-nop-1 --no-delete --env {environment}

# Test Oracle - should get reward=1
harbor run --agent oracle -p {dataset_path} -t {task_id} --jobs-dir {jobs_dir}/{task_id}-oracle-1 --env {environment}
```

If you need to re-run after fixing issues, increment the number:
- First NOP attempt: `{task_id}-nop-1`, second: `{task_id}-nop-2`, etc.
- First Oracle attempt: `{task_id}-oracle-1`, second: `{task_id}-oracle-2`, etc.

### Step 5: Fix Issues (if validation fails)

If harbor fails, check:
1. **Test file paths** - Most common issue (make sure you updated them for THIS PR)
2. **Missing build step** - Did you copy the build steps from reference?
3. **Missing packages** - Did you copy the system packages from reference?
4. **Post-patch rebuild** - For compiled languages, you MUST rebuild after applying bug.patch

### Step 6: Final Cleanup

**Once both NOP (reward=0) and Oracle (reward=1) pass**, clean up your files:

1. **Remove ALL TODO comments** from Dockerfile and test.sh
2. **Remove ALL template/example comments** that are no longer relevant
3. **Keep only meaningful comments** that explain non-obvious steps

**Files to clean:**
- `{task_dir}/environment/Dockerfile` - Remove TODOs, keep comments explaining non-standard steps
- `{task_dir}/tests/test.sh` - Remove TODOs and example templates, keep test-specific comments

## Tips

- **Your skeleton is the source of truth** - it has correct hashes
- **Reference is just an example** - shows you what to fill in
- **Don't copy entire files** - just the extra pieces (runtime, packages, env vars, build steps)
- **Update test paths** - most PRs touch different test files

You're done when both NOP (reward=0) and Oracle (reward=1) pass AND files are cleaned up!
"""

# The prompt for CC to analyze repo and fill in skeleton (from scratch)
CC_PROMPT = """
## Your Task: Make This Harbor Task Work

You have a skeleton Harbor task that needs to be completed. Your job is to:
1. **Analyze the repository** to detect language, build system, test framework, dependencies
2. **Fill in the TODO sections** in Dockerfile and test.sh
3. **Run harbor validation** and iterate until it passes

## Context

**Repository**: {repo} (cloned at `{repo_path}`)
**PR**: #{pr_number}
**Task Directory**: `{task_dir}`
**Dataset Path**: `{dataset_path}`

The repo is already cloned locally. You can browse it, read files, and run commands.

## Skeleton Files to Complete

The skeleton files have been generated with the deterministic parts filled in:
- Git clone commands with correct SHAs ✓
- Basic apt packages (git, curl, ca-certificates, patch, build-essential) ✓
- bug.patch/fix.patch ✓

**You need to fill in the TODOs:**

### `{task_dir}/environment/Dockerfile`
- **Language runtime**: Detect and install (Python, Node.js, Go, Rust, Ruby, Java, etc.)
- **System packages**: Additional packages needed (dev headers, native dependencies)
- **Package manager**: Set up if needed (pip, npm, cargo, bundler, etc.)
- **Environment variables**: CI=true, etc.
- **Dependencies**: Install project dependencies
- **Build step**: If needed (TypeScript, Rust, Go, Java, etc.)
- **Rebuild after bug.patch**: Required for compiled languages

### `{task_dir}/tests/test.sh`
- **Environment variables**: For test runner
- **Test command**: The actual command to run the specific test files

## Step 1: Deep Repository Analysis

Before filling anything in, thoroughly analyze the repository to detect the language and setup:

### 1.1 Detect Language and Runtime

Check for language indicators:
```bash
# List files to detect language
ls -la {repo_path}

# Check for language-specific files
cat {repo_path}/package.json 2>/dev/null        # Node.js/JavaScript/TypeScript
cat {repo_path}/pyproject.toml 2>/dev/null      # Python (modern)
cat {repo_path}/setup.py 2>/dev/null            # Python (legacy)
cat {repo_path}/requirements.txt 2>/dev/null    # Python
cat {repo_path}/go.mod 2>/dev/null              # Go
cat {repo_path}/Cargo.toml 2>/dev/null          # Rust
cat {repo_path}/Gemfile 2>/dev/null             # Ruby
cat {repo_path}/pom.xml 2>/dev/null             # Java (Maven)
cat {repo_path}/build.gradle 2>/dev/null        # Java/Kotlin (Gradle)
```

### 1.2 Check for Version Files
```bash
# Language version specifications
cat {repo_path}/.nvmrc 2>/dev/null              # Node.js
cat {repo_path}/.node-version 2>/dev/null       # Node.js
cat {repo_path}/.python-version 2>/dev/null     # Python (pyenv)
cat {repo_path}/.ruby-version 2>/dev/null       # Ruby
cat {repo_path}/rust-toolchain.toml 2>/dev/null # Rust
cat {repo_path}/.tool-versions 2>/dev/null      # asdf (multiple languages)
```

### 1.3 Check CI Configuration (GOLD MINE for setup hints!)
```bash
cat {repo_path}/.github/workflows/*.yml 2>/dev/null | head -300
```
CI configs often reveal:
- Exact language version and runtime setup
- Required system packages
- Environment variables
- Pre/post-install steps
- How tests are actually run

### 1.4 Check Test Configuration
Look for test framework configs:
```bash
# JavaScript/TypeScript
ls -la {repo_path}/*.config.* {repo_path}/jest.config.* {repo_path}/vitest.config.* 2>/dev/null

# Python
cat {repo_path}/pytest.ini 2>/dev/null
cat {repo_path}/pyproject.toml 2>/dev/null | grep -A20 "tool.pytest"
cat {repo_path}/setup.cfg 2>/dev/null | grep -A10 "tool:pytest"

# Go - tests are built into the language
# Rust - tests are built into the language
# Ruby
cat {repo_path}/.rspec 2>/dev/null
```

### 1.5 Analyze the Test Files
Read the test files from `{task_dir}/tests/` to understand:
- What test framework they use (look at imports)
- Any special setup requirements
- Test file naming conventions

## Test Files from PR

**CRITICAL**: You MUST run ONLY these specific test files, NOT the entire test suite!

These test files have been extracted to `{task_dir}/tests/`:
{test_files_list}

In test.sh, these get copied from `/tests/` into the container before running.

**Your test command MUST run ONLY these files.** Examples by language:

### Python
```bash
pytest -xvs path/to/test_file.py
python -m pytest path/to/test_file.py path/to/test_other.py
```

### JavaScript/TypeScript (TRICKY - read carefully!)

**Common test frameworks and their commands:**
```bash
# Jest (most common)
npx jest test/foo.test.js test/bar.test.js --coverage=false

# Vitest (Vite projects)
npx vitest run test/foo.test.ts --coverage.enabled=false

# Mocha
npx mocha test/foo.test.js test/bar.test.js

# TAP / borp (used by fastify, pino, undici, etc.)
npx borp test/foo.test.js --no-check-coverage
npx tap test/foo.test.js --no-check-coverage

# AVA
npx ava test/foo.test.js

# Node.js native test runner (node:test)
node --test test/foo.test.js
```

**CRITICAL JS/TS GOTCHAS:**
1. **NEVER run `npm test` or `npm run test` without file args** - runs entire suite!
2. **Disable coverage thresholds** - running a subset fails coverage checks:
   - Jest: `--coverage=false`
   - Vitest: `--coverage.enabled=false`
   - TAP/borp: `--no-check-coverage`
3. **TypeScript projects need build step** before AND after applying bug.patch
4. **Check for Deno/Bun-specific tests** - skip if using `Deno.test()` or `bun:test`
5. **Some repos use fixture discovery** (like webpack) - run the discovery test, not fixtures

## JS/TS Test File Compatibility Check (CRITICAL!)

**Not all test files may be compatible with Node.js!** Check test files for:

**Node.js / Jest / Vitest / Mocha tests** (COMPATIBLE):
- Standard ES imports/requires
- Framework-specific APIs: `describe`, `it`, `test`, `expect`

**Deno tests** (INCOMPATIBLE with Node.js - SKIP these):
- `Deno.test()`
- `import {{ ... }} from "https://deno.land/..."`
- `.ts` extensions in imports without bundler

**Bun tests** (INCOMPATIBLE with Node.js - SKIP these):
- `Bun.test()`
- `import {{ ... }} from "bun:test"`

If you find incompatible test files, **remove them from test.sh** - don't try to run them!

## JS/TS package.json Analysis

When analyzing a Node.js project, check package.json carefully:
```bash
cat {repo_path}/package.json
```

Look for:
- `engines.node` - Required Node version
- `scripts.test` - What runs tests? (but don't use it directly!)
- `scripts.build` - Build command for TypeScript?
- `dependencies` / `devDependencies`:
  - Test frameworks: jest, vitest, mocha, ava, tap, borp
  - Native modules needing node-gyp: @parcel/watcher, fsevents, better-sqlite3, etc.

## JS/TS Test Configuration Files

Check for coverage thresholds that will fail when running a subset:
```bash
ls -la {repo_path}/*.config.* {repo_path}/.* 2>/dev/null | grep -E "(jest|vitest|mocha|tap|nyc)"
cat {repo_path}/jest.config.* 2>/dev/null | grep -i coverage
cat {repo_path}/.taprc 2>/dev/null
cat {repo_path}/.nycrc* 2>/dev/null
```

If you see coverage thresholds, you MUST disable them:
- TAP/borp: `--no-check-coverage`
- Jest: `--coverage=false`
- Vitest: `--coverage.enabled=false`

### Go
```bash
go test -v ./path/to/package/...
go test -v -run TestSpecificName ./...
```

### Rust
```bash
cargo test --test test_name -- --nocapture
cargo test specific_test_name -- --nocapture
```

### Ruby
```bash
bundle exec rspec spec/path/to/spec.rb
bundle exec ruby -Itest test/path/to/test.rb
```

### Java
```bash
mvn test -Dtest=TestClassName
gradle test --tests TestClassName
```

**DO NOT run the entire test suite** - it's too slow and may have unrelated failures!

## Step 2: Fill In the Skeleton Files

Based on your analysis, edit the Dockerfile and test.sh.

### Dockerfile Guidelines

**CRITICAL: Always use Ubuntu base image**
- The skeleton starts with `FROM ubuntu:24.04` - **DO NOT change this**
- **NEVER** use language-specific base images (node:XX, python:XX, golang:XX)
- Install language runtimes via apt-get or official installers

**Language Runtime Installation Examples:**

**Python (PREFER uv for speed):**
```dockerfile
# Install Python and uv (much faster than pip)
RUN apt-get update && apt-get install -y \\
    python3 python3-pip python3-venv python3-dev \\
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast package management
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \\
    mv /root/.local/bin/uv /usr/local/bin/uv
```

**Node.js (check .nvmrc or package.json engines for version!):**
```dockerfile
# Check .nvmrc, .node-version, or package.json "engines.node" for required version
# Default to Node 20 if not specified
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \\
    apt-get install -y nodejs && \\
    rm -rf /var/lib/apt/lists/*

# Package manager setup - detect from lock file:
#   pnpm-lock.yaml → pnpm
#   yarn.lock → yarn
#   bun.lockb → bun
#   package-lock.json or none → npm

# For pnpm:
RUN corepack enable && corepack prepare pnpm@latest --activate

# For yarn (classic or berry):
RUN corepack enable

# For bun:
RUN curl -fsSL https://bun.sh/install | bash && ln -s /root/.bun/bin/bun /usr/local/bin/bun

# npm is included with Node.js (no extra setup needed)
```

**Node.js native dependencies (node-gyp):**
```dockerfile
# Many npm packages need native compilation (node-gyp)
# Add these if you see gyp errors during npm install:
RUN apt-get update && apt-get install -y \\
    python3 make g++ \\
    && rm -rf /var/lib/apt/lists/*
```

**Go:**
```dockerfile
RUN curl -fsSL https://go.dev/dl/go1.22.0.linux-amd64.tar.gz | tar -C /usr/local -xzf - && \\
    ln -s /usr/local/go/bin/go /usr/local/bin/go
```

**Rust:**
```dockerfile
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${{PATH}}"
```

**Ruby:**
```dockerfile
RUN apt-get update && apt-get install -y ruby ruby-dev && \\
    rm -rf /var/lib/apt/lists/*
RUN gem install bundler
```

**Java:**
```dockerfile
RUN apt-get update && apt-get install -y openjdk-17-jdk maven && \\
    rm -rf /var/lib/apt/lists/*
```

**Dependency Installation Examples:**

- **Python (PREFER uv):**
  ```dockerfile
  # Create venv and install with uv (10-100x faster than pip)
  RUN uv venv /opt/venv && \\
      uv pip install --python /opt/venv/bin/python -e ".[dev,test]"
  # Or for requirements.txt:
  # RUN uv pip install --python /opt/venv/bin/python -r requirements.txt
  ENV PATH="/opt/venv/bin:${{PATH}}"
  ```
- **Node.js (use frozen lockfile!):**
  - npm: `npm ci` (NOT `npm install`)
  - yarn: `yarn install --frozen-lockfile`
  - pnpm: `pnpm install --frozen-lockfile`
  - bun: `bun install`
- **Go:** `go mod download`
- **Rust:** `cargo fetch`
- **Ruby:** `bundle install`
- **Java:** `mvn dependency:resolve`

**Build Steps (for compiled languages):**

After installing dependencies AND after applying bug.patch, you may need to build:
- **TypeScript:** `npm run build` or `tsc` or `yarn build` or `pnpm build`
- **Go:** `go build ./...`
- **Rust:** `cargo build`
- **Java:** `mvn compile` or `gradle build`

**CRITICAL**: For compiled languages, you MUST rebuild AFTER applying bug.patch!

**TypeScript Projects - IMPORTANT:**
```dockerfile
# After npm install - build the project
RUN npm run build
# Or if no build script: RUN npx tsc

# Apply bug.patch
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

# MUST rebuild after patching TypeScript source!
RUN npm run build
```

Check for TypeScript by looking for:
- `tsconfig.json` in repo root
- `.ts` or `.tsx` files in src/
- `typescript` in devDependencies
- `build` or `compile` scripts in package.json

### test.sh Guidelines

**CRITICAL**: Run ONLY the specific test files, NOT the entire test suite!

The test files you MUST run are:
{test_files_list}

Replace the TODO placeholder with the actual test command.

**Test command patterns (run MULTIPLE files by passing all paths):**

```bash
# Python (pytest) - with multiple files
pytest -xvs path/to/test_file.py path/to/test_other.py

# Jest - run specific files (can pass multiple files)
npx jest path/to/test1.js path/to/test2.js --coverage=false

# Vitest - run specific files (can pass multiple files)
npx vitest run path/to/test1.ts path/to/test2.ts --coverage.enabled=false

# TAP / borp - run specific files (disable coverage threshold)
# IMPORTANT: Pass the test file paths directly to the test runner, NOT through npm test
npx borp path/to/test1.js path/to/test2.js --no-check-coverage  # For borp (used by fastify, pino, etc.)
npx tap path/to/test1.js path/to/test2.js --no-check-coverage   # For standard tap

# Mocha - run specific files (can pass multiple files)
npx mocha path/to/test1.js path/to/test2.js

# If you must use npm/pnpm/yarn, use `--` separator and pass file paths:
npm run test -- path/to/test1.js path/to/test2.js
pnpm test -- path/to/test1.js path/to/test2.js
```

**Example with multiple test files:**
If you have test files: `test/foo.test.js`, `test/bar.test.js`, `tests/subdir/baz.test.js`
Run: `npx jest test/foo.test.js test/bar.test.js tests/subdir/baz.test.js --coverage=false`

**CRITICAL WARNING**: Running `npm test` or `npm run test` without file arguments runs the ENTIRE test suite!
This wastes time (100+ seconds), may hit timeouts, and is WRONG for this task.
You MUST pass the specific test file paths as arguments to run ONLY the tests from this PR.

**Discovery-based tests** (like webpack):
Some repos use a test runner that discovers fixtures, not direct test files.
In this case, run the discovery test file, not the individual fixtures.

## Harbor Validation Commands

For each validation attempt, increment the run number (-1, -2, -3, etc.):

```bash
# Test NOP - should get reward=0 (tests FAIL on buggy code)
harbor run --agent nop -p {dataset_path} -t {task_id} --jobs-dir {jobs_dir}/{task_id}-nop-1 --no-delete --env {environment}

# Test Oracle - should get reward=1 (tests PASS after applying fix)
harbor run --agent oracle -p {dataset_path} -t {task_id} --jobs-dir {jobs_dir}/{task_id}-oracle-1 --env {environment}
```

If you need to re-run after fixing issues, increment the number:
- First NOP attempt: `{task_id}-nop-1`, second: `{task_id}-nop-2`, etc.
- First Oracle attempt: `{task_id}-oracle-1`, second: `{task_id}-oracle-2`, etc.

## Success Criteria

You're done when BOTH pass:
- **NOP**: reward=0 (tests fail because bug.patch reverted the fix)
- **Oracle**: reward=1 (tests pass after solve.sh applies the fix)

## Finding Logs

After harbor runs, check `{jobs_dir}`:
- `{jobs_dir}/{task_id}-nop-N/<timestamp>/result.json` - NOP job result (N = run number)
- `{jobs_dir}/{task_id}-oracle-N/<timestamp>/result.json` - Oracle job result

Inside each job directory:
- `result.json` - Overall result with reward
- `verifier_stdout.txt` - Test output
- `verifier_stderr.txt` - Test errors

## Common Issues & Fixes

### Docker build fails
- **Missing language runtime** → Add installation commands
- **Missing system packages** → Check CI config, add to apt-get
- **Version mismatch** → Check version files (.nvmrc, .python-version, etc.)
- **Node.js: node-gyp errors** → Add `python3 make g++` to apt-get
- **Node.js: wrong version** → Check .nvmrc or package.json engines field

### Tests fail unexpectedly
- **Missing build step** → Check if compiled language needs build
- **Wrong test command** → Check how tests are run in CI config
- **Missing env vars** → Check CI config for env setup
- **Coverage threshold fails** → Add --no-check-coverage or similar flag

### JS/TS Specific Issues
- **"npm test" runs too many tests** → Use `npx <runner>` with specific files instead
- **Coverage threshold fails** → Add `--coverage=false` (Jest) or `--no-check-coverage` (TAP)
- **TypeScript compilation errors** → Check for missing build step
- **"Cannot find module"** → May need to run build before tests
- **Tests pass but shouldn't** → Check if tests are actually being run (look at output)
- **Deno/Bun tests incompatible** → Skip tests with `Deno.test()` or `bun:test` imports

### NOP gets reward=1 (should be 0)
- Tests don't actually test the bug
- Wrong test files being run
- Tests are skipped or not executed (check test output!)

### Oracle gets reward=0 (should be 1)
- fix.patch doesn't apply cleanly
- **TypeScript: MUST rebuild after patching** (most common JS/TS issue!)
- Missing post-patch setup steps

## Your Approach

1. **Read the skeleton files** first
2. **Detect language** from repo files (package.json, go.mod, Cargo.toml, etc.)
3. **Deep-analyze the repo** (package.json, CI config, test configs, version files)
4. **Check test file compatibility** (JS/TS: filter out Deno/Bun tests!)
5. **Fill in Dockerfile and test.sh**
6. **Run NOP** and iterate until reward=0
7. **Run Oracle** and iterate until reward=1
8. **Clean up files** - Remove ALL TODO comments and template examples
9. Done when both pass AND files are cleaned up!

## Final Cleanup

**Once both NOP (reward=0) and Oracle (reward=1) pass**, you MUST clean up the files:

1. **Remove ALL TODO comments** from Dockerfile and test.sh
2. **Remove ALL template/example comments** (e.g., "Examples: CI=true, NODE_ENV=test...")
3. **Remove large comment blocks** listing framework examples that aren't relevant
4. **Keep only meaningful comments** that explain non-obvious steps specific to this task

**Files to clean:**
- `{task_dir}/environment/Dockerfile` - Remove TODOs, keep comments explaining non-standard steps
- `{task_dir}/tests/test.sh` - Remove TODOs and all example templates, keep only test-specific comments
"""


def run_claude_code_session(
    repo: str,
    pr_number: int,
    repo_path: Path,
    task_dir: Path,
    task_id: str,
    dataset_path: Path,
    test_files: list[str],
    timeout: int = 900,  # 15 minutes
    verbose: bool = False,
    reference_task_id: str | None = None,
    reference_pr: int | None = None,
    head_sha: str | None = None,
    environment: str = "docker",
) -> ClaudeCodeResult:
    """
    Run Claude Code session to complete skeleton and make harbor pass.

    Args:
        repo: Repository in "owner/repo" format
        pr_number: PR number
        repo_path: Path to local repo clone
        task_dir: Path to the task directory
        task_id: Task identifier
        dataset_path: Path to Harbor dataset root
        test_files: List of test file paths
        timeout: Maximum time for session
        verbose: If True, stream output to console
        reference_task_id: If provided, task_id to copy Dockerfile/test.sh from
        reference_pr: If provided, PR number of the reference task
        head_sha: If provided, new HEAD SHA to use in Dockerfile
        environment: Environment type for Harbor runs (docker, daytona, etc.)

    Returns:
        MakeItWorkResult with success status
    """
    # Run async session in sync context
    return asyncio.run(
        _run_claude_code_session_async(
            repo=repo,
            pr_number=pr_number,
            repo_path=repo_path,
            task_dir=task_dir,
            task_id=task_id,
            dataset_path=dataset_path,
            test_files=test_files,
            timeout=timeout,
            verbose=verbose,
            reference_task_id=reference_task_id,
            reference_pr=reference_pr,
            head_sha=head_sha,
            environment=environment,
        )
    )


async def _run_claude_code_session_async(
    repo: str,
    pr_number: int,
    repo_path: Path,
    task_dir: Path,
    task_id: str,
    dataset_path: Path,
    test_files: list[str],
    timeout: int = 900,
    verbose: bool = False,
    reference_task_id: str | None = None,
    reference_pr: int | None = None,
    head_sha: str | None = None,
    environment: str = "docker",
) -> ClaudeCodeResult:
    """Async implementation of Claude Code session."""
    logger = logging.getLogger("taskgen")
    logger.info("Starting Claude Code session for: %s", task_id)

    # Resolve all paths to absolute paths for reliable usage
    dataset_path = Path(dataset_path).resolve()
    task_dir = Path(task_dir).resolve()
    repo_path = Path(repo_path).resolve()

    # Jobs directory for harbor output
    jobs_dir = dataset_path.parent / ".state" / "harbor-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir = jobs_dir.resolve()

    # Format test files list
    if test_files:
        test_files_list = "\n".join(f"  - {tf}" for tf in test_files)
    else:
        test_files_list = "  (none)"

    # Choose prompt based on whether we're using a reference task
    if reference_task_id and reference_pr:
        reference_task_dir = (dataset_path / reference_task_id).resolve()
        prompt_text = CC_REFERENCE_PROMPT.format(
            repo=repo,
            pr_number=pr_number,
            reference_pr=reference_pr,
            reference_task_id=reference_task_id,
            reference_task_dir=reference_task_dir,
            repo_path=repo_path,
            task_dir=task_dir,
            task_id=task_id,
            dataset_path=dataset_path,
            jobs_dir=jobs_dir,
            test_files_list=test_files_list,
            head_sha=head_sha or "(check metadata)",
            environment=environment,
        )
        logger.info(
            f"Using reference prompt (copying from {reference_task_id}, PR #{reference_pr})"
        )
    else:
        prompt_text = CC_PROMPT.format(
            repo=repo,
            pr_number=pr_number,
            repo_path=repo_path,
            task_dir=task_dir,
            task_id=task_id,
            dataset_path=dataset_path,
            jobs_dir=jobs_dir,
            test_files_list=test_files_list,
            environment=environment,
        )
        logger.info("Using full prompt (generating from skeleton)")

    # Create hook for logging Harbor validation attempts
    harbor_runs: list[str] = []

    async def log_harbor_runs(input_data: dict, tool_use_id: str, context: dict) -> dict:
        """Log Harbor validation attempts for debugging."""
        command = input_data.get("tool_input", {}).get("command", "")
        if "harbor run" in command:
            harbor_runs.append(command)
            if verbose:
                print(f"{Colors.YELLOW}[Harbor]{Colors.RESET} {command}", flush=True)
        return {}

    try:
        logger.info("Invoking Claude Code SDK with %ds timeout...", timeout)

        if verbose:
            project_root = os.getcwd()
            print("[SDK] Running Claude Code Agent SDK", flush=True)
            print(f"[SDK] Working directory: {project_root}", flush=True)
            print(f"[SDK] Repo path: {repo_path}", flush=True)
            print(f"[SDK] Task dir: {task_dir}", flush=True)
            print("-" * 60, flush=True)

        # Configure SDK options
        options = ClaudeAgentOptions(
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "LS", "Bash"],
            permission_mode="bypassPermissions",  # Auto-approve actions
            cwd=os.getcwd(),  # Run from project root
            model="sonnet",  # Use Sonnet model
            hooks={
                "PreToolUse": [HookMatcher(matcher="Bash", hooks=[log_harbor_runs])]
            } if verbose else {},
        )

        # Run with timeout
        try:
            async with asyncio.timeout(timeout):
                response_parts = []
                
                if verbose:
                    # Stream messages with real-time display
                    async for message in query(prompt=prompt_text, options=options):
                        print_sdk_message(message)
                        
                        # Collect text for final result
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    response_parts.append(block.text)
                else:
                    # Collect messages without printing
                    async for message in query(prompt=prompt_text, options=options):
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    response_parts.append(block.text)

        except TimeoutError:
            logger.warning("Claude Code session timed out after %ds", timeout)
            if verbose:
                print(f"\n[SDK] Timed out after {timeout}s", flush=True)
            return _check_validation_state(jobs_dir, task_id, logger, timed_out=True)

        if verbose:
            print("-" * 60, flush=True)
            print("[SDK] Session complete", flush=True)

        # Check final state from job files
        return _check_validation_state(jobs_dir, task_id, logger)

    except Exception as e:
        logger.error("Claude Code session failed: %s", e)
        return ClaudeCodeResult(
            success=False,
            nop_passed=False,
            oracle_passed=False,
            error_message=f"SDK failed: {e}",
        )


def _check_validation_state(
    jobs_dir: Path,
    task_id: str,
    logger: logging.Logger,
    timed_out: bool = False,
) -> ClaudeCodeResult:
    """Check validation state from harbor job results."""
    nop_passed, oracle_passed = _check_job_results(jobs_dir, task_id)
    success = nop_passed and oracle_passed

    error_message = None
    if not success:
        parts = []
        if timed_out:
            parts.append("CC timed out")
        if not nop_passed:
            parts.append("NOP failed (expected reward=0)")
        if not oracle_passed:
            parts.append("Oracle failed (expected reward=1)")
        error_message = "; ".join(parts) if parts else None

    return ClaudeCodeResult(
        success=success,
        nop_passed=nop_passed,
        oracle_passed=oracle_passed,
        error_message=error_message,
    )


def _check_job_results(jobs_dir: Path, task_id: str) -> tuple[bool, bool]:
    """Check the actual job results to determine validation state.

    Looks for job directories matching:
    - {task_id}-nop-N (where N is 1, 2, 3, etc.)
    - {task_id}-oracle-N

    Finds the most recent result.json by modification time.
    """
    nop_passed = False
    oracle_passed = False

    if not jobs_dir.exists():
        return nop_passed, oracle_passed

    def find_most_recent_result(pattern: str) -> Path | None:
        """Find most recent result.json matching pattern."""
        best_path = None
        best_mtime = 0.0

        for job_dir in jobs_dir.glob(pattern):
            if not job_dir.is_dir():
                continue
            # Find result.json (Harbor creates a timestamped subdir inside --jobs-dir)
            for result_file in job_dir.rglob("result.json"):
                mtime = result_file.stat().st_mtime
                if mtime > best_mtime:
                    best_mtime = mtime
                    best_path = result_file

        return best_path

    # Find most recent NOP result
    nop_result_path = find_most_recent_result(f"{task_id}-nop-*")
    if nop_result_path:
        reward = parse_harbor_outcome(nop_result_path).reward
        nop_passed = reward == 0

    # Find most recent Oracle result
    oracle_result_path = find_most_recent_result(f"{task_id}-oracle-*")
    if oracle_result_path:
        reward = parse_harbor_outcome(oracle_result_path).reward
        oracle_passed = reward == 1

    return nop_passed, oracle_passed
