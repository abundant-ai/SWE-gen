"""Static task-policy checks.

Currently enforces the *text-only assets* policy:

    Task instructions must be plain text so the task is solvable by a
    non-multimodal model. instruction.md must NOT embed or link images,
    diagrams, screenshots, PDFs, or other binary/visual assets that the agent
    would have to *see* to solve the task — everything it needs must be conveyed
    as text.

Note: test files and fixtures are intentionally NOT checked — they are held out
and never shown to the agent, so a binary test fixture (e.g. a PNG the code
parses) does not make the task multimodal.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

# File extensions that require visual/binary interpretation (not plain text).
# SVG is technically text but encodes a diagram, so it's treated as visual.
_VISUAL_EXTS = (
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "ico", "svg",
    "pdf", "mp4", "mov", "avi", "webm", "mkv", "psd", "sketch", "fig",
)

_EXT_ALT = "|".join(_VISUAL_EXTS)

# (compiled regex, label). Matched against the raw instruction text.
_TEXT_ONLY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Markdown image embed: ![alt](url)
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), "markdown image embed"),
    # HTML <img> / <picture> / <svg> / <video> / <embed> / <object>
    (re.compile(r"<\s*(img|picture|svg|video|embed|object)\b", re.IGNORECASE), "HTML visual element"),
    # data: URI carrying an embedded image, video, or PDF payload. Note: application/* is
    # restricted to PDF so text MIME types (application/json, application/xml, ...) don't match.
    (re.compile(r"data:(?:image/[^;,\s]+|video/[^;,\s]+|application/pdf);base64", re.IGNORECASE),
     "embedded image/video/pdf data URI"),
    # Markdown link whose target is a visual/binary asset: [txt](foo.png)
    (re.compile(rf"\]\(\s*[^)\s]+\.(?:{_EXT_ALT})\b", re.IGNORECASE), "link to a visual/binary asset"),
    # Bare URL ending in a visual/binary asset extension
    (re.compile(rf"https?://\S+\.(?:{_EXT_ALT})\b", re.IGNORECASE), "URL to a visual/binary asset"),
]


@dataclass(frozen=True)
class TextOnlyViolation:
    """A single non-text reference found in instruction.md."""

    line_number: int
    line: str
    label: str

    def __str__(self) -> str:
        return f"  line {self.line_number}: {self.label}  ->  {self.line.strip()}"


def scan_instruction_text(text: str) -> list[TextOnlyViolation]:
    """Scan instruction text for non-text (visual/binary) references.

    Pure function over the instruction contents so it is easy to unit-test.
    """
    violations: list[TextOnlyViolation] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        for pattern, label in _TEXT_ONLY_PATTERNS:
            if pattern.search(line):
                violations.append(TextOnlyViolation(i, line, label))
                break  # one violation per line is enough
    return violations


def find_instruction_text_violations(task_dir: Path) -> list[TextOnlyViolation]:
    """Scan a task's ``instruction.md`` for text-only-policy violations.

    Returns an empty list if the file does not exist or is clean.
    """
    instruction = Path(task_dir) / "instruction.md"
    if not instruction.exists():
        return []
    text = instruction.read_text(encoding="utf-8", errors="ignore")
    return scan_instruction_text(text)


def format_text_only_violations(task_dir: Path, violations: list[TextOnlyViolation]) -> str:
    """Human-readable multi-line message describing violations."""
    header = (
        f"instruction.md in '{Path(task_dir).name}' references images/diagrams/PDFs or other "
        "visual/binary assets. This violates the text-only policy: instructions must be plain "
        "text solvable by a non-multimodal model — convey all needed information as text."
    )
    body = "\n".join(str(v) for v in violations)
    return f"{header}\n{body}"


def _main(argv: list[str]) -> int:
    """CLI/pre-commit entry: scan task dir(s) or a dataset root; exit 1 on violations."""
    if not argv:
        print("usage: python -m swegen.tools.policy <task_dir | dataset_dir> ...", file=sys.stderr)
        return 2

    task_dirs: list[Path] = []
    for arg in argv:
        p = Path(arg)
        if (p / "instruction.md").exists():
            task_dirs.append(p)
        elif p.is_dir():
            task_dirs.extend(
                d for d in sorted(p.iterdir()) if (d / "instruction.md").exists()
            )

    # No resolvable tasks almost always means a wrong/empty path argument; exit non-zero
    # (distinct from the violation code) so pre-commit/CI don't treat it as a passing scan.
    if not task_dirs:
        print(
            "policy: no tasks with instruction.md found in given paths (check the path)",
            file=sys.stderr,
        )
        return 2

    had_violation = False
    for task_dir in task_dirs:
        violations = find_instruction_text_violations(task_dir)
        if violations:
            had_violation = True
            print(format_text_only_violations(task_dir, violations), file=sys.stderr)
    return 1 if had_violation else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
