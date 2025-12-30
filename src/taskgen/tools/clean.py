from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


@dataclass
class CleanPlan:
    dirs: List[Path]
    files: List[Path]


def _existing_only(paths: Iterable[Path]) -> List[Path]:
    return [p for p in paths if p and p.exists()]


def build_clean_plan(
    state_dir: Path,
    output_root: Path,
    include_ledgers: bool = False,
    include_cache: bool = False,
    include_tasks: bool = False,
    include_leftovers: bool = True,
) -> CleanPlan:
    # Default artifacts under .state
    dirs = [
        state_dir / "harbor-jobs",
        state_dir / "logs",
    ]

    files: List[Path] = []
    if include_ledgers:
        files += [state_dir / "reversal.jsonl"]
    if include_cache:
        dirs.append(state_dir / "cache")

    # Task outputs (optional)
    if include_tasks:
        dirs.append(output_root)
        # Also clean intermediate format if it exists
        dirs.append(output_root / "intermediate")

    # Historical leftovers at repo root
    if include_leftovers:
        # Older job outputs (pre .state routing)
        dirs.append(Path("jobs"))
        dirs.append(Path("runs"))

    # Filter to existing only
    return CleanPlan(dirs=_existing_only(dirs), files=_existing_only(files))


def execute_clean(plan: CleanPlan) -> Tuple[int, int]:
    n_dirs = 0
    n_files = 0
    for d in plan.dirs:
        try:
            shutil.rmtree(d, ignore_errors=True)
            n_dirs += 1
        except Exception:
            pass
    for f in plan.files:
        try:
            f.unlink(missing_ok=True)
            n_files += 1
        except Exception:
            pass
    return n_dirs, n_files


def run_clean(
    state_dir: Path,
    output_root: Path,
    all_: bool,
    ledgers: bool,
    cache: bool,
    tasks: bool,
    dry_run: bool,
    console: Console | None = None,
) -> None:
    """Compute and execute a cleanup plan and print a concise summary."""
    console = console or Console()

    include_ledgers = all_ or ledgers
    include_cache = all_ or cache
    include_tasks = all_ or tasks
    include_leftovers = True

    plan = build_clean_plan(
        state_dir=state_dir,
        output_root=output_root,
        include_ledgers=include_ledgers,
        include_cache=include_cache,
        include_tasks=include_tasks,
        include_leftovers=include_leftovers,
    )

    table = Table(title="Cleanup Plan", title_style="bold cyan", show_lines=False, header_style="bold")
    table.add_column("Type", no_wrap=True)
    table.add_column("Path", overflow="fold")
    for d in plan.dirs:
        table.add_row("dir", str(d))
    for f in plan.files:
        table.add_row("file", str(f))

    if dry_run:
        console.print(Panel(table, title="Dry Run", border_style="cyan"))
        return

    n_dirs, n_files = execute_clean(plan)

    summary = Table(show_header=False, box=None)
    summary.add_row("State dir", str(state_dir))
    summary.add_row("Output root", str(output_root))
    summary.add_row("Removed dirs", str(n_dirs))
    summary.add_row("Removed files", str(n_files))
    console.print(Panel(summary, title="Cleanup Complete", border_style="green"))
