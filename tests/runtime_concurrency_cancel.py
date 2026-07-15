from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled"}
COMPLETED = {"completed", "completed_with_warnings"}


def multipart_project(project: Path, *, include_animation: bool = False) -> tuple[bytes, str]:
    boundary = f"VE2RBX{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for path in sorted(item for item in project.rglob("*") if item.is_file()):
        name = f"{project.name}/{path.relative_to(project).as_posix()}"
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'.encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="include_animation"\r\n\r\n{str(include_animation).lower()}\r\n'.encode(),
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class LocalAPI:
    def __init__(self, port: int) -> None:
        self.port = port
        self.token = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        expected: tuple[int, ...] = (200,),
        timeout: float = 120,
    ) -> dict:
        headers: dict[str, str] = {}
        if self.token:
            headers["X-VE2RBX-Token"] = self.token
        if content_type:
            headers["Content-Type"] = content_type
        connection = HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        if response.status not in expected:
            raise AssertionError(f"{method} {path}: HTTP {response.status}: {raw.decode(errors='replace')}")
        return json.loads(raw) if raw else {}

    def wait_ready(self, process: subprocess.Popen, timeout: float = 45) -> dict:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"server exited before readiness: {process.returncode}")
            try:
                payload = self.request("GET", "/api/local/config", timeout=2)
                self.token = payload["api_token"]
                return payload
            except (OSError, KeyError, json.JSONDecodeError, AssertionError) as exc:
                last_error = exc
                time.sleep(0.2)
        raise AssertionError(f"server did not become ready: {last_error}")

    def jobs(self) -> dict[str, dict]:
        return {job["job_id"]: job for job in self.request("GET", "/api/local/jobs")["jobs"]}


def wait_jobs(api: LocalAPI, job_ids: list[str], predicate, timeout: float, label: str) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    last: dict[str, dict] = {}
    while time.monotonic() < deadline:
        last = api.jobs()
        if all(job_id in last for job_id in job_ids) and predicate(last):
            return last
        time.sleep(0.15)
    states = {job_id: last.get(job_id, {}).get("status", "missing") for job_id in job_ids}
    raise AssertionError(f"timeout waiting for {label}: {states}")


def upload_owner(project_dir: str) -> Path:
    path = Path(project_dir)
    for candidate in (path, *path.parents):
        if candidate.parent.name.lower() == "uploads":
            return candidate
    raise AssertionError(f"upload owner was not found for {path}")


