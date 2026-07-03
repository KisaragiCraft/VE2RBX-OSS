from __future__ import annotations
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

VOXEL_SCALE = 0.1  # 1 voxel = 0.1m

# =========================================================================================
# Environment Detection & Imports
# =========================================================================================
try:
    import bpy
    IN_BLENDER = True
    from mathutils import Matrix, Vector, Euler
except ImportError:
    bpy = None
    Matrix = Vector = Euler = None
    IN_BLENDER = False

# Try importing Pillow for External Normalization
HAS_PIL = False
Image = None
if not IN_BLENDER:
    try:
        from PIL import Image
        HAS_PIL = True
    except ImportError:
        pass

# =========================================================================================
# Shared Globals (for Logging)
# =========================================================================================
LOG_FILE_PATH = None
LOG_FILE_HANDLE = None

def setup_logger(log_path: Path):
    global LOG_FILE_PATH, LOG_FILE_HANDLE
    if not log_path: return
    try:
        LOG_FILE_PATH = log_path
        # Signature
        with open(LOG_FILE_PATH, 'w', encoding='utf-8') as f:
            f.write("Created by KisaragiKoubou\n")
        
        LOG_FILE_HANDLE = open(LOG_FILE_PATH, 'a', encoding='utf-8')
        print(f"[Logger] Log initialized at {LOG_FILE_PATH}")
    except Exception as e:
        print(f"[Logger] Failed to setup file logging: {e}")

def log(msg: str):
    print(f"[OBJ2FBX] {msg}")
    if LOG_FILE_HANDLE:
        try:
            LOG_FILE_HANDLE.write(f"[OBJ2FBX] {msg}\n")
            LOG_FILE_HANDLE.flush()
        except Exception: pass

def parse_ve2rbx_args() -> Tuple[Optional[Path], Optional[Path]]:
    # Extracts (edit_dir, log_path) from sys.argv
    # Handles both Launcher (raw args) and Blender (args after --)
    import sys
    args = sys.argv
    start_idx = 1
    if "--" in args:
        start_idx = args.index("--") + 1
    
    edit_dir = None
    log_path = None
    
    skip_next = False
    for i in range(start_idx, len(args)):
        if skip_next:
            skip_next = False
            continue
        val = args[i]
        if val == "--log_path":
            if i + 1 < len(args):
                log_path = Path(args[i+1])
                skip_next = True
        elif not val.startswith("-"):
             if edit_dir is None:
                 edit_dir = Path(val)
    
    return edit_dir, log_path

def find_edit_dir(script_dir: Path) -> Path:
    # Auto-discovery fallback: Centralized -> Legacy
    # 1. Check Centralized Documents/VE2RBXoutput/Work Data, then legacy VE2RBX.
    try:
        user_docs = Path(os.path.expandvars(r"%USERPROFILE%\Documents"))
        for work_root in (
            user_docs / "VE2RBXoutput" / "Work Data",
            user_docs / "VE2RBX" / "Work Data",
        ):
            if not work_root.exists():
                continue
            # Strategy: Find latest timestamped folder
            # Structure: Work Data/{ProjectName}/{RunName (Timestamp)}/
            all_runs = []
            for project in work_root.iterdir():
                if project.is_dir():
                    for run_folder in project.iterdir():
                        if run_folder.is_dir():
                            # We assume any folder here is a run folder
                            all_runs.append(run_folder)
            
            if all_runs:
                # Sort by Modification Time (Newest First) to ensure cross-project accuracy
                all_runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                latest = all_runs[0]
                log(f"Auto-discovered latest Centralized Edit Dir: {latest}")
                return latest
    except Exception as e:
        log(f"Centralized discovery failed: {e}")

    # 2. Legacy script_dir fallback
    log(f"Searching for VE2RBXedit N folders in {script_dir} ...")
    pattern = re.compile(r"^VE2RBXedit (\d+)$")
    max_n = -1
    best_dir = None

    if script_dir.exists():
        for child in script_dir.iterdir():
            if child.is_dir():
                m = pattern.match(child.name)
                if m:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
                        best_dir = child
    
    if best_dir:
        return best_dir.resolve()
    return script_dir

