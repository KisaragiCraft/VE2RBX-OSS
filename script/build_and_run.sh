#!/usr/bin/env bash
set -euo pipefail

verify=0
if [[ $# -gt 1 ]]; then
  echo "usage: $0 [--verify]" >&2
  exit 2
elif [[ $# -eq 1 ]]; then
  if [[ "$1" == "--verify" ]]; then
    verify=1
  else
    echo "usage: $0 [--verify]" >&2
    exit 2
  fi
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
build_dir="$repo_root/build/macos"
dist_dir="$repo_root/dist/macos"

case "$build_dir" in "$repo_root"/*) ;; *) echo "unsafe build path: $build_dir" >&2; exit 1 ;; esac
case "$dist_dir" in "$repo_root"/*) ;; *) echo "unsafe dist path: $dist_dir" >&2; exit 1 ;; esac
[[ "$build_dir" != "$repo_root" && "$dist_dir" != "$repo_root" ]]

cd "$repo_root"
rm -rf -- "$build_dir" "$dist_dir"

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --onedir \
  --windowed \
  --name VE2RBX \
  --osx-bundle-identifier com.kisaragicraft.ve2rbx \
  --distpath dist/macos \
  --workpath build/macos \
  --add-data 'app/static:app/static' \
  --add-data 'converter:converter' \
  --hidden-import app.server.main \
  --hidden-import app.server.jobs \
  --hidden-import app.server.paths \
  app/launcher/local_launcher.py

app="$dist_dir/VE2RBX.app"
codesign --force --deep --sign - "$app"
codesign --verify --deep --strict "$app"

app_pid=""
cleanup() {
  if [[ -n "$app_pid" ]] && kill -0 "$app_pid" 2>/dev/null; then
    kill "$app_pid" 2>/dev/null || true
    wait "$app_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ $verify -eq 1 ]]; then
  "$app/Contents/MacOS/VE2RBX" --no-browser --port 8765 &
  app_pid=$!
  python3 - <<'PY'
import json
import time
import urllib.error
import urllib.parse
import urllib.request

config_url = "http://127.0.0.1:8765/api/local/config"
deadline = time.monotonic() + 20
payload = None
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(config_url, timeout=1) as response:
            payload = json.load(response)
        break
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        time.sleep(0.2)
if not payload or not payload.get("api_token"):
    raise SystemExit("local app health check did not return api_token")

query = urllib.parse.urlencode({"token": payload["api_token"]})
request = urllib.request.Request(
    f"http://127.0.0.1:8765/api/local/session/close?{query}",
    data=b"",
    method="POST",
)
with urllib.request.urlopen(request, timeout=3) as response:
    if response.status != 202:
        raise SystemExit(f"session close returned HTTP {response.status}")
PY

  for _ in {1..150}; do
    if ! kill -0 "$app_pid" 2>/dev/null; then
      wait "$app_pid"
      app_pid=""
      break
    fi
    sleep 0.1
  done
  if [[ -n "$app_pid" ]]; then
    echo "local app did not exit after session close" >&2
    exit 1
  fi
fi

ditto -c -k --sequesterRsrc --keepParent "$app" "$dist_dir/VE2RBX-macOS.zip"
(
  cd "$dist_dir"
  shasum -a 256 VE2RBX-macOS.zip > VE2RBX-macOS.zip.sha256
  shasum -a 256 -c VE2RBX-macOS.zip.sha256
)
