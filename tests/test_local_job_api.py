import http.client
import json
import threading

import pytest

from app.server import main
from app.server.jobs import JobConflict, LocalJob


class FakeJobs:
    max_concurrent_jobs = 2

    def __init__(self, job):
        self.job = job

    def list(self):
        return [self.job]

    def get(self, job_id):
        return self.job if job_id == self.job.job_id else None

    def active_count(self):
        return int(self.job.status in {"queued", "running", "cancelling"})

    def cancel(self, job_id):
        if job_id != self.job.job_id:
            raise KeyError(job_id)
        if self.job.status in {"completed", "completed_with_warnings", "failed"}:
            raise JobConflict(f"job is already {self.job.status}")
        self.job.status = "cancelled"
        return self.job

    def open_output(self, job_id):
        if job_id != self.job.job_id:
            raise KeyError(job_id)
        raise JobConflict("completed output is unavailable")


@pytest.fixture
def job(tmp_path):
    job_id = "a" * 32
    job_dir = tmp_path / "jobs" / job_id
    job_dir.mkdir(parents=True)
    return LocalJob(
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
    )


@pytest.fixture
def api(job, monkeypatch):
    monkeypatch.setattr(main, "jobs", FakeJobs(job))
    server = main.VE2RBXHTTPServer(("127.0.0.1", 0), main.LocalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    yield request
    server.shutdown()
    server.stop_monitor()
    server.server_close()
    thread.join(3)


def test_list_jobs_and_config_expose_concurrency_limit(api, job):
    status, payload = api("GET", "/api/local/jobs")
    assert status == 200
    assert payload["max_concurrent_jobs"] == 2
    assert payload["jobs"][0]["job_id"] == job.job_id

    status, payload = api("GET", "/api/local/config")
    assert status == 200
    assert payload["max_concurrent_jobs"] == 2


def test_cancel_requires_token_and_returns_job(api, job):
    status, _payload = api("POST", f"/api/local/jobs/{job.job_id}/cancel")
    assert status == 403

    status, payload = api(
        "POST",
        f"/api/local/jobs/{job.job_id}/cancel",
        headers={main.API_TOKEN_HEADER: main.API_TOKEN},
    )
    assert status == 202
    assert payload["job"]["status"] == "cancelled"


def test_cancel_completed_job_returns_conflict(api, job):
    job.status = "completed"
    status, payload = api(
        "POST",
        f"/api/local/jobs/{job.job_id}/cancel",
        headers={main.API_TOKEN_HEADER: main.API_TOKEN},
    )
    assert status == 409
    assert "completed" in payload["error"]


def test_open_output_conflict_returns_409(api, job):
    status, payload = api(
        "POST",
        f"/api/local/jobs/{job.job_id}/open-output",
        headers={main.API_TOKEN_HEADER: main.API_TOKEN},
    )
    assert status == 409
    assert payload["error"] == "completed output is unavailable"
