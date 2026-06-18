from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import uuid
from email import policy
from email.parser import BytesParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .jobs import jobs
from .paths import (
    CONVERTER_ENTRYPOINT,
    OUTPUT_ROOT,
    PROJECT_EXTENSIONS,
    STATIC_ROOT,
    UPLOAD_ROOT,
    UPLOAD_MAX_BYTES,
    UPLOAD_MAX_FILES,
    UploadError,
    detect_project_root,
    ensure_runtime_dirs,
    safe_extract_zip,
    safe_write_upload,
)


HOST = "127.0.0.1"
DEFAULT_PORT = 8765
JOB_ROUTE = re.compile(r"^/api/local/jobs/([a-f0-9]{32})(?:/(log|open-output))?$")
API_TOKEN_HEADER = "X-VE2RBX-Token"
API_TOKEN = os.environ.get("VE2RBX_OSS_API_TOKEN") or secrets.token_urlsafe(32)
SHUTDOWN_GRACE_SECONDS = 5.0


def _server_log(message: str) -> None:
    stream = getattr(sys, "stderr", None)
    if stream:
        stream.write(message + "\n")


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _parse_content_disposition(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split(";"):
        item = part.strip()
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        result[key.lower()] = raw.strip().strip('"')
    return result


def _parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    match = re.search(r"boundary=(?P<boundary>[^;]+)", content_type)
    if not match:
        raise UploadError("missing multipart boundary")

    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if not message.is_multipart():
        raise UploadError("invalid multipart body")

    fields: dict[str, str] = {}
    files: list[tuple[str, bytes]] = []

    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        field_name = part.get_param("name", header="content-disposition") or ""
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            text = part.get_content()
            payload = text.encode(part.get_content_charset() or "utf-8") if isinstance(text, str) else bytes(text)
        if filename:
            files.append((unquote(filename), payload))
        elif field_name:
            fields[field_name] = payload.decode("utf-8", errors="replace")

    return fields, files


def _has_valid_api_token(headers) -> bool:
    return headers.get(API_TOKEN_HEADER) == API_TOKEN


class VE2RBXHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, request_handler_class):
        super().__init__(server_address, request_handler_class)
        self._shutdown_lock = threading.Lock()
        self._shutdown_deadline: float | None = None
        self._shutdown_started = False
        self._monitor_stop = threading.Event()
        self._monitor_thread = threading.Thread(target=self._shutdown_monitor, daemon=True)
        self._monitor_thread.start()

    def cancel_app_shutdown(self) -> None:
        with self._shutdown_lock:
            if not self._shutdown_started:
                self._shutdown_deadline = None

    def request_app_shutdown(self, reason: str) -> None:
        with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
        _server_log(f"VE2RBX local app shutdown requested: {reason}")

    def stop_monitor(self) -> None:
        self._monitor_stop.set()
        self._monitor_thread.join(timeout=1.0)

    def _shutdown_monitor(self) -> None:
        while not self._monitor_stop.wait(0.5):
            with self._shutdown_lock:
                deadline = self._shutdown_deadline
                if self._shutdown_started or deadline is None:
                    continue
                if time.monotonic() < deadline:
                    continue

            if jobs.active_count() > 0:
                with self._shutdown_lock:
                    if not self._shutdown_started:
                        self._shutdown_deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
                continue

            with self._shutdown_lock:
                if self._shutdown_started:
                    return
                self._shutdown_started = True

            _server_log("VE2RBX local app shutting down after browser tab close.")
            threading.Thread(target=self.shutdown, daemon=True).start()
            return


