"""Static task-policy checks.

Currently enforces the *offline tests* policy:

    tests/test.sh must NOT install dependencies or access the network.
    All dependencies, tools, and builds belong in the Dockerfile, where the
    internet is available at build time. The verifier/test container runs with
    no network (task.toml sets [environment].allow_internet = false), so any
    live install in test.sh would fail at runtime anyway. This check catches it
    statically at generation/validation time with a clear message.

The check is intentionally conservative to avoid false positives on legitimate
test runners (``bundle exec rspec``, ``go test``, ``cargo test``, ``npx jest``,
``mvn test`` …): it flags explicit installers and network-fetch commands only,
and exempts lines that opt into a clearly-offline mode (``--no-index`` /
``--offline``).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Tokens that make an otherwise-flagged install run without network access.
# If present on a line, the line is not considered a violation.
_OFFLINE_MARKERS = ("--no-index", "--offline")

# (compiled regex, human-readable label). Patterns match against a shell line
# with inline comments stripped. Kept deliberately narrow: the *verb* must be an
# install/fetch, so test *runners* (go test, cargo test, mvn test, npx <runner>,
# bundle exec) are not matched.
_VIOLATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\b"), "apt install"),
    (re.compile(r"\badd-apt-repository\b"), "add-apt-repository"),
    (re.compile(r"\bapk\s+add\b"), "apk add"),
    (re.compile(r"\byum\s+install\b"), "yum install"),
    (re.compile(r"\bdnf\s+install\b"), "dnf install"),
    (re.compile(r"\bpip[0-9.]*\s+install\b"), "pip install"),
    (re.compile(r"\bpython[0-9.]*\s+-m\s+pip\s+install\b"), "python -m pip install"),
    (re.compile(r"\bpip[0-9.]*\s+download\b"), "pip download"),
    (re.compile(r"\buv\s+pip\s+install\b"), "uv pip install"),
    (re.compile(r"\buv\s+(?:add|sync)\b"), "uv add/sync"),
    (re.compile(r"\bpoetry\s+(?:add|install|lock)\b"), "poetry add/install"),
    (re.compile(r"\bpipenv\s+(?:install|sync)\b"), "pipenv install"),
    (re.compile(r"\b(?:conda|mamba)\s+install\b"), "conda install"),
    (re.compile(r"\bnpm\s+(?:i|ci|install|add)\b"), "npm install"),
    (re.compile(r"\byarn\s+(?:add|install)\b"), "yarn add/install"),
    (re.compile(r"\bpnpm\s+(?:i|add|install)\b"), "pnpm install"),
    (re.compile(r"\bbun\s+(?:add|install)\b"), "bun add/install"),
    (re.compile(r"\bcorepack\s+prepare\b"), "corepack prepare (downloads)"),
    (re.compile(r"\bgo\s+(?:get|install)\b"), "go get/install"),
    (re.compile(r"\bgo\s+mod\s+download\b"), "go mod download"),
    (re.compile(r"\bcargo\s+(?:fetch|install|add|update)\b"), "cargo fetch/install/add"),
    (re.compile(r"\bgem\s+install\b"), "gem install"),
    (re.compile(r"\bbundle\s+(?:install|update)\b"), "bundle install/update"),
    (re.compile(r"\bmvn\b.*(?:dependency:|(?<!-)\binstall\b)"), "maven dependency resolution"),
    (re.compile(r"\bgradle\b.*(?:--refresh-dependencies|\bdependencies\b)"), "gradle dependency resolution"),
    (re.compile(r"\bcurl\b"), "curl (network fetch)"),
    (re.compile(r"\bwget\b"), "wget (network fetch)"),
    (re.compile(r"\bgit\s+(?:clone|fetch|pull)\b"), "git clone/fetch/pull"),
]

_COMMENT_RE = re.compile(r"(?<!\S)#.*$")


@dataclass(frozen=True)
class TestNetworkViolation:
    """A single offending line in tests/test.sh."""

    line_number: int
    line: str
    label: str

    def __str__(self) -> str:
        return f"  line {self.line_number}: {self.label}  ->  {self.line.strip()}"


def _strip_comment(line: str) -> str:
    """Remove an inline shell comment (a ``#`` at line start or after whitespace)."""
    return _COMMENT_RE.sub("", line)


# Shell command separators, used to break a line into individual commands so an
# offline marker (e.g. --no-index) only exempts the command it belongs to.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\|")


def scan_test_script(script_text: str) -> list[TestNetworkViolation]:
    """Scan the text of a test.sh for install/network commands.

    Pure function over the script contents so it is easy to unit-test.
    """
    violations: list[TestNetworkViolation] = []
    for i, raw in enumerate(script_text.splitlines(), start=1):
        code = _strip_comment(raw)
        if not code.strip():
            continue
        # Split into individual command segments so an offline marker only exempts the command
        # it applies to — other install/network commands chained on the same line are still checked.
        for segment in _SEGMENT_SPLIT.split(code):
            seg = segment.strip()
            if not seg:
                continue
            if any(marker in seg for marker in _OFFLINE_MARKERS):
                continue
            for pattern, label in _VIOLATION_PATTERNS:
                if pattern.search(seg):
                    violations.append(TestNetworkViolation(i, seg, label))
                    break  # one violation per segment is enough
    return violations


def find_test_network_violations(task_dir: Path) -> list[TestNetworkViolation]:
    """Scan a task's ``tests/test.sh`` for offline-policy violations.

    Returns an empty list if the file does not exist (nothing to check) or is
    clean.
    """
    test_sh = Path(task_dir) / "tests" / "test.sh"
    if not test_sh.exists():
        return []
    text = test_sh.read_text(encoding="utf-8", errors="ignore")
    return scan_test_script(text)


def format_violations(task_dir: Path, violations: list[TestNetworkViolation]) -> str:
    """Human-readable multi-line message describing violations."""
    header = (
        f"tests/test.sh in '{Path(task_dir).name}' installs dependencies or accesses "
        "the network at test time. This is forbidden by the offline-tests policy: "
        "all dependencies/tools/builds must be in the Dockerfile (build time), and the "
        "verifier runs with no network (allow_internet=false)."
    )
    body = "\n".join(str(v) for v in violations)
    return f"{header}\n{body}"


def _main(argv: list[str]) -> int:
    """CLI/pre-commit entry: scan task dir(s) or a dataset root; exit 1 on violations."""
    if not argv:
        print("usage: python -m swegen.tools.policy <task_dir | dataset_dir> ...", file=sys.stderr)
        return 2

    # Expand each argument: a task dir (has tests/test.sh) is checked directly;
    # otherwise treat it as a dataset root and check each task subdirectory.
    task_dirs: list[Path] = []
    for arg in argv:
        p = Path(arg)
        if (p / "tests" / "test.sh").exists():
            task_dirs.append(p)
        elif p.is_dir():
            task_dirs.extend(
                d for d in sorted(p.iterdir()) if (d / "tests" / "test.sh").exists()
            )

    had_violation = False
    for task_dir in task_dirs:
        violations = find_test_network_violations(task_dir)
        if violations:
            had_violation = True
            print(format_violations(task_dir, violations), file=sys.stderr)
    if not task_dirs:
        print("policy: no tasks with tests/test.sh found in given paths", file=sys.stderr)
    return 1 if had_violation else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
