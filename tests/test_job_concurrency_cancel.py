import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.server import jobs as jobs_module
from app.server.jobs import (
    JobConflict,
    JobManager,
    ProcessTreeTerminationError,
    UnsafeCleanup,
    remove_owned_tree,
)


class BlockingJobManager(JobManager):
    def __init__(self):
        super().__init__(max_concurrent_jobs=2)
        self.started = []
        self.started_event = threading.Event()
        self.release = threading.Event()

    def _run_subprocess(self, job):
        self.started.append(job.job_id)
        if len(self.started) == 2:
            self.started_event.set()
        self.release.wait(5)
        job.exit_code = 0
        job.status = self._final_status(job)


class HoldingProcessManager(JobManager):
    def __init__(self):
        super().__init__(max_concurrent_jobs=1)
        self.process_ready = threading.Event()

    def _run_subprocess(self, job):
        with self._lock:
            job.process = object()
        self.process_ready.set()
        wait_for(lambda: job.status in {"completed", "completed_with_warnings", "failed", "cancelled"})


def make_project(root: Path, name: str) -> Path:
    project = root / name
    project.mkdir(parents=True)
    (project / f"{name}.vxr").write_bytes(b"x")
    (project / f"{name}.vxa").write_bytes(b"x")
    return project


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached")


def test_remove_owned_tree_deletes_only_descendant(tmp_path):
    root = tmp_path / "runtime"
    owned = root / "jobs" / "job-a" / "Work Data"
    sentinel = root / "sentinel.txt"
    owned.mkdir(parents=True)
    sentinel.write_text("keep", encoding="utf-8")

    remove_owned_tree(owned, root / "jobs")

    assert not owned.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("relative", [".", "..", "../outside"])
def test_remove_owned_tree_rejects_root_or_outside(tmp_path, relative):
    root = tmp_path / "runtime" / "jobs"
    root.mkdir(parents=True)
    target = (root / relative).resolve()
    with pytest.raises(UnsafeCleanup):
        remove_owned_tree(target, root)


def test_only_two_jobs_run_and_third_stays_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(jobs_module, "OUTPUT_ROOT", tmp_path / "output")
    manager = BlockingJobManager()
    created = [manager.start(make_project(tmp_path, f"Asset{i}"), False) for i in range(3)]

    assert manager.started_event.wait(3)
    assert [job.status for job in created].count("running") == 2
    assert [job.status for job in created].count("queued") == 1

    manager.release.set()
    wait_for(lambda: all(job.status == "completed" for job in created))


@pytest.mark.parametrize("value", [0, 3])
def test_concurrency_limit_cannot_leave_one_to_two_range(value):
    with pytest.raises(ValueError):
        JobManager(max_concurrent_jobs=value)


def test_same_name_jobs_have_distinct_owned_output_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(jobs_module, "OUTPUT_ROOT", tmp_path / "output")
    manager = BlockingJobManager()
    first = manager.start(make_project(tmp_path / "first", "Asset"), False)
    second = manager.start(make_project(tmp_path / "second", "Asset"), False)

    assert first.output_dir_hint != second.output_dir_hint
    assert first.job_id in first.output_dir_hint.parts
    assert second.job_id in second.output_dir_hint.parts

    manager.release.set()
    wait_for(lambda: first.status == second.status == "completed")


def configure_runtime_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "JOB_ROOT", tmp_path / "runtime" / "jobs")
    monkeypatch.setattr(jobs_module, "UPLOAD_ROOT", tmp_path / "runtime" / "uploads")
    monkeypatch.setattr(jobs_module, "OUTPUT_ROOT", tmp_path / "output")


def create_owned_partial_data(job):
    assert job.work_root is not None
    (job.work_root / "_ascii_stage").mkdir(parents=True)
    (job.work_root / "_ascii_stage" / "mesh.tmp").write_text("partial", encoding="utf-8")
    (job.work_root / "textures" / "nested").mkdir(parents=True)
    (job.work_root / "textures" / "nested" / "texture.tmp").write_text("partial", encoding="utf-8")
    (job.output_dir_hint / "partial.fbx").write_text("partial", encoding="utf-8")


