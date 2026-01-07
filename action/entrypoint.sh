#!/bin/bash
set -euo pipefail

# =============================================================================
# Harbor Task Checker - GitHub Action Entrypoint
# 
# This script wraps the taskgen CLI to:
# 1. Extract PR context from GitHub environment
# 2. Run taskgen create with appropriate flags
# 3. Format output for GitHub (Job Summary, annotations, outputs)
# 4. Upload task artifact if validation passes
# =============================================================================

# -----------------------------------------------------------------------------
# Parse GitHub context
# -----------------------------------------------------------------------------

echo "::group::Parsing GitHub context"

# Extract PR info from event payload
if [[ ! -f "$GITHUB_EVENT_PATH" ]]; then
    echo "::error::GITHUB_EVENT_PATH not set or file doesn't exist"
    exit 1
fi

PR_NUMBER=$(jq -r '.pull_request.number // .issue.number // empty' "$GITHUB_EVENT_PATH")
REPO=$(jq -r '.repository.full_name' "$GITHUB_EVENT_PATH")
PR_MERGED=$(jq -r '.pull_request.merged // false' "$GITHUB_EVENT_PATH")
PR_STATE=$(jq -r '.pull_request.state // "unknown"' "$GITHUB_EVENT_PATH")
PR_TITLE=$(jq -r '.pull_request.title // "Unknown"' "$GITHUB_EVENT_PATH")

if [[ -z "$PR_NUMBER" || -z "$REPO" ]]; then
    echo "::error::Could not extract PR context from event. Is this running on a pull_request event?"
    exit 1
fi

echo "Repository: $REPO"
echo "PR Number: $PR_NUMBER"
echo "PR Title: $PR_TITLE"
echo "PR State: $PR_STATE"
echo "PR Merged: $PR_MERGED"
echo "::endgroup::"

# -----------------------------------------------------------------------------
# Input validation
# -----------------------------------------------------------------------------

SKIP_VALIDATION="${INPUT_SKIP_VALIDATION:-false}"
SKIP_LLM="${INPUT_SKIP_LLM_CHECK:-false}"
REQUIRE_MERGED="${INPUT_REQUIRE_MERGED:-false}"
MIN_SOURCE_FILES="${INPUT_MIN_SOURCE_FILES:-3}"
MAX_SOURCE_FILES="${INPUT_MAX_SOURCE_FILES:-10}"
CC_TIMEOUT="${INPUT_CC_TIMEOUT:-1800}"

# Check if PR needs to be merged
if [[ "$REQUIRE_MERGED" == "true" && "$PR_MERGED" != "true" ]]; then
    echo "::notice title=Harbor Task Check::PR is not merged yet. Skipping validation (require_merged=true)"
    echo "eligible=false" >> "$GITHUB_OUTPUT"
    echo "reason=PR must be merged before validation" >> "$GITHUB_OUTPUT"
    
    cat >> "$GITHUB_STEP_SUMMARY" << 'EOF'
# ⏳ Waiting for Merge

This PR has not been merged yet. Harbor task eligibility will be checked after merge.

Set `require_merged: false` in the workflow to check open PRs.
EOF
    exit 0
fi

# Check for Claude Code authentication (OAuth token preferred, API key as fallback)
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
    if [[ "$SKIP_VALIDATION" != "true" ]]; then
        echo "::warning title=Missing API Key::Neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY set. Full validation requires Claude Code."
        echo "::notice::Setting skip_validation=true due to missing authentication"
        SKIP_VALIDATION="true"
    fi