def _png_crc(chunk_type: bytes, data: bytes) -> int:
    import zlib
    return zlib.crc32(chunk_type + data) & 0xffffffff

def _png_pack_chunk(chunk_type: bytes, data: bytes) -> bytes:
    import struct
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", _png_crc(chunk_type, data))

PALETTE_ENTRY_COUNT = 256

PALETTE_BLOCK_SIZE = 4

PALETTE_CANONICAL_WIDTH = PALETTE_ENTRY_COUNT * PALETTE_BLOCK_SIZE

PALETTE_CANONICAL_HEIGHT = PALETTE_CANONICAL_WIDTH

def write_palette_png_deterministic(dst: Path, rgba_rows: bytes, width: int = PALETTE_CANONICAL_WIDTH, height: int = PALETTE_CANONICAL_HEIGHT):
    import struct
    import zlib

    stride = width * 4
    expected_len = stride * height
    if len(rgba_rows) != expected_len:
        raise ValueError(f"Unexpected RGBA buffer size: {len(rgba_rows)} (expected {expected_len})")

    filtered = bytearray()
    prev_row = bytes(stride)
    for y in range(height):
        row = rgba_rows[y * stride:(y + 1) * stride]
        if y == 0:
            filtered.append(1)  # Sub
            for i, val in enumerate(row):
                left = row[i - 4] if i >= 4 else 0
                filtered.append((val - left) & 0xFF)
        else:
            filtered.append(2)  # Up
            for i, val in enumerate(row):
                filtered.append((val - prev_row[i]) & 0xFF)
        prev_row = row

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(filtered), level=9)

    png = bytearray(b"\x89PNG\r\n\x1a\n")
    png.extend(_png_pack_chunk(b"IHDR", ihdr))
    png.extend(_png_pack_chunk(b"IDAT", idat))
    png.extend(_png_pack_chunk(b"IEND", b""))
    dst.write_bytes(bytes(png))

def ensure_palette_png(src: Path, dst: Path):
    log(f"Normalizing palette via Pillow: {src} -> {dst}")
    if not src.exists():
        raise FileNotFoundError(f"Source palette not found: {src}")

    try:
        if HAS_PIL:
            with Image.open(src) as img:
                img = img.convert("RGBA")
                w, h = img.size
                if (w, h) != (PALETTE_CANONICAL_WIDTH, PALETTE_CANONICAL_HEIGHT):
                     log(f"Warning: Palette size {w}x{h} != {PALETTE_CANONICAL_WIDTH}x{PALETTE_CANONICAL_HEIGHT}. Resizing Nearest.")
                     img = img.resize((PALETTE_CANONICAL_WIDTH, PALETTE_CANONICAL_HEIGHT), resample=Image.NEAREST)
                write_palette_png_deterministic(dst, img.tobytes(), img.size[0], img.size[1])
                log("Palette normalized and saved successfully.")
                return

        if IN_BLENDER:
            temp_img = bpy.data.images.load(str(src), check_existing=False)
            try:
                w, h = int(temp_img.size[0]), int(temp_img.size[1])
                src_pixels = [max(0, min(255, int(round(v * 255.0)))) for v in temp_img.pixels[:]]
                rgba = bytearray(PALETTE_CANONICAL_WIDTH * PALETTE_CANONICAL_HEIGHT * 4)

                def copy_px(src_x: int, src_y: int, dst_x: int, dst_y: int):
                    src_idx = (src_y * w + src_x) * 4
                    dst_idx = (dst_y * PALETTE_CANONICAL_WIDTH + dst_x) * 4
                    rgba[dst_idx:dst_idx+4] = bytes(src_pixels[src_idx:src_idx+4])

                if w == PALETTE_CANONICAL_WIDTH and h == PALETTE_CANONICAL_HEIGHT:
                    rgba[:] = bytes(src_pixels)
                else:
                    log(f"Warning: Palette size {w}x{h} != {PALETTE_CANONICAL_WIDTH}x{PALETTE_CANONICAL_HEIGHT}. Resizing Nearest via bpy.")
                    for y in range(PALETTE_CANONICAL_HEIGHT):
                        src_y = min(h - 1, int(y * h / PALETTE_CANONICAL_HEIGHT))
                        for x in range(PALETTE_CANONICAL_WIDTH):
                            src_x = min(w - 1, int(x * w / PALETTE_CANONICAL_WIDTH))
                            copy_px(src_x, src_y, x, y)

                write_palette_png_deterministic(dst, bytes(rgba), PALETTE_CANONICAL_WIDTH, PALETTE_CANONICAL_HEIGHT)
                log("Palette normalized and saved successfully via bpy.")
                return
            finally:
                bpy.data.images.remove(temp_img)

        raise ImportError("Palette processing requires Pillow or Blender image APIs.")
    except Exception as e:
        log(f"Error processing palette with Pillow: {e}")
        raise e

