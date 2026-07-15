import subprocess
import sys

import pytest

from app.launcher import local_launcher
from app.server import paths
from app.server.jobs import JobManager, LocalJob


@pytest.fixture
def job(tmp_path):
    job_dir = tmp_path / "jobs" / ("a" * 32)
    return LocalJob(
        job_id="a" * 32,
        project_dir=tmp_path / "project",
        asset_name="Asset",
        output_root=tmp_path / "output",
        output_dir_hint=tmp_path / "output" / "Asset" / ("a" * 32),
        job_dir=job_dir,
        work_root=job_dir / "work",
        upload_dir=tmp_path / "uploads" / "upload-a",
        log_path=job_dir / "job.log",
        include_animation=False,
        command=[],
    )


def test_source_command_uses_launcher_worker(job, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    command = JobManager(max_concurrent_jobs=2)._build_command(job)
    assert command[0] == sys.executable
    assert command[1:3] == ["-u", str(paths.LAUNCHER_ENTRYPOINT)]
    assert command[3] == "--worker-convert"
    assert command[4:6] == ["--worker-log", str(job.log_path)]
    assert command[command.index("--work-root") + 1] == str(job.work_root)
    assert command[command.index("--output-dir") + 1] == str(job.output_dir_hint)


def test_frozen_command_relaunches_same_executable(job, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = JobManager(max_concurrent_jobs=2)._build_command(job)
    assert command[:2] == [sys.executable, "--worker-convert"]
    assert command[2:4] == ["--worker-log", str(job.log_path)]


def test_source_worker_imports_converter_from_required_context(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "Asset.vxr").write_bytes(b"invalid fixture")
    (project / "Asset.vxa").write_bytes(b"invalid fixture")
    log_path = tmp_path / "job.log"
    work_root = tmp_path / "work"
    output_dir = tmp_path / "output" / "Asset" / ("b" * 32)
    command = [
        sys.executable,
        "-u",
        str(paths.LAUNCHER_ENTRYPOINT),
        "--worker-convert",
        "--worker-log",
        str(log_path),
        str(project),
        "--output-root",
        str(tmp_path / "output"),
        "--work-root",
        str(work_root),
        "--output-dir",
        str(output_dir),
        "--asset-name",
        "Asset",
        "--skip-animation",
    ]

    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)

    assert result.returncode != 0
    assert log_path.exists()
    log = log_path.read_text(encoding="utf-8", errors="replace")
    assert "VE2RBX Modular Runner" in log
    assert "ModuleNotFoundError" not in log
    assert "ImportError" not in log


def test_worker_records_unhandled_converter_exception(tmp_path, monkeypatch):
    log_path = tmp_path / "job.log"

    def fail_run(*_args, **_kwargs):
        raise RuntimeError("converter exploded")

    monkeypatch.setattr(local_launcher.runpy, "run_path", fail_run)
    result = local_launcher.run_worker(["--worker-log", str(log_path), "unused-project"])

    assert result == 1
    log = log_path.read_text(encoding="utf-8")
    assert "RuntimeError: converter exploded" in log
