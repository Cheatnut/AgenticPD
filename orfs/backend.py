# -*- coding: utf-8 -*-
"""orfs.backend — Stage C4: abstract execution backend.

Separates "how to run a command" from "what command to run".  Two backends:

    LocalBackend  — subprocess.Popen (current WSL / single-machine usage)
    SlurmBackend  — sbatch submit / squeue poll / scancel cancel (school server)

Both implement the same ``execute(cmd, cwd, log_path, timeout_s) -> (exit_code, timed_out)``
interface, so ORFS runner code does not need to know which backend is active.
"""

from __future__ import annotations

import abc
import logging
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared result type
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """Immutable result of one command execution."""
    exit_code: int          # -1 if timed out
    timed_out: bool
    job_id: Optional[str] = None   # Slurm job ID (None for local)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class ExecutionBackend(abc.ABC):
    """Abstract command execution backend.

    Subclasses implement ``execute()`` and ``cancel()`` for a specific
    execution environment (local subprocess, Slurm, etc.).
    """

    @abc.abstractmethod
    def execute(
        self, cmd: List[str], cwd: Path, log_path: Path, timeout_s: int,
    ) -> ExecutionResult:
        """Run *cmd* with stdout/stderr appended to *log_path*.

        Must enforce *timeout_s*: if the command does not complete within
        the timeout, terminate it and return ``timed_out=True``.
        """
        ...

    @abc.abstractmethod
    def cancel(self, job_id: str) -> None:
        """Cancel a previously submitted job (no-op for local backend)."""
        ...


# ---------------------------------------------------------------------------
# Local backend (subprocess)
# ---------------------------------------------------------------------------

class LocalBackend(ExecutionBackend):
    """Execute commands via ``subprocess.Popen`` on the local machine.

    Uses ``start_new_session=True`` so that SIGKILL on timeout cleans up
    all child processes (yosys, openroad, etc.) spawned by make.
    """

    def execute(
        self, cmd: List[str], cwd: Path, log_path: Path, timeout_s: int,
    ) -> ExecutionResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fout:
            proc = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=fout, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                returncode = proc.wait(timeout=timeout_s)
                return ExecutionResult(exit_code=returncode, timed_out=False)
            except subprocess.TimeoutExpired:
                log.error(
                    "[backend] local timeout (>%ds), killing pg %d",
                    timeout_s, proc.pid,
                )
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                return ExecutionResult(exit_code=-1, timed_out=True)

    def cancel(self, job_id: str) -> None:
        pass  # local processes are managed inline


# ---------------------------------------------------------------------------
# Slurm backend (stub — full implementation deferred to server deployment)
# ---------------------------------------------------------------------------

class SlurmBackend(ExecutionBackend):
    """Submit jobs via Slurm ``sbatch``, poll via ``squeue``, cancel via ``scancel``.

    Stage C provides the interface and method signatures.  The submit/poll/cancel
    implementations below are stubs that raise NotImplementedError — they will be
    completed once deployed on a Slurm cluster.
    """

    def __init__(self, partition: str = "compute", qos: str = "normal",
                 cpus_per_task: int = 4, mem_mb: int = 8192):
        self.partition = partition
        self.qos = qos
        self.cpus_per_task = cpus_per_task
        self.mem_mb = mem_mb

    def execute(
        self, cmd: List[str], cwd: Path, log_path: Path, timeout_s: int,
    ) -> ExecutionResult:
        """Submit to Slurm and block until completion or timeout.

        Stage C stub: delegates to LocalBackend until Slurm is available.
        """
        # TODO: replace with sbatch submit + squeue poll loop
        raise NotImplementedError(
            "SlurmBackend.execute() is a stub.  "
            "Use LocalBackend for now, or implement sbatch submission."
        )

    def cancel(self, job_id: str) -> None:
        """Cancel a Slurm job by ID."""
        # TODO: subprocess.run(["scancel", job_id], check=False)
        raise NotImplementedError(
            "SlurmBackend.cancel() is a stub."
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_backend(name: str = "local", **kwargs) -> ExecutionBackend:
    """Return an ExecutionBackend by name.

    Args:
        name: "local" or "slurm".
        **kwargs: passed to the backend constructor (e.g. partition, cpus_per_task).
    """
    if name == "local":
        return LocalBackend()
    elif name == "slurm":
        return SlurmBackend(**kwargs)
    else:
        raise ValueError(f"Unknown backend: {name}")
