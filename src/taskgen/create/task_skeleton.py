from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harbor.models.task.config import (
    AgentConfig,
    EnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)

from .utils import strip_tests_prefix


@dataclass
class SkeletonParams:
    """Parameters for skeleton generation (all deterministic from git)."""

    repo_url: str
    head_sha: str
    base_sha: str


def generate_dockerfile(params: SkeletonParams) -> str:
    """
    Generate a minimal, language-agnostic Dockerfile skeleton.

    The skeleton contains:
    - Deterministic parts filled in (git clone, SHAs, bug.patch application)
    - TODO comments for Claude Code to fill in (runtime, deps, build)

    Claude Code will analyze the repo and fill in:
    - Language runtime installation
    - Package manager setup
    - Dependency installation
    - Build steps (if needed)
    - Post-patch rebuild (if needed)
    """
    return f"""FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \\
    git \\
    curl \\
    ca-certificates \\
    patch \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

# TODO: Install language runtime
# Analyze the repo to determine what's needed. Examples:
#   Python: apt-get install python3 python3-pip python3-venv python3-dev
#   Node.js: curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs
#   Go: Download from golang.org/dl or use apt
#   Rust: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
#   Ruby: apt-get install ruby ruby-dev
#   Java: apt-get install openjdk-17-jdk
# Check .nvmrc, .python-version, .ruby-version, go.mod, rust-toolchain.toml, etc.

# TODO: Install additional system packages if needed
# Check CI config (.github/workflows/*.yml) for hints about required packages
# Examples: python3-dev, libssl-dev, pkg-config, cmake, etc.

# TODO: Set up package manager if needed
# For Python: PREFER uv (much faster than pip)
#   curl -LsSf https://astral.sh/uv/install.sh | sh && mv /root/.local/bin/uv /usr/local/bin/uv
# For Node.js: corepack enable (for yarn/pnpm) or npm is built-in
# For Ruby: gem install bundler

WORKDIR /app

# Clone repo at HEAD commit (with fix applied)
RUN git clone {params.repo_url} src && \\
    cd src && \\
    git fetch origin {params.head_sha} && \\
    git checkout {params.head_sha} && \\
    git submodule update --init --recursive

WORKDIR /app/src

# TODO: Set environment variables if needed
# Check CI config and README for required env vars
# Examples: CI=true, NODE_ENV=test, CARGO_TERM_COLOR=never

# TODO: Install dependencies
# For Python: PREFER uv (much faster). Create venv and install:
#   uv venv /opt/venv
#   uv pip install --python /opt/venv/bin/python -e ".[dev,test]"
#   # Or: uv pip install --python /opt/venv/bin/python -r requirements.txt
#   # Then add to PATH: ENV PATH="/opt/venv/bin:${{PATH}}"
# For Node.js: npm ci, yarn install --frozen-lockfile, pnpm install --frozen-lockfile
# For Go: go mod download
# For Rust: cargo fetch
# For Ruby: bundle install
# For Java: mvn dependency:resolve or gradle dependencies

# TODO: Build if needed (check if it's a compiled language or has build step)
# Examples:
#   TypeScript: npm run build, tsc
#   Rust: cargo build
#   Go: go build ./...
#   Java: mvn compile, gradle build

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

# TODO: Rebuild after applying bug.patch if needed
# For compiled languages (TypeScript, Rust, Go, Java), you MUST rebuild after patching

RUN rm -rf /app/src/.git

WORKDIR /app/src
"""


