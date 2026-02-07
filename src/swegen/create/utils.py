from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class CombinedPRTaskEvaluation(BaseModel):
    """Combined evaluation and task generation for a PR.

    First evaluates if PR is substantial, then generates task details if it is.
    """

    is_substantial: bool = Field(
        ..., description="Whether the PR is substantial enough to generate a task"
    )
    reason: str = Field(..., description="Brief explanation of why the PR is or isn't substantial")
    instruction: str | None = Field(
        None,
        description="Concise bug report describing problem, reproduction, expected behavior. No bullet lists or verbose sections.",
    )
    difficulty: str = Field("medium", description="Task difficulty: easy, medium, or hard")
    category: str = Field("bugfix", description="Task category, typically 'bugfix' or 'feature'")
    tags: list[str] = Field(
        default_factory=list,
        description="Exactly 3 tags: [language, tier, framework/category]. Example: ['python', 'backend', 'fastapi']",
    )
    task_name: str | None = Field(
        None,
        description="Optional short task name (1-3 words, dash-separated, lowercase) when requested",
    )


def strip_tests_prefix(path: str) -> str:
    """Strip leading test directory prefix if present.

    Handles common patterns across languages:
    - tests/, test/, __tests__/ (Python, JS/TS)
    - spec/ (Ruby)
    - src/test/ (Java/Kotlin)

    Args:
        path: File path that may start with a test directory prefix

    Returns:
        Path with test directory prefix removed if present
    """
    p = Path(path)
    parts = p.parts

    if not parts:
        return path

    first = parts[0].lower()

    # Python, JS/TS, Ruby
    if first in ("tests", "test", "__tests__", "spec"):
        return str(Path(*parts[1:]))

    # Java/Kotlin: src/test/java/... or src/test/kotlin/...
    if len(parts) >= 2 and parts[0].lower() == "src" and parts[1].lower() == "test":
        return str(Path(*parts[2:]))

    return path


def is_test_file(filename: str) -> bool:
    """Check if a filename represents a test file or test-related resource.

    Supports all languages: Python, JS/TS, Go, Rust, Ruby, Java, C/C++, PHP, C#.

    Args:
        filename: File path (repo-relative)

    Returns:
        True if the file is a test file or test resource (fixtures, data, etc.)
    """
    if not filename:
        return False

    name_lower = filename.lower()
    base_name = filename.split("/")[-1].lower()

    # Check if file is under a test directory (common across languages)
    in_test_dir = (
        # Python/generic
        name_lower.startswith("tests/")
        or "/tests/" in name_lower
        or name_lower.startswith("test/")
        or "/test/" in name_lower
        # JS/TS
        or name_lower.startswith("__tests__/")
        or "/__tests__/" in name_lower
        # Ruby
        or name_lower.startswith("spec/")
        or "/spec/" in name_lower
        # Java/Kotlin (Maven/Gradle convention)
        or "/src/test/" in name_lower
        or name_lower.startswith("src/test/")
    )

    # Python patterns
    is_python_test = (
        base_name.startswith("test_") and name_lower.endswith(".py")
    ) or base_name.endswith("_test.py")

    # JavaScript/TypeScript patterns
    is_js_ts_test = (
        base_name.endswith(".test.js")
        or base_name.endswith(".test.ts")
        or base_name.endswith(".test.jsx")
        or base_name.endswith(".test.tsx")
        or base_name.endswith(".test.mjs")
        or base_name.endswith(".test.cjs")
        or base_name.endswith(".spec.js")
        or base_name.endswith(".spec.ts")
        or base_name.endswith(".spec.jsx")
        or base_name.endswith(".spec.tsx")
        or base_name.endswith(".spec.mjs")
        or base_name.endswith(".spec.cjs")
    )

    # Go patterns
    is_go_test = base_name.endswith("_test.go")

    # Rust patterns
    is_rust_test = base_name.endswith("_test.rs") or base_name == "tests.rs"

    # Ruby patterns
    is_ruby_test = (
        base_name.endswith("_spec.rb")
        or base_name.endswith("_test.rb")
        or base_name.startswith("test_")
        and name_lower.endswith(".rb")
    )

    # Java/Kotlin patterns
    is_java_test = (
        base_name.endswith("test.java")
        or base_name.endswith("tests.java")
        or base_name.endswith("test.kt")
        or base_name.endswith("tests.kt")
        or base_name.startswith("test")
        and (name_lower.endswith(".java") or name_lower.endswith(".kt"))
    )

    # C/C++ patterns
    is_cpp_test = (
        base_name.endswith("_test.cpp")
        or base_name.endswith("_test.cc")
        or base_name.endswith("_test.c")
        or base_name.startswith("test_")
        and name_lower.endswith((".cpp", ".cc", ".c"))
    )

    # PHP patterns
    is_php_test = (
        base_name.endswith("test.php")
        or base_name.startswith("test")
        and name_lower.endswith(".php")
    )

    # C# patterns
    is_csharp_test = base_name.endswith("tests.cs") or base_name.endswith("test.cs")

    return (
        in_test_dir
        or is_python_test
        or is_js_ts_test
        or is_go_test
        or is_rust_test
        or is_ruby_test
        or is_java_test
        or is_cpp_test
        or is_php_test
        or is_csharp_test
    )


