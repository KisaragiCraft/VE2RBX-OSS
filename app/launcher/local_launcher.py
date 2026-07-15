from __future__ import annotations

import argparse
import os
import runpy
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path


def default_output_root() -> Path:
    return Path.home() / "Documents" / "VE2RBXoutput"


def default_runtime_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VE2RBX OSS"
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "VE2RBX OSS"


if getattr(sys, "frozen", False):
    OSS_ROOT = Path(getattr(sys, "_MEIPASS")).resolve()
    runtime_root = default_runtime_root()
    runtime_root.mkdir(parents=True, exist_ok=True)
    os.environ["VE2RBX_OSS_BUNDLE_ROOT"] = str(OSS_ROOT)
    os.environ["VE2RBX_OSS_RUNTIME_ROOT"] = str(runtime_root)
    os.environ.setdefault("VE2RBX_OSS_OUTPUT_ROOT", str(default_output_root()))
else:
    OSS_ROOT = Path(__file__).resolve().parents[2]
    os.environ.setdefault("VE2RBX_OSS_BUNDLE_ROOT", str(OSS_ROOT))
    os.environ.setdefault("VE2RBX_OSS_RUNTIME_ROOT", str(OSS_ROOT))
    os.environ.setdefault("VE2RBX_OSS_OUTPUT_ROOT", str(default_output_root()))
sys.path.insert(0, str(OSS_ROOT))

from app.server.main import DEFAULT_PORT, HOST, run  # noqa: E402

WORKER_FLAG = "--worker-convert"


def open_browser(port: int) -> None:
    time.sleep(0.8)
    webbrowser.open(f"http://{HOST}:{port}")


def run_worker(args: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker-log", required=True)
    worker_options, converter_args = parser.parse_known_args(args)
    old_argv = sys.argv[:]
    old_cwd = Path.cwd()
    old_path = sys.path[:]
    old_stdout, old_stderr = sys.stdout, sys.stderr
    converter_dir = OSS_ROOT / "converter"
    sys.argv = [str(converter_dir / "modular_runner.py"), *converter_args]
    try:
        sys.path.insert(0, str(converter_dir))
        os.chdir(converter_dir)
        with open(worker_options.worker_log, "a", encoding="utf-8", errors="replace") as log:
            sys.stdout = log
            sys.stderr = log
            try:
                runpy.run_path(str(converter_dir / "modular_runner.py"), run_name="__main__")
                return 0
            except SystemExit as exc:
                return int(exc.code) if isinstance(exc.code, int) else 1
            except Exception:
                traceback.print_exc(file=log)
                return 1
    finally:
        sys.argv = old_argv
        sys.path[:] = old_path
        os.chdir(old_cwd)
        sys.stdout, sys.stderr = old_stdout, old_stderr


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == WORKER_FLAG:
        return run_worker(argv[1:])

    parser = argparse.ArgumentParser(description="Launch VE2RBX OSS converter.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    if not args.no_browser:
        threading.Thread(target=open_browser, args=(args.port,), daemon=True).start()
    run(port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