@dataclass
class NodeInfo:
    name: str
    parent_name: Optional[str]
    vxm_basename: Optional[str] = None
    pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    rot: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    children: List['NodeInfo'] = field(default_factory=list)

def norm_name(s: str) -> str:
    return s.lower().replace("_", "").replace(" ", "")

def normalize_deg(v: float) -> float:
    v = v % 360.0
    if v > 180.0:
        v -= 360.0
    return v

def apply_pivot_origin_from_bbox(obj: bpy.types.Object, pivot_norm: Tuple[float, float, float]):
    if obj.type != 'MESH' or not obj.data:
        return
    if not obj.bound_box:
        log(f"Warning: Object {obj.name} has no bounding box.")
        return

    mw = obj.matrix_world
    bbox_world = [mw @ Vector(b) for b in obj.bound_box]
    
    min_x = min(v.x for v in bbox_world)
    max_x = max(v.x for v in bbox_world)
    min_y = min(v.y for v in bbox_world)
    max_y = max(v.y for v in bbox_world)
    min_z = min(v.z for v in bbox_world)
    max_z = max(v.z for v in bbox_world)
    
    size_x = max_x - min_x
    size_y = max_y - min_y
    size_z = max_z - min_z
    
    nx, ny, nz = pivot_norm
    bx = nx
    by = nz
    bz = ny
    
    pivot_world = Vector((
        min_x + size_x * bx,
        min_y + size_y * by,
        min_z + size_z * bz
    ))
    
    pivot_local = mw.inverted() @ pivot_world
    
    log(f"  [Pivot] {obj.name}: Norm({nx}, {ny}, {nz}) -> BlenderNorm({bx:.2f}, {by:.2f}, {bz:.2f})")

    obj.data.transform(Matrix.Translation(-pivot_local))
    obj.data.update()

def read_vox_size(vox_path: Path) -> Optional[Tuple[int, int, int]]:
    if not vox_path.exists():
        return None
    try:
        data = vox_path.read_bytes()
        idx = data.find(b'SIZE')
        if idx < 0 or idx + 24 > len(data):
            return None
        content_size = struct.unpack_from('<I', data, idx + 4)[0]
        if content_size < 12:
            return None
        return struct.unpack_from('<III', data, idx + 12)
    except Exception as e:
        log(f"Warning: failed to read VOX SIZE from {vox_path.name}: {e}")
        return None