class LocalHandler(SimpleHTTPRequestHandler):
    server_version = "VE2RBXLocal/0.1"

    def log_message(self, format: str, *args: object) -> None:
        _server_log("[%s] %s" % (self.log_date_time_string(), format % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: object) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"ok": False, "error": message})

    def _require_api_token(self) -> bool:
        if _has_valid_api_token(self.headers):
            return True
        self._send_error_json(403, "invalid local API token")
        return False

    def _has_query_or_header_api_token(self, parsed) -> bool:
        query_token = parse_qs(parsed.query).get("token", [""])[0]
        return _has_valid_api_token(self.headers) or query_token == API_TOKEN

    def _discard_request_body(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length > 0:
            self.rfile.read(content_length)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/local/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "mode": "local",
                    "converter_exists": CONVERTER_ENTRYPOINT.exists(),
                    "converter": str(CONVERTER_ENTRYPOINT),
                    "active_jobs": jobs.active_count(),
                    "time": time.time(),
                },
            )
            return

        if path == "/api/local/config":
            self._send_json(
                200,
                {
                    "ok": True,
                    "output_root": str(OUTPUT_ROOT),
                    "converter_entrypoint": str(CONVERTER_ENTRYPOINT),
                    "project_extensions": sorted(PROJECT_EXTENSIONS),
                    "supports_zip": True,
                    "supports_folder_upload": True,
                    "api_token": API_TOKEN,
                },
            )
            return

        job_match = JOB_ROUTE.match(path)
        if job_match:
            job_id, action = job_match.groups()
            job = jobs.get(job_id)
            if not job:
                self._send_error_json(404, "job not found")
                return
            if action == "log":
                if job.log_path.exists():
                    body = job.log_path.read_bytes()
                else:
                    body = b""
                self._send(200, body, "text/plain; charset=utf-8")
                return
            if action == "open-output":
                self._send_error_json(405, "POST is required")
                return
            self._send_json(200, {"ok": True, "job": job.to_dict()})
            return

        self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/local/session/open":
            self._discard_request_body()
            if not self._require_api_token():
                return
            if hasattr(self.server, "cancel_app_shutdown"):
                self.server.cancel_app_shutdown()
            self._send_json(200, {"ok": True})
            return

        if path == "/api/local/session/close":
            self._discard_request_body()
            if not self._has_query_or_header_api_token(parsed):
                self._send_error_json(403, "invalid local API token")
                return
            if hasattr(self.server, "request_app_shutdown"):
                self.server.request_app_shutdown("browser session closed")
            self._send_json(202, {"ok": True})
            return

        if path == "/api/local/convert":
            if not self._require_api_token():
                return
            self._handle_convert()
            return

        job_match = JOB_ROUTE.match(path)
        if job_match and job_match.group(2) == "open-output":
            if not self._require_api_token():
                return
            self._open_output(job_match.group(1))
            return

        self._send_error_json(404, "not found")

    def _handle_convert(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._send_error_json(400, "multipart/form-data is required")
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_error_json(400, "invalid content length")
            return
        if content_length <= 0:
            self._send_error_json(400, "empty upload")
            return
        if content_length > UPLOAD_MAX_BYTES:
            self._send_error_json(413, "upload is too large")
            return

        try:
            body = self.rfile.read(content_length)
            fields, files = _parse_multipart(body, content_type)
            if not files:
                raise UploadError("no files were uploaded")
            if len(files) > UPLOAD_MAX_FILES:
                raise UploadError(f"too many uploaded files: {len(files)}")
            total_upload_size = sum(len(content) for _filename, content in files)
            if total_upload_size > UPLOAD_MAX_BYTES:
                raise UploadError("upload is too large")

            upload_dir = UPLOAD_ROOT / uuid.uuid4().hex
            upload_dir.mkdir(parents=True, exist_ok=True)
            source_dir = upload_dir / "source"
            source_dir.mkdir(parents=True, exist_ok=True)

            if len(files) == 1 and files[0][0].lower().endswith(".zip"):
                zip_path = upload_dir / "upload.zip"
                zip_path.write_bytes(files[0][1])
                extracted_dir = upload_dir / "extracted"
                safe_extract_zip(zip_path, extracted_dir)
                project_base = extracted_dir
            else:
                for filename, content in files:
                    safe_write_upload(source_dir, filename, content)
                project_base = source_dir

            project_root = detect_project_root(project_base)
            if not project_root:
                raise UploadError("no complete VoxEdit project was found (.vxr and .vxa are required)")

            include_animation = fields.get("include_animation", "").lower() in {"1", "true", "yes", "on"}
            job = jobs.start(project_root, include_animation)
            self._send_json(202, {"ok": True, "job": job.to_dict()})
        except UploadError as exc:
            self._send_error_json(400, str(exc))
        except Exception as exc:
            self._send_error_json(500, str(exc))

    def _open_output(self, job_id: str) -> None:
        try:
            path = jobs.open_output(job_id)
            self._send_json(200, {"ok": True, "opened": str(path)})
        except KeyError:
            self._send_error_json(404, "job not found")
        except Exception as exc:
            self._send_error_json(500, str(exc))

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
        target = (STATIC_ROOT / relative).resolve()
        static_root = STATIC_ROOT.resolve()
        if not target.is_relative_to(static_root) or not target.is_file():
            target = STATIC_ROOT / "index.html"

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix.lower() in {".html", ".css", ".js"}:
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)


def run(host: str = HOST, port: int = DEFAULT_PORT) -> None:
    ensure_runtime_dirs()
    server = VE2RBXHTTPServer((host, port), LocalHandler)
    _server_log(f"VE2RBX local app: http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.stop_monitor()
        server.server_close()


if __name__ == "__main__":
    selected_port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    run(port=selected_port)
