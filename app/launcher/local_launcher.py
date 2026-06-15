from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def default_output_root() -> Path:
    return Path.home() / "Documents" / "VE2RBXoutput"


if getattr(sys, "frozen", False):
    OSS_ROOT = Path(getattr(sys, "_MEIPASS")).resolve()
    runtime_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "VE2RBX OSS"
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


def open_browser(port: int) -> None:
    time.sleep(0.8)
    webbrowser.open(f"http://{HOST}:{port}")


def main(argv: list[str] | None = None) -> int:
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