def test_queued_cancel_never_launches_and_cleans_only_owned_data(tmp_path, monkeypatch):
    configure_runtime_roots(tmp_path, monkeypatch)
    manager = BlockingJobManager()
    running = [
        manager.start(make_project(tmp_path / f"running-{index}", "Asset"), False)
        for index in range(2)
    ]
    assert manager.started_event.wait(3)
    sibling_sentinel = running[0].output_dir_hint / "keep.txt"
    sibling_sentinel.write_text("keep", encoding="utf-8")

    upload_dir = jobs_module.UPLOAD_ROOT / "queued-upload"
    queued = manager.start(make_project(upload_dir, "Asset"), False, upload_dir=upload_dir)
    create_owned_partial_data(queued)

    assert manager.cancel(queued.job_id).status == "cancelled"
    assert queued.job_id not in manager.started
    assert not upload_dir.exists()
    assert not queued.work_root.exists()
    assert not queued.output_dir_hint.exists()
    assert queued.log_path.exists()
    assert sibling_sentinel.read_text(encoding="utf-8") == "keep"
    assert manager.cancel(queued.job_id).status == "cancelled"

    manager.release.set()
    wait_for(lambda: all(job.status == "completed" for job in running))


def test_running_cancel_counts_active_until_termination_then_cleans(tmp_path, monkeypatch):
    configure_runtime_roots(tmp_path, monkeypatch)
    manager = HoldingProcessManager()
    upload_dir = jobs_module.UPLOAD_ROOT / "running-upload"
    job = manager.start(make_project(upload_dir, "Asset"), False, upload_dir=upload_dir)
    assert manager.process_ready.wait(3)
    create_owned_partial_data(job)
    termination_started = threading.Event()
    allow_termination = threading.Event()

    def terminate(_process):
        termination_started.set()
        assert allow_termination.wait(3)

    monkeypatch.setattr(jobs_module, "terminate_process_tree", terminate)
    cancel_thread = threading.Thread(target=manager.cancel, args=(job.job_id,))
    cancel_thread.start()

    assert termination_started.wait(3)
    assert job.status == "cancelling"
    assert manager.active_count() == 1
    allow_termination.set()
    cancel_thread.join(3)

    wait_for(lambda: job.status == "cancelled")
    assert not upload_dir.exists()
    assert not job.work_root.exists()
    assert not job.output_dir_hint.exists()
    assert job.log_path.exists()


def test_termination_failure_marks_failed_and_preserves_owned_data(tmp_path, monkeypatch):
    configure_runtime_roots(tmp_path, monkeypatch)
    manager = HoldingProcessManager()
    upload_dir = jobs_module.UPLOAD_ROOT / "failed-upload"
    job = manager.start(make_project(upload_dir, "Asset"), False, upload_dir=upload_dir)
    assert manager.process_ready.wait(3)
    create_owned_partial_data(job)

    def fail_termination(_process):
        raise ProcessTreeTerminationError("tree still alive")

    monkeypatch.setattr(jobs_module, "terminate_process_tree", fail_termination)
    assert manager.cancel(job.job_id).status == "failed"
    assert upload_dir.exists()
    assert job.work_root.exists()
    assert job.output_dir_hint.exists()
    assert "tree still alive" in job.log_path.read_text(encoding="utf-8")