elif [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    echo "Using Claude Code OAuth token for authentication"
else
    echo "Using Anthropic API key for authentication"
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    if [[ "$SKIP_LLM" != "true" ]]; then
        echo "::warning title=Missing API Key::OPENAI_API_KEY not set. LLM substantiality check will be skipped."
        SKIP_LLM="true"
    fi
fi

# -----------------------------------------------------------------------------
# Build taskgen command
# -----------------------------------------------------------------------------

# Use GITHUB_WORKSPACE so artifacts persist between steps
TASK_OUTPUT="${GITHUB_WORKSPACE:-/tmp}/harbor-tasks"
STATE_DIR="${GITHUB_WORKSPACE:-/tmp}/.state"
mkdir -p "$TASK_OUTPUT" "$STATE_DIR"

# Generate task ID (lowercase, replace / with __)
TASK_ID="${REPO//\//__}-${PR_NUMBER}"
TASK_ID=$(echo "$TASK_ID" | tr '[:upper:]' '[:lower:]')

echo "::group::Building taskgen command"
echo "Task ID: $TASK_ID"

CMD="taskgen create"
CMD+=" --repo $REPO"
CMD+=" --pr $PR_NUMBER"
CMD+=" --output $TASK_OUTPUT"
CMD+=" --state-dir $STATE_DIR"
CMD+=" --force"  # Always regenerate in CI
CMD+=" --no-require-issue"  # Don't require linked issue for action
CMD+=" --min-source-files $MIN_SOURCE_FILES"
CMD+=" --max-source-files $MAX_SOURCE_FILES"
CMD+=" --cc-timeout $CC_TIMEOUT"
CMD+=" --verbose"

# Allow unmerged PRs in CI (for testing/preview on open PRs)
if [[ "$REQUIRE_MERGED" != "true" ]]; then
    CMD+=" --allow-unmerged"
fi

if [[ "$SKIP_VALIDATION" == "true" ]]; then
    CMD+=" --no-validate"
    echo "Validation: SKIPPED"
else
    echo "Validation: ENABLED"
fi

if [[ "$SKIP_LLM" == "true" ]]; then
    CMD+=" --no-require-minimum-difficulty"
    echo "LLM Check: SKIPPED"
else
    echo "LLM Check: ENABLED"
fi

echo "Command: $CMD"
echo "::endgroup::"

# -----------------------------------------------------------------------------
# Run taskgen
# -----------------------------------------------------------------------------

echo "::group::Running taskgen create"

TASK_DIR="$TASK_OUTPUT/$TASK_ID"
LOG_FILE="/tmp/taskgen-output.log"

# Capture output and exit code
set +e
$CMD 2>&1 | tee "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}
set -e

echo "Exit code: $EXIT_CODE"
echo "::endgroup::"

# -----------------------------------------------------------------------------
# Process results
# -----------------------------------------------------------------------------

if [[ $EXIT_CODE -eq 0 && -d "$TASK_DIR" ]]; then
    # =========================================================================
    # SUCCESS: Task generated and validated
    # =========================================================================
    
    echo "::notice title=Harbor Task Eligible::✅ This PR can become Harbor task: $TASK_ID"
    
    # Set outputs
    echo "eligible=true" >> "$GITHUB_OUTPUT"
    echo "reason=PR passed all eligibility checks and Harbor validation" >> "$GITHUB_OUTPUT"
    echo "task_id=$TASK_ID" >> "$GITHUB_OUTPUT"
    
    # Artifact name for upload
    ARTIFACT_NAME="harbor-task-$TASK_ID"
    echo "artifact_name=$ARTIFACT_NAME" >> "$GITHUB_OUTPUT"
    
    # Write task directory path for artifact upload step
    echo "$TASK_DIR" > /tmp/task_dir_path
    
    # Determine validation status text
    if [[ "$SKIP_VALIDATION" == "true" ]]; then
        VALIDATION_STATUS="⚠️ Skipped (no API key)"
    else
        VALIDATION_STATUS="✅ NOP (reward=0) ✅ Oracle (reward=1)"
    fi
    
    # Read instruction preview
    INSTRUCTION_PREVIEW=""
    if [[ -f "$TASK_DIR/instruction.md" ]]; then
        INSTRUCTION_PREVIEW=$(head -c 500 "$TASK_DIR/instruction.md" | sed 's/`/\\`/g')
    fi
    
    # Create Job Summary
    cat >> "$GITHUB_STEP_SUMMARY" << EOF
# ✅ Harbor Task Eligible

This PR meets all criteria to become a Harbor task for LLM training/evaluation!

## Task Details

| Property | Value |
|----------|-------|
| **Task ID** | \`$TASK_ID\` |
| **Source** | [$REPO#$PR_NUMBER](https://github.com/$REPO/pull/$PR_NUMBER) |
| **Validation** | $VALIDATION_STATUS |

## 📦 Task Artifact Available

The validated Harbor task has been uploaded as a workflow artifact and can be downloaded from this workflow run.

## What This Means

Your PR demonstrates:
- A real-world bug fix or feature
- Proper test coverage (tests fail on buggy baseline, pass with fix)
- Changes substantial enough for LLM training

---

<details>
<summary>📋 Instruction Preview</summary>

\`\`\`markdown
$INSTRUCTION_PREVIEW
\`\`\`

</details>
EOF

else
    # =========================================================================
    # FAILED: Not eligible or validation failed
    # =========================================================================
    
    # Parse error type from output
    OUTPUT=$(cat "$LOG_FILE")
    
    if echo "$OUTPUT" | grep -qi "TrivialPRError\|too trivial\|insufficient\|source file"; then
        REASON="PR does not meet minimum requirements (needs $MIN_SOURCE_FILES-$MAX_SOURCE_FILES source files, must include tests)"
        LEVEL="notice"
        EMOJI="📋"
    elif echo "$OUTPUT" | grep -qi "MissingIssueError\|linked issue"; then
        REASON="PR does not have a linked issue (better instructions come from issue descriptions)"
        LEVEL="notice"
        EMOJI="🔗"
    elif echo "$OUTPUT" | grep -qi "ValidationError\|validation failed\|NOP\|Oracle"; then
        REASON="PR structure is valid but Harbor validation failed (tests may not properly fail/pass)"
        LEVEL="warning"
        EMOJI="⚠️"
    elif echo "$OUTPUT" | grep -qi "timeout\|timed out"; then
        REASON="Task generation timed out (Claude Code took too long)"
        LEVEL="warning"
        EMOJI="⏱️"
    else
        REASON="Task generation failed: check logs for details"
        LEVEL="warning"
        EMOJI="❌"
    fi
    
    echo "::$LEVEL title=Not Harbor Task Eligible::$REASON"
    
    # Set outputs
    echo "eligible=false" >> "$GITHUB_OUTPUT"
    echo "reason=$REASON" >> "$GITHUB_OUTPUT"
    
    # Create Job Summary
    cat >> "$GITHUB_STEP_SUMMARY" << EOF
# $EMOJI Not Harbor Task Eligible

## Reason

$REASON

## Task Requirements

| Requirement | Description |
|-------------|-------------|
| ✅ **Test changes** | PR must include test file modifications |
| ✅ **Multi-file** | Must modify $MIN_SOURCE_FILES-$MAX_SOURCE_FILES source files (excluding tests) |
| ✅ **Substantial** | Not just docs, formatting, or version bumps |
| ✅ **Reversible** | Tests must fail on buggy baseline, pass with fix |

## Common Reasons for Ineligibility

- **Single-file changes** - Harbor tasks need multi-component fixes
- **Documentation only** - No functional code changes
- **Missing tests** - Can't validate without test coverage
- **Trivial fixes** - One-line changes don't make good training data
- **Flaky tests** - Validation requires deterministic pass/fail
- **Large refactors** - Too many files (>$MAX_SOURCE_FILES) makes tasks unwieldy

---

<details>
<summary>📋 Full Output</summary>

\`\`\`
$(tail -c 10000 "$LOG_FILE")
\`\`\`

</details>
EOF

fi

# Always exit 0 - eligibility is informational, not a gate
exit 0

