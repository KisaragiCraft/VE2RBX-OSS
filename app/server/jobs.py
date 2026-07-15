from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .paths import JOB_ROOT, LAUNCHER_ENTRYPOINT, OUTPUT_ROOT, UPLOAD_ROOT, derive_project_name, sanitize_output_name

WARNING_PREFIX = "[VE2RBX_WARNING]"
DEFAULT_MAX_CONCURRENT_JOBS = 2
TERMINAL_STATUSES = {"completed", "completed_with_warnings", "failed", "cancelled"}


class JobConflict(RuntimeError):
    pass


class ProcessTreeTerminationError(RuntimeError):
    pass


class UnsafeCleanup(RuntimeError):
    pass


def _resolve_owned_target(target: Path, owner_root: Path) -> Path:
    resolved_target = target.resolve()
    resolved_root = owner_root.resolve()
    if resolved_target == resolved_root or not resolved_target.is_relative_to(resolved_root):
        raise UnsafeCleanup(f"refusing to remove unowned path: {resolved_target}")
    return resolved_target


def remove_owned_tree(target: Path, owner_root: Path) -> None:
    resolved_target = _resolve_owned_target(target, owner_root)
    if resolved_target.exists():
        shutil.rmtree(resolved_target)


def terminate_process_tree(process: subprocess.Popen, timeout: float = 5.0) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ProcessTreeTerminationError(
                f"taskkill failed for PID {process.pid}: {result.stderr.strip()}"
            )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ProcessTreeTerminationError(
                f"worker PID {process.pid} did not exit after taskkill"
            ) from exc
        return

    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        process.wait(timeout=timeout)
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=timeout)
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ProcessTreeTerminationError(
                f"worker PID {process.pid} did not exit after SIGKILL"
            ) from exc
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return
    os.killpg(pgid, signal.SIGKILL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise ProcessTreeTerminationError(f"process group {pgid} is still alive")


@dataclass
class LocalJob:
    job_id: str
    project_dir: Path
    asset_name: str
    output_root: Path
    output_dir_hint: Path
    log_path: Path
    include_animation: bool
    command: list[str]
    job_dir: Path | None = None
    work_root: Path | None = None
    upload_dir: Path | None = None
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    error: str | None = None
    process: subprocess.Popen | None = field(default=None, repr=False, compare=False)
    termination_claimed: bool = field(default=False, repr=False, compare=False)

    def output_dir(self) -> Path | None:
        try:
            return self.output_dir_hint if self.output_dir_hint.is_dir() and any(self.output_dir_hint.iterdir()) else None
        except OSError:
            return None

    def to_dict(self) -> dict:
        output_dir = self.output_dir()
        return {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "include_animation": self.include_animation,
            "asset_name": self.asset_name,
            "project_dir": str(self.project_dir),
            "output_root": str(self.output_root),
            "output_dir_hint": str(self.output_dir_hint),
            "output_dir": str(output_dir) if output_dir else None,
            "log_path": str(self.log_path),
            "error": self.error,
            "has_warnings": self.status == "completed_with_warnings",
        }


class JobManager:
    def __init__(self, max_concurrent_jobs: int | None = None) -> None:
        configured = DEFAULT_MAX_CONCURRENT_JOBS if max_concurrent_jobs is None else max_concurrent_jobs
        if configured not in {1, 2}:
            raise ValueError("max_concurrent_jobs must be 1 or 2")
        self.max_concurrent_jobs = configured
        self._jobs: dict[str, LocalJob] = {}
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=configured, thread_name_prefix="ve2rbx-job")

    def _allocate_output_dir(self, asset_name: str, job_id: str) -> Path:
        candidate = OUTPUT_ROOT / asset_name / job_id
        candidate.mkdir(parents=True, exist_ok=False)
        return candidate

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status in {"queued", "running", "cancelling"})

    def get(self, job_id: str) -> LocalJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[LocalJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def start(
        self,
        project_dir: Path,
        include_animation: bool,
        upload_dir: Path | None = None,
    ) -> LocalJob:
        job_id = uuid.uuid4().hex
        job_dir = JOB_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        work_root = job_dir / "work"
        asset_name = sanitize_output_name(derive_project_name(project_dir))
        output_root = OUTPUT_ROOT
        output_dir_hint = self._allocate_output_dir(asset_name, job_id)
        log_path = job_dir / "job.log"

        job = LocalJob(
            job_id=job_id,
            project_dir=project_dir,
            asset_name=asset_name,
            output_root=output_root,
            output_dir_hint=output_dir_hint,
            log_path=log_path,
            include_animation=include_animation,
            command=[],
            job_dir=job_dir,
            work_root=work_root,
            upload_dir=upload_dir,
        )
        job.command = self._build_command(job)
        with self._lock:
            self._jobs[job_id] = job
            self._futures[job_id] = self._executor.submit(self._run, job)
        return job

    def _build_command(self, job: LocalJob) -> list[str]:
        if job.work_root is None:
            raise ValueError("job work_root is required")
        command = [sys.executable]
        if getattr(sys, "frozen", False):
            command.append("--worker-convert")
        else:
            command.extend(["-u", str(LAUNCHER_ENTRYPOINT), "--worker-convert"])
        command.extend(
            [
                "--worker-log",
                str(job.log_path),
                str(job.project_dir),
                "--output-root",
                str(job.output_root),
                "--work-root",
                str(job.work_root),
                "--output-dir",
                str(job.output_dir_hint),
                "--asset-name",
                job.asset_name,
            ]
        )
        if not job.include_animation:
            command.append("--skip-animation")
        return command

    def _append_log(self, job: LocalJob, message: str) -> None:
        with job.log_path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(message)

    def _job_has_warnings(self, job: LocalJob) -> bool:
        try:
            return WARNING_PREFIX in job.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    def _final_status(self, job: LocalJob) -> str:
        if job.exit_code != 0:
            return "failed"
        return "completed_with_warnings" if self._job_has_warnings(job) else "completed"

    def _run(self, job: LocalJob) -> None:
        with self._lock:
            cancel_before_start = job.status == "cancelling"
            if cancel_before_start:
                job.termination_claimed = True
            else:
                job.status = "running"
                job.started_at = time.time()
        if cancel_before_start:
            self._finish_cancelled(job, termination_confirmed=True)
            job.finished_at = time.time()
            return
        self._append_log(job, "VE2RBX local conversion job\n")
        self._append_log(job, f"job_id={job.job_id}\n")
        self._append_log(job, f"project_dir={job.project_dir}\n")
        self._append_log(job, f"asset_name={job.asset_name}\n")
        self._append_log(job, f"output_root={job.output_root}\n")
        self._append_log(job, f"output_dir={job.output_dir_hint}\n")
        self._append_log(job, f"include_animation={json.dumps(job.include_animation)}\n")
        self._append_log(job, f"command={' '.join(job.command)}\n\n")

        try:
            self._run_subprocess(job)
        except Exception as exc:
            with self._lock:
                if job.status not in TERMINAL_STATUSES:
                    job.status = "failed"
                    job.error = str(exc)
            self._append_log(job, f"\nServer-side launch error: {exc}\n")
        finally:
            if job.status == "failed":
                self._remove_empty_output_dir(job)
            job.finished_at = time.time()

    def _remove_empty_output_dir(self, job: LocalJob) -> None:
        try:
            target = job.output_dir_hint.resolve()
            output_root = job.output_root.resolve()
            if not target.is_relative_to(output_root) or not target.is_dir():
                return
            target.rmdir()
        except OSError:
            return

    def _run_subprocess(self, job: LocalJob) -> None:
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(
            job.command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )
        with self._lock:
            job.process = process
            terminate_here = job.status == "cancelling" and not job.termination_claimed
            if terminate_here:
                job.termination_claimed = True
        if terminate_here:
            try:
                terminate_process_tree(process)
            except ProcessTreeTerminationError as exc:
                self._fail_cancellation(job, exc)
            else:
                with self._lock:
                    job.process = None
                self._finish_cancelled(job, termination_confirmed=True)
            return
        job.exit_code = process.wait()
        with self._lock:
            job.process = None
            if job.status == "running":
                job.status = self._final_status(job)

    def _fail_cancellation(self, job: LocalJob, exc: Exception) -> None:
        with self._lock:
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = time.time()
        try:
            self._append_log(job, f"Cancellation failed safely: {exc}\n")
        except OSError:
            pass

    def _finish_cancelled(self, job: LocalJob, *, termination_confirmed: bool) -> None:
        if not termination_confirmed:
            raise ProcessTreeTerminationError("process tree termination was not confirmed")
        try:
            if job.job_dir is None or job.work_root is None:
                raise UnsafeCleanup("job ownership paths are missing")
            owned = [(job.work_root, job.job_dir), (job.output_dir_hint, job.output_root)]
            if job.upload_dir is not None:
                owned.insert(0, (job.upload_dir, UPLOAD_ROOT))
            for target, root in owned:
                _resolve_owned_target(target, root)
            for target, root in owned:
                remove_owned_tree(target, root)
        except (UnsafeCleanup, OSError) as exc:
            self._fail_cancellation(job, exc)
            return
        with self._lock:
            if job.status != "cancelling":
                return
            job.status = "cancelled"
            job.finished_at = time.time()
        try:
            self._append_log(job, "Cancellation completed; owned partial data removed.\n")
        except OSError:
            pass

    def cancel(self, job_id: str) -> LocalJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status == "cancelled":
                return job
            if job.status in TERMINAL_STATUSES:
                raise JobConflict(f"job is already {job.status}")
            job.status = "cancelling"
            future = self._futures[job_id]
            process = job.process
            claimed = False
            if process is not None and not job.termination_claimed:
                job.termination_claimed = True
                claimed = True

        try:
            if future.cancel():
                self._finish_cancelled(job, termination_confirmed=True)
            elif process is not None and claimed:
                terminate_process_tree(process)
                with self._lock:
                    job.process = None
                self._finish_cancelled(job, termination_confirmed=True)
        except ProcessTreeTerminationError as exc:
            self._fail_cancellation(job, exc)
        return job

    def open_output(self, job_id: str) -> Path:
        job = self.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job.status not in {"completed", "completed_with_warnings"}:
            raise JobConflict(f"job is {job.status}; completed output is unavailable")
        target = job.output_dir()
        if target is None:
            raise JobConflict("completed output is unavailable")
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return target


jobs = JobManager()