def test_queued_cleanup_failure_finishes_failed_without_deleting_owned_data(tmp_path, monkeypatch):
    configure_runtime_roots(tmp_path, monkeypatch)
    manager = BlockingJobManager()
    running = [
        manager.start(make_project(tmp_path / f"running-cleanup-{index}", "Asset"), False)
        for index in range(2)
    ]
    assert manager.started_event.wait(3)
    sibling_sentinel = running[0].output_dir_hint / "keep.txt"
    sibling_sentinel.write_text("keep", encoding="utf-8")
    upload_dir = jobs_module.UPLOAD_ROOT / "queued-cleanup-failure"
    queued = manager.start(make_project(upload_dir, "Asset"), False, upload_dir=upload_dir)
    create_owned_partial_data(queued)

    monkeypatch.setattr(
        jobs_module.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup denied")),
    )
    result = manager.cancel(queued.job_id)

    assert result.status == "failed"
    assert result.error == "cleanup denied"
    assert result.finished_at is not None
    assert manager.active_count() == 2
    assert upload_dir.exists()
    assert queued.work_root.exists()
    assert queued.output_dir_hint.exists()
    assert sibling_sentinel.read_text(encoding="utf-8") == "keep"
    assert "cleanup denied" in queued.log_path.read_text(encoding="utf-8")

    manager.release.set()
    wait_for(lambda: all(job.status == "completed" for job in running))


def test_running_cleanup_failure_finishes_failed_without_deleting_owned_data(tmp_path, monkeypatch):
    configure_runtime_roots(tmp_path, monkeypatch)
    manager = HoldingProcessManager()
    upload_dir = jobs_module.UPLOAD_ROOT / "running-cleanup-failure"
    job = manager.start(make_project(upload_dir, "Asset"), False, upload_dir=upload_dir)
    assert manager.process_ready.wait(3)
    create_owned_partial_data(job)
    sibling_sentinel = job.output_root / "Sibling" / ("s" * 32) / "keep.txt"
    sibling_sentinel.parent.mkdir(parents=True)
    sibling_sentinel.write_text("keep", encoding="utf-8")

    monkeypatch.setattr(jobs_module, "terminate_process_tree", lambda _process: None)
    monkeypatch.setattr(
        jobs_module.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup denied")),
    )
    result = manager.cancel(job.job_id)

    assert result.status == "failed"
    assert result.error == "cleanup denied"
    assert result.finished_at is not None
    assert manager.active_count() == 0
    assert upload_dir.exists()
    assert job.work_root.exists()
    assert job.output_dir_hint.exists()
    assert sibling_sentinel.read_text(encoding="utf-8") == "keep"
    assert "cleanup denied" in job.log_path.read_text(encoding="utf-8")


def test_cancelled_same_name_job_never_resolves_sibling_output(tmp_path, monkeypatch):
    configure_runtime_roots(tmp_path, monkeypatch)
    manager = BlockingJobManager()
    running = [
        manager.start(make_project(tmp_path / f"owned-{index}", "Asset"), False)
        for index in range(2)
    ]
    assert manager.started_event.wait(3)
    sibling_output = running[0].output_dir_hint / "finished.fbx"
    sibling_output.write_text("keep", encoding="utf-8")
    cancelled = manager.start(make_project(tmp_path / "cancelled-owned", "Asset"), False)
    create_owned_partial_data(cancelled)

    assert manager.cancel(cancelled.job_id).status == "cancelled"

    assert cancelled.output_dir() is None
    assert running[0].output_dir() == running[0].output_dir_hint
    assert sibling_output.read_text(encoding="utf-8") == "keep"
    manager.release.set()
    wait_for(lambda: all(job.status == "completed" for job in running))


def make_standalone_job(tmp_path, status):
    job_id = "o" * 32
    job_dir = tmp_path / "jobs" / job_id
    job_dir.mkdir(parents=True)
    return jobs_module.LocalJob(
        job_id=job_id,
        project_dir=tmp_path / "project",
        asset_name="Asset",
        output_root=tmp_path / "output",
        output_dir_hint=tmp_path / "output" / "Asset" / job_id,
        log_path=job_dir / "job.log",
        include_animation=False,
        command=[],
        job_dir=job_dir,
        work_root=job_dir / "work",
        status=status,
    )