def identify_test_files(files: list[dict]) -> list[str]:
    """Identify test files from a list of changed files.

    Supports all languages: Python, JS/TS, Go, Rust, Ruby, Java, C/C++, PHP, C#.

    Args:
        files: List of file dicts with 'filename' key (from GitHub API)

    Returns:
        List of test file paths (repo-relative)
    """
    test_files = []

    for f in files:
        filename = f.get("filename", "")
        if is_test_file(filename):
            test_files.append(filename)

    return test_files


def _is_relevant_source(path: str) -> bool:
    """Check if a file path is relevant for the fix (not tests, CI, or build artifacts).

    NOTE: We include docs, examples, and other non-test files to keep fix.patch
    consistent with bug.patch. This prevents issues where bug.patch reverts docs
    but fix.patch doesn't re-apply them, causing inconsistencies.

    Supports all languages: Python, JS/TS, Go, Rust, Ruby, Java, C/C++, PHP, C#.

    Args:
        path: File path to check

    Returns:
        True if the file should be included in fix.patch
    """
    pl = path.lower()
    base = path.split("/")[-1].lower()

    # === Common exclusions (all languages) ===

    # Exclude test directories
    if pl.startswith("tests/") or "/tests/" in pl:
        return False
    if pl.startswith("test/") or "/test/" in pl:
        return False
    if pl.startswith("__tests__/") or "/__tests__/" in pl:
        return False
    if pl.startswith("spec/") or "/spec/" in pl:  # Ruby
        return False
    if "/src/test/" in pl or pl.startswith("src/test/"):  # Java/Kotlin
        return False

    # Exclude CI and meta (these shouldn't be in fix.patch)
    if pl.startswith(".github/") or "/.github/" in pl:
        return False
    if pl.startswith(".gitlab/") or "/.gitlab/" in pl:
        return False
    if pl.startswith(".circleci/") or "/.circleci/" in pl:
        return False

    # Exclude build outputs and dependency directories (should never be in a PR)
    build_dirs = [
        "node_modules/",
        "dist/",
        "build/",
        ".next/",
        "__pycache__/",
        ".tox/",
        ".pytest_cache/",
        "*.egg-info/",
        "target/",
        "vendor/",
        "bin/",
        "obj/",
        "out/",
    ]
    for bd in build_dirs:
        if bd in pl or pl.startswith(bd.rstrip("/")):
            return False

    # Exclude test files by naming convention (comprehensive, language-agnostic)

    # Python
    if base.startswith("test_") and base.endswith(".py"):
        return False
    if base.endswith("_test.py"):
        return False

    # JavaScript/TypeScript
    if base.endswith((".test.js", ".test.ts", ".test.jsx", ".test.tsx", ".test.mjs", ".test.cjs")):
        return False
    if base.endswith((".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx", ".spec.mjs", ".spec.cjs")):
        return False

    # Go
    if base.endswith("_test.go"):
        return False

    # Rust
    if base.endswith("_test.rs") or base == "tests.rs":
        return False

    # Ruby
    if base.endswith("_spec.rb") or base.endswith("_test.rb"):
        return False
    if base.startswith("test_") and base.endswith(".rb"):
        return False

    # Java/Kotlin
    if base.endswith(("test.java", "tests.java", "test.kt", "tests.kt")):
        return False

    # C/C++
    if base.endswith(("_test.cpp", "_test.cc", "_test.c")):
        return False
    if base.startswith("test_") and base.endswith((".cpp", ".cc", ".c")):
        return False

    # PHP
    if base.endswith("test.php"):
        return False

    # C#
    if base.endswith(("tests.cs", "test.cs")):
        return False

    # Include everything else (source code, docs, examples, type definitions, etc.)
    # This ensures fix.patch is comprehensive and consistent with bug.patch
    return True


def check_multi_file_requirement(
    files: list[dict], min_files: int = 3, max_files: int = 10
) -> tuple[bool, str, int]:
    """Check if PR modifies sufficient source files for a good task.

    Harbor tasks should require changes to 3+ source files (tests don't count).
    Single-file and two-file changes are too easy - agents can pattern-match.
    Large refactors (10+ files) are too complex and often not single bug fixes.

    Args:
        files: List of file dicts with 'filename' key (from GitHub API)
        min_files: Minimum number of source files required (default: 3)
        max_files: Maximum number of source files allowed (default: 10)

    Returns:
        Tuple of (passes, reason, source_count) where:
        - passes: True if source files are within [min_files, max_files] range
        - reason: Explanation if failed
        - source_count: Number of source files found
    """
    source_files = []
    for f in files:
        filename = f.get("filename", "")
        if _is_relevant_source(filename):
            source_files.append(filename)

    count = len(source_files)

    if count < min_files:
        return (
            False,
            f"Only {count} source file{'s' if count != 1 else ''} modified (need {min_files}+, tests excluded)",
            count,
        )

    if count > max_files:
        return (
            False,
            f"Too many source files modified ({count}, max {max_files}) - likely a large refactor (tests excluded)",
            count,
        )

    return True, "", count