def windows_processes_for(job_id: str) -> list[dict]:
    if os.name != "nt":
        return []
    script = (
        f"$needle='{job_id}'; "
        "@(Get-CimInstance Win32_Process | Where-Object { "
        "$_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains($needle) "
        "} | Select-Object ProcessId,Name) | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    if not result.stdout.strip():
        return []
    parsed = json.loads(result.stdout)
    return parsed if isinstance(parsed, list) else [parsed]


def terminate_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def run(args: argparse.Namespace) -> dict:
    project = args.project.resolve()
    if not project.is_dir():
        raise AssertionError(f"project does not exist: {project}")
    if not list(project.glob("*.vxr")) or not list(project.glob("*.vxa")):
        raise AssertionError("project must contain .vxr and .vxa files")

    with tempfile.TemporaryDirectory(prefix="ve2rbx-task8-") as temporary:
        temp = Path(temporary)
        environment = os.environ.copy()
        environment["VE2RBX_OSS_OUTPUT_ROOT"] = str(temp / "output")
        environment["VE2RBX_OSS_RUNTIME_ROOT"] = str(temp / "data")
        command = (
            [sys.executable, "-u", str(ROOT / "app" / "launcher" / "local_launcher.py")]
            if args.source
            else [str(args.exe.resolve())]
        )
        command.extend(["--no-browser", "--port", str(args.port)])
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        server_log_path = temp / "server.log"
        started = time.time()
        with server_log_path.open("w", encoding="utf-8") as server_log:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            api = LocalAPI(args.port)
            try:
                api.wait_ready(process)
                body, content_type = multipart_project(project)
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(
                            api.request,
                            "POST",
                            "/api/local/convert",
                            body=body,
                            content_type=content_type,
                            expected=(202,),
                        )
                        for _ in range(2)
                    ]
                    created = [future.result()["job"] for future in futures]
                job_ids = [job["job_id"] for job in created]
                jobs = wait_jobs(
                    api,
                    job_ids,
                    lambda values: all(values[job_id]["status"] == "running" for job_id in job_ids),
                    120,
                    "two running jobs",
                )
                overlap_at = time.time()
                cancelled_id, survivor_id = job_ids
                processes_before = windows_processes_for(cancelled_id)
                if os.name == "nt" and not processes_before:
                    raise AssertionError("cancelled job worker was not visible before cancellation")

                cancelled_before = jobs[cancelled_id]
                owned_paths = {
                    "upload": upload_owner(cancelled_before["project_dir"]),
                    "work": Path(cancelled_before["log_path"]).parent / "work",
                    "output": Path(cancelled_before["output_dir_hint"]),
                }
                api.request(
                    "POST",
                    f"/api/local/jobs/{cancelled_id}/cancel",
                    expected=(202,),
                )
                jobs = wait_jobs(
                    api,
                    job_ids,
                    lambda values: values[cancelled_id]["status"] == "cancelled",
                    45,
                    "cancelled terminal state",
                )
                for name, path in owned_paths.items():
                    if path.exists():
                        raise AssertionError(f"cancelled {name} path still exists: {path}")
                if not Path(jobs[cancelled_id]["log_path"]).is_file():
                    raise AssertionError("cancelled job log was not preserved")

                survivor_status_after_cancel = jobs[survivor_id]["status"]
                if survivor_status_after_cancel != "running":
                    raise AssertionError(
                        "surviving job was not still running when sibling cancellation completed: "
                        f"{survivor_status_after_cancel}"
                    )
                survivor_processes_after_cancel = windows_processes_for(survivor_id)
                if os.name == "nt" and not survivor_processes_after_cancel:
                    raise AssertionError(
                        "surviving job process was not visible when sibling cancellation completed"
                    )

                process_deadline = time.monotonic() + 15
                remaining = windows_processes_for(cancelled_id)
                while remaining and time.monotonic() < process_deadline:
                    time.sleep(0.2)
                    remaining = windows_processes_for(cancelled_id)
                if remaining:
                    raise AssertionError(f"cancelled job processes remain: {remaining}")

                jobs = wait_jobs(
                    api,
                    job_ids,
                    lambda values: values[survivor_id]["status"] in TERMINAL,
                    300,
                    "surviving job completion",
                )
                survivor = jobs[survivor_id]
                if survivor["status"] not in COMPLETED:
                    log = Path(survivor["log_path"]).read_text(encoding="utf-8", errors="replace")
                    raise AssertionError(f"surviving job failed:\n{log[-4000:]}")
                output_dir = Path(survivor["output_dir"])
                outputs = [
                    {"path": str(path.relative_to(output_dir)), "bytes": path.stat().st_size}
                    for path in output_dir.rglob("*")
                    if path.is_file() and path.suffix.lower() in {".fbx", ".glb", ".obj"} and path.stat().st_size > 0
                ]
                if not outputs:
                    raise AssertionError(f"surviving job has no non-empty FBX/GLB/OBJ output: {output_dir}")

                api.request(
                    "POST",
                    f"/api/local/session/close?token={quote(api.token)}",
                    expected=(202,),
                )
                process.wait(timeout=20)
                return {
                    "mode": "source" if args.source else "exe",
                    "job_ids": job_ids,
                    "overlap_at": overlap_at,
                    "cancelled_at": jobs[cancelled_id]["finished_at"],
                    "cancelled_cleanup": {name: str(path) for name, path in owned_paths.items()},
                    "processes_before_cancel": processes_before,
                    "survivor_status_after_cancel": survivor_status_after_cancel,
                    "survivor_processes_after_cancel": survivor_processes_after_cancel,
                    "survivor_status": survivor["status"],
                    "outputs": outputs,
                    "server_exit_code": process.returncode,
                    "duration_seconds": round(time.time() - started, 3),
                }
            except Exception:
                server_log.flush()
                if server_log_path.exists():
                    print(server_log_path.read_text(encoding="utf-8", errors="replace")[-4000:], file=sys.stderr)
                raise
            finally:
                terminate_server(process)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source", action="store_true")
    mode.add_argument("--exe", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))