@pytest.mark.parametrize("status", ["queued", "running", "cancelling", "cancelled", "failed"])
def test_open_output_rejects_non_completed_job(tmp_path, status):
    manager = JobManager(max_concurrent_jobs=1)
    job = make_standalone_job(tmp_path, status)
    with manager._lock:
        manager._jobs[job.job_id] = job

    with pytest.raises(JobConflict):
        manager.open_output(job.job_id)
    assert not job.output_root.exists()


@pytest.mark.parametrize("status", ["completed", "completed_with_warnings"])
def test_open_output_requires_exact_nonempty_job_output(tmp_path, monkeypatch, status):
    manager = JobManager(max_concurrent_jobs=1)
    job = make_standalone_job(tmp_path, status)
    with manager._lock:
        manager._jobs[job.job_id] = job
    opened = []
    if os.name == "nt":
        monkeypatch.setattr(jobs_module.os, "startfile", lambda path: opened.append(Path(path)))
    else:
        monkeypatch.setattr(jobs_module.subprocess, "Popen", lambda command: opened.append(Path(command[1])))

    with pytest.raises(JobConflict):
        manager.open_output(job.job_id)

    job.output_dir_hint.mkdir(parents=True)
    (job.output_dir_hint / "finished.fbx").write_text("done", encoding="utf-8")
    assert manager.open_output(job.job_id) == job.output_dir_hint
    assert opened == [job.output_dir_hint]


@pytest.mark.parametrize("missing", ["job_dir", "work_root"])
def test_cancel_with_missing_ownership_path_finishes_failed_even_if_failure_log_breaks(
    tmp_path, monkeypatch, missing
):
    manager = JobManager(max_concurrent_jobs=1)
    job = make_standalone_job(tmp_path, "queued")
    setattr(job, missing, None)
    with manager._lock:
        manager._jobs[job.job_id] = job
        manager._futures[job.job_id] = jobs_module.Future()
    monkeypatch.setattr(
        manager,
        "_append_log",
        lambda _job, _message: (_ for _ in ()).throw(OSError("log denied")),
    )

    result = manager.cancel(job.job_id)

    assert result.status == "failed"
    assert result.error == "job ownership paths are missing"
    assert result.finished_at is not None
    assert manager.active_count() == 0


def test_success_log_failure_cannot_undo_cancelled_terminal_state(tmp_path, monkeypatch):
    configure_runtime_roots(tmp_path, monkeypatch)
    manager = JobManager(max_concurrent_jobs=1)
    job = make_standalone_job(tmp_path, "queued")
    job.output_dir_hint.mkdir(parents=True)
    create_owned_partial_data(job)
    with manager._lock:
        manager._jobs[job.job_id] = job
        manager._futures[job.job_id] = jobs_module.Future()
    original_append_log = manager._append_log

    def fail_success_log(selected_job, message):
        if message.startswith("Cancellation completed"):
            raise OSError("log denied")
        original_append_log(selected_job, message)

    monkeypatch.setattr(manager, "_append_log", fail_success_log)
    result = manager.cancel(job.job_id)

    assert result.status == "cancelled"
    assert result.finished_at is not None
    assert manager.active_count() == 0
    assert not job.work_root.exists()
    assert not job.output_dir_hint.exists()


def test_completed_job_wins_before_cancel_and_is_not_overwritten(tmp_path, monkeypatch):
    configure_runtime_roots(tmp_path, monkeypatch)
    manager = HoldingProcessManager()
    job = manager.start(make_project(tmp_path / "complete", "Asset"), False)
    assert manager.process_ready.wait(3)
    with manager._lock:
        job.exit_code = 0
        job.status = "completed"

    with pytest.raises(JobConflict):
        manager.cancel(job.job_id)
    assert job.status == "completed"


