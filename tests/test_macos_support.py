import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "converter"))

from app.launcher import local_launcher
import modular_runner


def test_macos_runtime_root(monkeypatch, tmp_path):
    monkeypatch.setattr(local_launcher.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert local_launcher.default_runtime_root() == (
        tmp_path / "Library" / "Application Support" / "VE2RBX OSS"
    )


def test_macos_blender_standard_path(monkeypatch):
    expected = "/Applications/Blender.app/Contents/MacOS/Blender"
    monkeypatch.setattr(modular_runner.sys, "platform", "darwin")
    monkeypatch.setattr(modular_runner, "load_config", lambda: {})
    monkeypatch.setattr(modular_runner.shutil, "which", lambda _: None)
    monkeypatch.setattr(modular_runner.os.path, "isfile", lambda path: path == expected)
    assert modular_runner.get_blender_exe() == expected


def test_macos_build_script_contract():
    script = (ROOT / "script" / "build_and_run.sh").read_text(encoding="utf-8")
    for required in (
        "set -euo pipefail",
        '[[ "$1" == "--verify" ]]',
        "--onedir",
        "--windowed",
        "--osx-bundle-identifier com.kisaragicraft.ve2rbx",
        "codesign --force --deep --sign -",
        "codesign --verify --deep --strict",
        "/api/local/config",
        "/api/local/session/close",
        "ditto -c -k --sequesterRsrc --keepParent",
        "shasum -a 256 -c VE2RBX-macOS.zip.sha256",
    ):
        assert required in script