def parse_vox_sizes(vox_dir: Path) -> Dict[str, Tuple[int, int, int]]:
    sizes: Dict[str, Tuple[int, int, int]] = {}
    if not vox_dir.exists():
        log(f"Warning: {vox_dir} not found. VOX size metadata unavailable.")
        return sizes

    for vox_path in vox_dir.glob("*.vox"):
        size = read_vox_size(vox_path)
        if size:
            sizes[vox_path.stem.lower()] = size
    log(f"Loaded VOX sizes for {len(sizes)} parts from {vox_dir}")
    return sizes

def pivot_norm_to_blender_local(
    pivot_norm: Tuple[float, float, float],
    vox_size: Tuple[int, int, int],
) -> Vector:
    # VOX SIZE is written in (X, Z, Y) order from the original VXM canvas.
    vox_size_x, vox_size_y, vox_size_z = vox_size
    vxm_size_x = float(vox_size_x)
    vxm_size_y = float(vox_size_z)
    vxm_size_z = float(vox_size_y)

    nx, ny, nz = pivot_norm
    pivot_vxm_x = nx * vxm_size_x
    pivot_vxm_y = ny * vxm_size_y
    pivot_vxm_z = nz * vxm_size_z

    # vox2obj emits Blender vertices as (x, y, -z) * VOXEL_SCALE in VXM terms.
    return Vector((
        pivot_vxm_x * VOXEL_SCALE,
        pivot_vxm_y * VOXEL_SCALE,
        -pivot_vxm_z * VOXEL_SCALE,
    ))

def apply_pivot_origin(obj: bpy.types.Object, pivot_norm: Tuple[float, float, float], vox_size: Optional[Tuple[int, int, int]]):
    if obj.type != 'MESH' or not obj.data:
        return

    if vox_size:
        pivot_local = pivot_norm_to_blender_local(pivot_norm, vox_size)
        log(
            f"  [PivotCanvas] {obj.name}: Norm{pivot_norm} + VOXSize{vox_size} -> "
            f"Local({pivot_local.x:.6f}, {pivot_local.y:.6f}, {pivot_local.z:.6f})"
        )
        obj.data.transform(Matrix.Translation(-pivot_local))
        obj.data.update()
        return

    log(f"  [PivotCanvas] {obj.name}: VOX size missing, falling back to bbox-based pivot.")
    apply_pivot_origin_from_bbox(obj, pivot_norm)

def ascii_safe_name(text: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    return safe or "export"

def parse_pivot_txt(path: Path) -> Dict[str, Tuple[float, float, float]]:
    log(f"Parsing Pivot.txt: {path}")
    pivots = {}
    if not path.exists():
        log(f"Warning: {path} not found. Pivots will be 0,0,0.")
        return {}
    
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            if ":" not in line: continue
            left, right = line.split(":", 1)
            filename = os.path.basename(left.strip())
            key = os.path.splitext(filename)[0].lower()
            parts = right.strip().split()
            if len(parts) >= 3:
                try:
                    px, py, pz = float(parts[0]), float(parts[1]), float(parts[2])
                    pivots[key] = (px, py, pz)
                except ValueError:
                    log(f"Warning: Failed to parse float in pivot line: {line}")
    return pivots

def select_final_objects(final_objects: List[bpy.types.Object]) -> List[bpy.types.Object]:
    bpy.ops.object.select_all(action='DESELECT')
    selected = []
    for obj in final_objects:
        if obj and obj.name in bpy.data.objects:
            obj.select_set(True)
            selected.append(obj)
    if selected:
        bpy.context.view_layer.objects.active = selected[0]
    return selected

def prepare_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)

def normalize_obj_material_texture_paths(obj_dir: Path) -> None:
    for mtl_path in obj_dir.glob("*.mtl"):
        text = mtl_path.read_text(encoding="utf-8", errors="replace")
        normalized_lines = []
        for line in text.splitlines():
            if line.strip().lower().startswith("map_kd "):
                normalized_lines.append("map_Kd palette.png")
            else:
                normalized_lines.append(line)
        mtl_path.write_text("\n".join(normalized_lines) + "\n", encoding="utf-8")