class FakeProcess:
    pid = 12345

    def __init__(self, wait_error=None):
        self.wait_error = wait_error

    def wait(self, timeout=None):
        if self.wait_error:
            raise self.wait_error
        return 0


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill contract")
def test_windows_termination_rejects_taskkill_failure(monkeypatch):
    result = subprocess.CompletedProcess([], 1, stdout="", stderr="denied")
    monkeypatch.setattr(jobs_module.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(ProcessTreeTerminationError, match="taskkill failed"):
        jobs_module.terminate_process_tree(FakeProcess())


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill contract")
def test_windows_termination_rejects_worker_wait_timeout(monkeypatch):
    result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(jobs_module.subprocess, "run", lambda *args, **kwargs: result)
    timeout = subprocess.TimeoutExpired("worker", 0.01)
    with pytest.raises(ProcessTreeTerminationError, match="did not exit"):
        jobs_module.terminate_process_tree(FakeProcess(timeout))


def pid_is_running(pid):
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        check=False,
        capture_output=True,
        text=True,
    )
    return f'"{pid}"' in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree integration")
def test_windows_termination_removes_real_parent_and_child(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    parent_code = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8');"
        "time.sleep(60)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(child_pid_path)],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        wait_for(child_pid_path.exists)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        jobs_module.terminate_process_tree(parent)
        wait_for(lambda: not pid_is_running(parent.pid) and not pid_is_running(child_pid))
    finally:
        if parent.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(parent.pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )


def launch_ready_posix_tree(tmp_path, ignore_term):
    child_script = tmp_path / "child.py"
    parent_script = tmp_path / "parent.py"
    child_ready = tmp_path / "child.ready"
    tree_ready = tmp_path / "tree.ready"
    child_script.write_text(
        """
import signal
import sys
import time
from pathlib import Path

if sys.argv[2] == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
Path(sys.argv[1]).write_text("ready", encoding="utf-8")
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )
    parent_script.write_text(
        """
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[4] == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[4]])
child_ready = Path(sys.argv[2])
deadline = time.monotonic() + 5
while not child_ready.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
if not child_ready.exists():
    raise SystemExit("child signal handler was not ready")
Path(sys.argv[3]).write_text(
    json.dumps({"parent": os.getpid(), "child": child.pid, "pgid": os.getpgrp()}),
    encoding="utf-8",
)
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )
    mode = "ignore" if ignore_term else "exit"
    process = subprocess.Popen(
        [sys.executable, str(parent_script), str(child_script), str(child_ready), str(tree_ready), mode],
        start_new_session=True,
    )
    wait_for(tree_ready.exists)
    return process, json.loads(tree_ready.read_text(encoding="utf-8"))


def posix_pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def cleanup_posix_group(process, pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group integration")
@pytest.mark.parametrize("ignore_term", [False, True], ids=["sigterm", "sigkill-fallback"])
def test_posix_termination_removes_ready_parent_child_group(tmp_path, ignore_term):
    process, ready = launch_ready_posix_tree(tmp_path, ignore_term)
    try:
        assert ready["parent"] == process.pid
        assert ready["pgid"] == process.pid
        assert posix_pid_exists(ready["parent"])
        assert posix_pid_exists(ready["child"])

        jobs_module.terminate_process_tree(process, timeout=0.2 if ignore_term else 1)

        wait_for(lambda: not posix_pid_exists(ready["parent"]) and not posix_pid_exists(ready["child"]))
        with pytest.raises(ProcessLookupError):
            os.killpg(ready["pgid"], 0)
    finally:
        cleanup_posix_group(process, ready["pgid"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX residual process-group contract")
def test_posix_termination_rejects_residual_process_group(monkeypatch):
    monkeypatch.setattr(jobs_module.os, "getpgid", lambda _pid: 77)
    monkeypatch.setattr(jobs_module.os, "killpg", lambda _pgid, _signal: None)
    with pytest.raises(ProcessTreeTerminationError, match="still alive"):
        jobs_module.terminate_process_tree(FakeProcess(), timeout=0)
