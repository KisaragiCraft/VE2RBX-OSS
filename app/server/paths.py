from __future__ import annotations

import shutil
import zipfile
import os
import re
from pathlib import Path, PurePosixPath


OSS_ROOT = Path(os.environ.get("VE2RBX_OSS_BUNDLE_ROOT", Path(__file__).resolve().parents[2])).resolve()
DATA_ROOT = Path(os.environ.get("VE2RBX_OSS_RUNTIME_ROOT", OSS_ROOT)).resolve()
APP_ROOT = OSS_ROOT / "app"
STATIC_ROOT = APP_ROOT / "static"
CONVERTER_ENTRYPOINT = OSS_ROOT / "converter" / "modular_runner.py"
RUNTIME_ROOT = DATA_ROOT / "runtime"
UPLOAD_ROOT = RUNTIME_ROOT / "uploads"
JOB_ROOT = RUNTIME_ROOT / "jobs"
DEFAULT_OUTPUT_ROOT = (Path.home() / "Documents" / "VE2RBXoutput").resolve()
OUTPUT_ROOT = Path(os.environ.get("VE2RBX_OSS_OUTPUT_ROOT", DEFAULT_OUTPUT_ROOT)).resolve()

PROJECT_EXTENSIONS = {".vxr", ".vxa"}
REQUIRED_PROJECT_EXTENSIONS = {".vxr", ".vxa"}
ZIP_MAX_FILES = 5000
ZIP_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_MAX_FILES = ZIP_MAX_FILES
UPLOAD_MAX_BYTES = 512 * 1024 * 1024


class UploadError(ValueError):
    """Raised when an upload cannot be safely materialized."""


def ensure_runtime_dirs() -> None:
    for path in (UPLOAD_ROOT, JOB_ROOT, OUTPUT_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def sanitize_output_name(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    safe = safe.rstrip(". ")
    if not safe:
        safe = "Unnamed"
    if len(safe) > 80:
        safe = safe[:80].rstrip(". ")
    return safe or "Unnamed"


def derive_project_name(project_dir: Path) -> str:
    project_dir = project_dir.resolve()
    temp_upload_root = (
        project_dir.name.lower() in {"source", "extracted"}
        and project_dir.parent.parent.resolve() == UPLOAD_ROOT.resolve()
    )
    if not temp_upload_root:
        return project_dir.name

    priority = {".vxr": 0, ".vxa": 1, ".vxm": 2}
    candidates = [
        (priority[path.suffix.lower()], path.name.lower(), path.stem)
        for path in project_dir.iterdir()
        if path.is_file() and path.suffix.lower() in PROJECT_EXTENSIONS
    ]
    if not candidates:
        return project_dir.name
    candidates.sort()
    return candidates[0][2]


def has_required_project_files(project_dir: Path) -> bool:
    present = {path.suffix.lower() for path in project_dir.iterdir() if path.is_file()}
    return REQUIRED_PROJECT_EXTENSIONS.issubset(present)


def safe_relative_path(raw_name: str) -> Path:
    name = (raw_name or "").replace("\\", "/").strip()
    if not name:
        raise UploadError("empty file name")

    posix = PurePosixPath(name)
    if posix.is_absolute() or ".." in posix.parts:
        raise UploadError(f"unsafe path: {raw_name}")

    safe_parts = [part for part in posix.parts if part not in ("", ".")]
    if not safe_parts:
        raise UploadError("empty file name")
    if any(":" in part for part in safe_parts):
        raise UploadError(f"unsafe path: {raw_name}")
    return Path(*safe_parts)


def safe_write_upload(base_dir: Path, raw_name: str, content: bytes) -> Path:
    relative = safe_relative_path(raw_name)
    target = (base_dir / relative).resolve()
    base = base_dir.resolve()
    if not target.is_relative_to(base):
        raise UploadError(f"unsafe path: {raw_name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def safe_extract_zip(zip_path: Path, target_dir: Path) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total_size = 0

    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            if len(infos) > ZIP_MAX_FILES:
                raise UploadError(f"ZIP has too many files: {len(infos)}")

            for info in infos:
                if info.is_dir():
                    continue
                total_size += max(info.file_size, 0)
                if total_size > ZIP_MAX_UNCOMPRESSED_BYTES:
                    raise UploadError("ZIP is too large after extraction")

                relative = safe_relative_path(info.filename)
                target = (target_dir / relative).resolve()
                base = target_dir.resolve()
                if not target.is_relative_to(base):
                    raise UploadError(f"unsafe ZIP path: {info.filename}")

                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                extracted.append(target)
    except zipfile.BadZipFile as exc:
        raise UploadError("invalid ZIP file") from exc

    return extracted


def detect_project_root(base_dir: Path) -> Path | None:
    candidates: list[tuple[int, int, str, Path]] = []
    priority = {".vxr": 0, ".vxa": 1, ".vxm": 2}

    for path in base_dir.rglob("*"):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in PROJECT_EXTENSIONS:
            continue
        parent = path.parent.resolve()
        if not has_required_project_files(parent):
            continue
        try:
            depth = len(parent.relative_to(base_dir.resolve()).parts)
        except ValueError:
            depth = 999
        candidates.append((priority[ext], depth, path.name.lower(), parent))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    best_priority = candidates[0][0]
    best_dirs = sorted({item[3] for item in candidates if item[0] == best_priority})
    reduced: list[Path] = []
    for candidate in best_dirs:
        if not any(other != candidate and other in candidate.parents for other in best_dirs):
            reduced.append(candidate)
    if len(reduced) > 1:
        relative = []
        for path in reduced[:5]:
            try:
                relative.append(str(path.relative_to(base_dir.resolve())))
            except ValueError:
                relative.append(str(path))
        raise UploadError("multiple VoxEdit project roots found: " + ", ".join(relative))
    return reduced[0]