def generate_test_sh(
    test_files: list[str],
) -> str:
    """
    Generate a minimal, language-agnostic test.sh skeleton.

    The skeleton contains:
    - Test file copy commands (deterministic)
    - TODO for Claude Code to fill in the actual test command

    Claude Code will analyze the repo and fill in:
    - Test framework detection
    - Correct test command with specific file paths
    """
    # Build copy commands for test files
    if test_files:
        copy_lines = []
        for tf in test_files:
            # Handle common test directory prefixes
            source_path = strip_tests_prefix(tf)
            target_dir = str(Path(tf).parent)
            copy_lines.append(f'mkdir -p "{target_dir}"')
            copy_lines.append(f'cp "/tests/{source_path}" "{tf}"')
        copy_commands = "\n".join(copy_lines)

        # Build example test file list for comments
        test_files_example = " ".join([f'"{tf}"' for tf in test_files[:5]])
        if len(test_files) > 5:
            test_files_example += f" # ... and {len(test_files) - 5} more"
    else:
        copy_commands = "# No test files to copy"
        test_files_example = ""

    return f"""#!/bin/bash

cd /app/src

# TODO: Set environment variables if needed for tests
# Examples: CI=true, NODE_ENV=test, RUST_BACKTRACE=1

# Copy HEAD test files from /tests (overwrites BASE state)
{copy_commands}

# CRITICAL: Run ONLY the specific test files from the PR, NOT the entire test suite!
# The test files to run are: {test_files_example if test_files_example else "(see list above)"}
#
# TODO: Fill in the actual test command to run ONLY these specific files
#
# DO NOT run the entire test suite - it's too slow and may have unrelated failures!
#
# Examples for different languages/frameworks:
#
# Python (pytest with uv):
#   # If using uv venv at /opt/venv:
#   source /opt/venv/bin/activate
#   uv pip install -e . --no-deps 2>/dev/null || true  # Reinstall to pick up changes
#   pytest -xvs path/to/test_file.py
#   # Or without venv activation:
#   /opt/venv/bin/pytest -xvs path/to/test_file.py
#
# JavaScript/TypeScript (IMPORTANT: disable coverage thresholds when running subset!):
#   npx jest path/to/test.js path/to/test2.js --coverage=false
#   npx vitest run path/to/test.ts --coverage.enabled=false
#   npx mocha path/to/test.js path/to/test2.js
#   npx borp path/to/test.js --no-check-coverage   # Used by fastify, pino, etc.
#   npx tap path/to/test.js --no-check-coverage    # Node TAP framework
#   npx ava path/to/test.js                        # AVA framework
#
#   CRITICAL for JS/TS: DO NOT use "npm test" or "npm run test" without args!
#   These run the ENTIRE suite. Pass specific files via the test runner directly.
#   If you must use npm: npm run test -- path/to/test.js (note the -- separator)
#
# Go:
#   go test -v ./path/to/package/...
#   go test -v -run TestSpecificName ./...
#
# Rust:
#   cargo test --test test_name -- --nocapture
#   cargo test specific_test_name -- --nocapture
#
# Ruby (RSpec/Minitest):
#   bundle exec rspec path/to/spec.rb
#   bundle exec ruby -Itest path/to/test.rb
#
# Java (JUnit/Maven/Gradle):
#   mvn test -Dtest=TestClassName
#   gradle test --tests TestClassName

# TODO: Replace this placeholder with actual test command running ONLY the specific test files above
echo "ERROR: Test command not filled in! Must run specific test files, not entire suite." >&2
false
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"
"""


def generate_solve_sh() -> str:
    """Generate solution/solve.sh script (same for all tasks)."""
    return """#!/bin/bash

set -euo pipefail
cd /app/src

patch -p1 < /solution/fix.patch
"""


def generate_instruction_md(instruction_data: dict) -> str:
    """Generate instruction.md file for Harbor format."""
    return instruction_data["instruction"]


def generate_task_toml(instruction_data: dict) -> str:
    """Generate task.toml config file for Harbor format.

    Uses Harbor's TaskConfig for proper serialization and validation.
    """
    config = TaskConfig(
        metadata={
            "difficulty": instruction_data.get("difficulty", "medium"),
            "category": instruction_data.get("category", "bugfix"),
            "tags": instruction_data.get("tags", []),
        },
        verifier=VerifierConfig(timeout_sec=600.0),
        agent=AgentConfig(timeout_sec=600.0),
        environment=EnvironmentConfig(
            build_timeout_sec=600.0,
            cpus=1,
            memory_mb=2048,
            storage_mb=10240,
        ),
    )
    return config.model_dump_toml()
