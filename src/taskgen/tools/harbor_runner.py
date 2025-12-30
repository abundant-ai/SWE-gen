from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from harbor.models.environment_type import EnvironmentType
from harbor.models.job.result import JobResult
from harbor.models.trial.result import TrialResult
from harbor.models.trial.paths import TrialPaths


def harbor_cmd_base() -> list[str]:
    """Get the base command to invoke Harbor.
    
    Prefers direct `harbor` binary, falls back to `uv run harbor`.
    """
    if shutil.which("harbor"):
        return ["harbor"]
    if shutil.which("uv"):
        return ["uv", "run", "harbor"]
    return ["python", "-m", "harbor"]


def run_harbor_agent(
    task_id: str,
    dataset_path: Path,
    jobs_dir: Path,
    agent: str,
    timeout_multiplier: Optional[float] = None,
    capture_output: bool = False,
    delete_after: bool = True,
    environment: EnvironmentType = EnvironmentType.DOCKER,
) -> tuple[int, Path | None]:
    """Run a Harbor agent and return (exit_code, job_result_path).
    
    Args:
        task_id: The task identifier
        dataset_path: Path to the Harbor dataset root
        jobs_dir: Parent directory for job artifacts
        agent: Agent type ("nop" or "oracle")
        timeout_multiplier: Optional timeout multiplier for long tasks
        capture_output: If True, suppress stdout/stderr (for rich console usage)
        delete_after: If True, delete Docker images after run (default: True)
                     Set to False to keep images for faster subsequent runs
        environment: Environment type (docker, daytona, e2b, modal, runloop, gke)
    
    Returns:
        Tuple of (exit_code, path_to_result_json or None)
    """
    # Create unique job directory to avoid race conditions
    unique_parent = jobs_dir / f"{task_id}.{agent}.{int(time.time())}"
    unique_parent.mkdir(parents=True, exist_ok=True)
    before = set(p for p in unique_parent.iterdir())

    cmd = harbor_cmd_base() + [
        "run",
        "--agent", agent,
        "-p", str(dataset_path),
        "-t", task_id,
        "--jobs-dir", str(unique_parent),
        "--env", environment.value,
    ]
    if timeout_multiplier is not None:
        cmd += ["--timeout-multiplier", str(timeout_multiplier)]
    
    # Control image deletion: --no-delete keeps images for faster subsequent runs
    if not delete_after:
        cmd.append("--no-delete")

    if capture_output:
        proc = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    else:
        proc = subprocess.run(cmd, check=False)

    # Check if directory still exists after subprocess
    if not unique_parent.exists():
        return proc.returncode, None

    after = set(p for p in unique_parent.iterdir())
    new_dirs = [p for p in (after - before) if p.is_dir()]
    job_dir = sorted(new_dirs, key=lambda p: p.stat().st_mtime, reverse=True)[0] if new_dirs else None
    job_result = (job_dir / "result.json").resolve() if job_dir else None

    return proc.returncode, job_result


def parse_harbor_reward(job_result_path: Path | None) -> int | None:
    """Parse Harbor job result and return reward (0 or 1) using Harbor's typed models.
    
    Uses Harbor's JobResult and TrialResult Pydantic models for type-safe parsing.
    This automatically handles schema changes and provides better error messages.
    
    Args:
        job_result_path: Path to the job-level result.json
    
    Returns:
        Reward value (0 or 1) or None if parsing failed
    """
    if not job_result_path or not job_result_path.exists():
        return None

    try:
        # Use Harbor's JobResult model for type-safe parsing
        job_result = JobResult.model_validate_json(job_result_path.read_text())
        
        # Method 1: Check reward_stats in job stats (fastest)
        if job_result.stats.evals:
            # Get first eval (typically only one for single-task runs)
            first_eval = next(iter(job_result.stats.evals.values()))
            
            # Check reward_stats for "reward" key
            if first_eval.reward_stats and "reward" in first_eval.reward_stats:
                reward_map = first_eval.reward_stats["reward"]
                
                # Check for reward=1 first (oracle success)
                if 1 in reward_map or 1.0 in reward_map:
                    return 1
                # Then check for reward=0 (nop success)
                if 0 in reward_map or 0.0 in reward_map:
                    return 0
        
        # Method 2: Check trial results directly
        for trial_result in job_result.trial_results:
            if trial_result.verifier_result and trial_result.verifier_result.rewards:
                reward_value = trial_result.verifier_result.rewards.get("reward")
                if reward_value is not None:
                    return int(float(reward_value))
        
        # Method 3: Fallback - scan trial directories using TrialPaths
        job_root = job_result_path.parent
        for trial_dir in (p for p in job_root.iterdir() if p.is_dir()):
            try:
                trial_paths = TrialPaths(trial_dir)
                if trial_paths.result_path.exists():
                    trial_result = TrialResult.model_validate_json(
                        trial_paths.result_path.read_text()
                    )
                    if trial_result.verifier_result and trial_result.verifier_result.rewards:
                        reward_value = trial_result.verifier_result.rewards.get("reward")
                        if reward_value is not None:
                            return int(float(reward_value))
            except Exception:
                # Not a valid trial directory, continue searching
                continue
        
    except Exception:
        return None

    return None
