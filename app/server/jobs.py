from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout

from .paths import CONVERTER_ENTRYPOINT, JOB_ROOT, OUTPUT_ROOT, derive_project_name, sanitize_output_name

WARNING_PREFIX = "[VE2RBX_WARNING]"


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
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    error: str | None = None

    def output_dir(self) -> Path | None:
        if self.output_dir_hint.exists():
            return self.output_dir_hint if any(self.output_dir_hint.iterdir()) else None
        if not self.output_root.exists():
            return None
        children = [
            path
            for path in self.output_root.iterdir()
            if path.is_dir()
            and path.name != "_ascii_stage"
            and (path.name == self.asset_name or path.name.startswith(f"{self.asset_name}_"))
        ]
        if not children:
            return None
        children.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return children[0]

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
    def __init__(self) -> None:
        self._jobs: dict[str, LocalJob] = {}
        self._lock = threading.Lock()
        self._converter_lock = threading.Lock()

    def _allocate_output_dir(self, asset_name: str) -> Path:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        for idx in range(1, 10000):
            suffix = "" if idx == 1 else f"_{idx:02d}"
            candidate = OUTPUT_ROOT / f"{asset_name}{suffix}"
            try:
                candidate.mkdir(parents=True, exist_ok=False)
                return candidate
            except FileExistsError:
                continue
        raise RuntimeError(f"could not allocate output folder for {asset_name}")

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status in {"queued", "running"})

    def get(self, job_id: str) -> LocalJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, project_dir: Path, include_animation: bool) -> LocalJob:
        job_id = uuid.uuid4().hex
        job_dir = JOB_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        asset_name = sanitize_output_name(derive_project_name(project_dir))
        output_root = OUTPUT_ROOT
        output_dir_hint = self._allocate_output_dir(asset_name)
        log_path = job_dir / "job.log"

        command = [
            sys.executable,
            "-u",
            str(CONVERTER_ENTRYPOINT),
            str(project_dir),
            "--output-root",
            str(output_root),
            "--work-root",
            str(job_dir),
            "--output-dir",
            str(output_dir_hint),
            "--asset-name",
            asset_name,
        ]
        if not include_animation:
            command.append("--skip-animation")

        job = LocalJob(
            job_id=job_id,
            project_dir=project_dir,
            asset_name=asset_name,
            output_root=output_root,
            output_dir_hint=output_dir_hint,
            log_path=log_path,
            include_animation=include_animation,
            command=command,
        )
        with self._lock:
            self._jobs[job_id] = job

        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job

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
        job.status = "running"
        job.started_at = time.time()
        self._append_log(job, "VE2RBX local conversion job\n")
        self._append_log(job, f"job_id={job.job_id}\n")
        self._append_log(job, f"project_dir={job.project_dir}\n")
        self._append_log(job, f"asset_name={job.asset_name}\n")
        self._append_log(job, f"output_root={job.output_root}\n")
        self._append_log(job, f"output_dir={job.output_dir_hint}\n")
        self._append_log(job, f"include_animation={json.dumps(job.include_animation)}\n")
        self._append_log(job, f"command={' '.join(job.command)}\n\n")

        try:
            if getattr(sys, "frozen", False):
                with self._converter_lock:
                    self._run_in_process(job)
            else:
                self._run_subprocess(job)
        except Exception as exc:
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
        try:
            process = subprocess.Popen(
                job.command,
                cwd=str(CONVERTER_ENTRYPOINT.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(job, line)
            job.exit_code = process.wait()
            job.status = self._final_status(job)
        except Exception:
            raise

    def _run_in_process(self, job: LocalJob) -> None:
        argv = [
            str(CONVERTER_ENTRYPOINT),
            str(job.project_dir),
            "--output-root",
            str(job.output_root),
            "--work-root",
            str(job.log_path.parent),
            "--output-dir",
            str(job.output_dir_hint),
            "--asset-name",
            job.asset_name,
        ]
        if not job.include_animation:
            argv.append("--skip-animation")

        old_argv = sys.argv[:]
        old_cwd = Path.cwd()
        old_path = sys.path[:]
        sys.argv = argv
        sys.path.insert(0, str(CONVERTER_ENTRYPOINT.parent))
        os.chdir(CONVERTER_ENTRYPOINT.parent)
        try:
            with job.log_path.open("a", encoding="utf-8", errors="replace") as log:
                with redirect_stdout(log), redirect_stderr(log):
                    try:
                        runpy.run_path(str(CONVERTER_ENTRYPOINT), run_name="__main__")
                        job.exit_code = 0
                    except SystemExit as exc:
                        code = exc.code
                        job.exit_code = int(code) if isinstance(code, int) else 1
        finally:
            sys.argv = old_argv
            sys.path = old_path
            os.chdir(old_cwd)
        job.status = self._final_status(job)

    def open_output(self, job_id: str) -> Path:
        job = self.get(job_id)
        if not job:
            raise KeyError(job_id)

        target = job.output_dir() or job.output_root
        target.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return target


jobs = JobManager()
