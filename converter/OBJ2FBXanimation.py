import sys
import os
import re
import math
import struct
import subprocess
import shutil
import tempfile
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set

# =========================================================================================
# Configuration
# =========================================================================================
# Config
VOXEL_SCALE = 0.1  # 1 voxel = 0.1m
APPLY_PIVOT_WORLD = True   # Enable VXM-pivot-based origin application
KEEP_HIERARCHY_MODE = False # Skip Bake & Join, export full hierarchy
OBJ_IS_PIVOT_CENTERED = False # False = OBJ origin is VoxEdit (0,0,0). True = OBJ origin is Pivot.
OBJ_IS_PIVOT_CENTERED = False # False = OBJ origin is VoxEdit (0,0,0). True = OBJ origin is Pivot.
IMPORT_ROT_AS_BASE = True  # True=Keep Import Rot as Base, False=Bake Import Rot (Not recommended yet)
EXPORT_BASE_NAME = "ve2rbx_output_anim"
OBJ_EXPORT_DIR_NAME = f"{EXPORT_BASE_NAME}_obj"
GLB_EXPORT_FILE_NAME = f"{EXPORT_BASE_NAME}.glb"
RBX_MODEL_FBX_NAME = f"{EXPORT_BASE_NAME}_model.fbx"
DEFAULT_ANIM_FPS = 24
DEFAULT_BONE_LENGTH = 0.25
IMPORT_ROOT_NAME = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", os.environ.get("VE2RBX_ASSET_NAME", "").strip()).rstrip(". ") or "VE2RBX"

# =========================================================================================
# Environment Detection & Imports
# =========================================================================================
try:
    import bpy
    IN_BLENDER = True
    from mathutils import Matrix, Vector, Euler
except ImportError:
    IN_BLENDER = False

# Try importing Pillow for External Normalization
HAS_PIL = False
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
        except: pass

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

# =========================================================================================
# Shared Utils (Available to Launcher & Blender)
# =========================================================================================
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

# =========================================================================================
# PNG Utils (Signature Injection)
# =========================================================================================
PNG_SIGNATURE_KEY = "VE2RBX_CreatedBy"
PNG_SIGNATURE_VAL = "Created by KisaragiKoubou"

def _png_crc(chunk_type: bytes, data: bytes) -> int:
    import zlib
    return zlib.crc32(chunk_type + data) & 0xffffffff

def inject_png_text_chunk(png_path: Path, key: str, value: str):
    # Insert tEXt chunk before IEND. Safe: no pixel changes.
    import struct

    if not png_path.exists():
        raise FileNotFoundError(f"PNG not found: {png_path}")

    b = png_path.read_bytes()
    sig = b[:8]
    if sig != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not a PNG file")

    out = bytearray()
    out += sig

    i = 8
    inserted = False
    text_data = (key + "\x00" + value).encode("latin-1", errors="replace")  # PNG tEXt is ISO-8859-1
    chunk_type = b"tEXt"
    chunk_len = struct.pack(">I", len(text_data))
    chunk_crc = struct.pack(">I", _png_crc(chunk_type, text_data))
    text_chunk = chunk_len + chunk_type + text_data + chunk_crc

    while i < len(b):
        if i + 8 > len(b):
            break
        length = struct.unpack(">I", b[i:i+4])[0]
        ctype = b[i+4:i+8]
        chunk_total = 12 + length
        chunk_bytes = b[i:i+chunk_total]

        if (ctype == b"IEND") and (not inserted):
            out += text_chunk
            inserted = True

        out += chunk_bytes
        i += chunk_total

    if not inserted:
        # If somehow no IEND found, fail hard (should not happen for valid PNG)
        raise ValueError("IEND chunk not found; invalid PNG?")

    png_path.write_bytes(bytes(out))

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

# =========================================================================================
# Launcher Mode (Standard Python)
# =========================================================================================
if not IN_BLENDER:
    print("[OBJ2FBX-Launcher] Environment: Standard Python")
    script_path = Path(__file__).resolve()
    script_dir = script_path.parent
    
    # Parse Args
    target_edit_dir, passed_log_path = parse_ve2rbx_args()
    
    if passed_log_path:
        setup_logger(passed_log_path)
    
    if not target_edit_dir:
        target_edit_dir = find_edit_dir(script_dir)
        if passed_log_path is None:
             # Auto-create log path in edit_dir/log if running standalone
             try:
                 log_dir = target_edit_dir / "log"
                 if target_edit_dir.exists():
                     log_dir.mkdir(parents=True, exist_ok=True)
                     passed_log_path = log_dir / "OBJ2FBX_standalone.log"
                     setup_logger(passed_log_path)
                     log(f"Standalone Log: {passed_log_path}")
             except Exception as e:
                 print(f"Failed to auto-create standalone log: {e}")
    
    log(f"Target Edit Dir: {target_edit_dir}")
    

    try:
        src_palette = target_edit_dir / "OutputOBJ" / "palette.png"
        dst_palette = src_palette # Refinement: Overwrite/Normalize in place
        if src_palette.exists():
            ensure_palette_png(src_palette, dst_palette)
        else:
            log(f"Warning: Source palette {src_palette} not found. Skipping normalization.")
    except Exception as e:
        log(f"Launcher Error during palette prep: {e}")
    
    BLENDER_FOUNDATION_DIR = r"C:\Program Files\Blender Foundation"
    print(f"[OBJ2FBX-Launcher] Searching for Blender in: {BLENDER_FOUNDATION_DIR}")

    if not os.path.isdir(BLENDER_FOUNDATION_DIR):
        print(f"[OBJ2FBX-Launcher] Error: Directory not found: {BLENDER_FOUNDATION_DIR}")
        sys.exit(1)

    version_pattern = re.compile(r"^Blender (\d+\.\d+)$", re.IGNORECASE)
    candidates = []

    for item in os.listdir(BLENDER_FOUNDATION_DIR):
        full_path = os.path.join(BLENDER_FOUNDATION_DIR, item)
        if os.path.isdir(full_path):
            m = version_pattern.match(item)
            if m:
                try:
                    ver_float = float(m.group(1))
                    exe_path = os.path.join(full_path, "blender.exe")
                    if os.path.isfile(exe_path):
                        candidates.append((ver_float, exe_path, item))
                except ValueError:
                    pass
    
    if not candidates:
        print("[OBJ2FBX-Launcher] Error: No valid Blender installations found.")
        sys.exit(1)

    candidates.sort(key=lambda x: x[0], reverse=True)
    selected_ver, selected_exe, selected_name = candidates[0]
    print(f"[OBJ2FBX-Launcher] Selected: {selected_name}")

    # Pass args to Blender
    cmd = [
        selected_exe,
        "--background",
        "--python", str(script_path),
        "--",
        str(target_edit_dir)
    ]
    if passed_log_path:
        cmd.extend(["--log_path", str(passed_log_path)])

    print(f"[OBJ2FBX-Launcher] Launching Blender: {' '.join(cmd)}")
    
    try:
        subprocess.run(cmd, check=True)
        print("[OBJ2FBX-Launcher] Blender process finished successfully.")
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        print(f"[OBJ2FBX-Launcher] Blender process failed with exit code {e.returncode}.")
        sys.exit(e.returncode)
    except Exception as e:
        print(f"[OBJ2FBX-Launcher] Unexpected error: {e}")
        sys.exit(1)


# =========================================================================================
# Data Structures (Blender Side)
# =========================================================================================
@dataclass
class NodeInfo:
    name: str
    parent_name: Optional[str]
    vxm_basename: Optional[str] = None
    pos: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    rot: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    children: List['NodeInfo'] = field(default_factory=list)


@dataclass
class AnimScalarKey:
    frame: int
    value: float
    ease: int = 1
    slerp: Optional[int] = None


@dataclass
class NodeAnimData:
    name: str
    pos_channels: List[List[AnimScalarKey]] = field(default_factory=lambda: [[], [], []])
    rot_channels: List[List[AnimScalarKey]] = field(default_factory=lambda: [[], [], []])
    slerp_by_frame: Dict[int, int] = field(default_factory=dict)


@dataclass
class AnimationClipInfo:
    name: str
    txt_path: Path
    node_dict: Dict[str, NodeInfo]
    anim_dict: Dict[str, NodeAnimData]
    max_frame: int

# =========================================================================================
# Utils
# =========================================================================================
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


def sanitize_output_component(text: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', '_', text).strip()
    safe = safe.rstrip(". ")
    return safe or "Unnamed"


def parse_clip_name_from_txt(path: Path) -> str:
    prefix = "VXR&VXA."
    if path.name.startswith(prefix) and path.suffix.lower() == ".txt":
        return path.name[len(prefix):-4]
    return path.stem


def discover_animation_clip_txts(edit_dir: Path) -> List[Tuple[str, Path]]:
    clips: List[Tuple[str, Path]] = []
    for clip_path in sorted(edit_dir.glob("VXR&VXA.*.txt"), key=lambda p: p.name.lower()):
        clip_name = parse_clip_name_from_txt(clip_path).strip()
        if clip_name:
            clips.append((clip_name, clip_path))
    return clips


def make_animclip_export_name(clip_name: str) -> str:
    return f"{EXPORT_BASE_NAME}_animclip_{sanitize_output_component(clip_name)}.fbx"


def make_action_name(clip_name: str) -> str:
    return sanitize_output_component(clip_name)[:59] or "Anim"


def make_ascii_export_dir(dest_dir: Path) -> Path:
    base_root = Path(tempfile.gettempdir()) / "VE2RBX_Export_Test"
    base_root.mkdir(parents=True, exist_ok=True)
    prefix = f"{ascii_safe_name(dest_dir.parent.name)}_{ascii_safe_name(dest_dir.name)}_"
    for _ in range(100):
        candidate = base_root / f"{prefix}{uuid.uuid4().hex[:8]}"
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
    raise RuntimeError(f"Failed to create staging export dir under {base_root}")

# =========================================================================================
# Parsers
# =========================================================================================
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

KEYFRAME_LINE_RE = re.compile(
    r"^(?P<frame>\d+)KF => "
    r"POS X:(?P<px>[-\d\.]+),Y:(?P<py>[-\d\.]+),Z:(?P<pz>[-\d\.]+)\s+/\s+"
    r"ROT X:(?P<rx>[-\d\.]+),Y:(?P<ry>[-\d\.]+),Z:(?P<rz>[-\d\.]+)"
    r"(?:\s+\|\s+EASE\s+(?P<ease>.+?))?"
    r"(?:\s+\|\s+SLERP:(?P<slerp>\d+)(?:\([^)]+\))?)?$"
)
EASE_TOKEN_RE = re.compile(r"([PR][XYZ]):(\d+)")


def normalize_node_label(text: str) -> str:
    text = text.strip()
    bracket_index = text.find("[")
    if bracket_index != -1:
        text = text[bracket_index:]
    text = re.sub(r"^\[\-?\d+\]\s*", "", text)
    return text.strip()


def parse_anim_header_name(stripped: str) -> Optional[str]:
    if not stripped.startswith("[") or "]" not in stripped:
        return None
    name = normalize_node_label(stripped)
    name = name.rstrip("：:").strip()
    return name or None


def parse_ease_codes(raw: Optional[str]) -> List[int]:
    codes = [1, 1, 1, 1, 1, 1]
    if not raw:
        return codes

    order = {"PX": 0, "PY": 1, "PZ": 2, "RX": 3, "RY": 4, "RZ": 5}
    for token, value in EASE_TOKEN_RE.findall(raw):
        idx = order.get(token)
        if idx is not None:
            codes[idx] = int(value)
    return codes


def parse_keyframe_line(stripped: str) -> Optional[Tuple[int, Tuple[float, float, float], Tuple[float, float, float], List[int], int]]:
    m = KEYFRAME_LINE_RE.match(stripped)
    if not m:
        return None

    frame = int(m.group("frame"))
    pos = (float(m.group("px")), float(m.group("py")), float(m.group("pz")))
    rot = (float(m.group("rx")), float(m.group("ry")), float(m.group("rz")))
    ease_codes = parse_ease_codes(m.group("ease"))
    slerp = int(m.group("slerp")) if m.group("slerp") is not None else 0
    return frame, pos, rot, ease_codes, slerp


def ensure_anim_node(anim_dict: Dict[str, NodeAnimData], name: str) -> NodeAnimData:
    if name not in anim_dict:
        anim_dict[name] = NodeAnimData(name=name)
    return anim_dict[name]


def parse_vxr_vxa(path: Path) -> Tuple[Dict[str, NodeInfo], List[str], List[str], Dict[str, NodeAnimData], int]:
    log(f"Parsing VXR&VXA.txt: {path}")
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")

    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    node_dict: Dict[str, NodeInfo] = {}
    root_names: List[str] = []
    ordered_names: List[str] = []
    anim_dict: Dict[str, NodeAnimData] = {}
    max_frame = 0
    
    section = None
    stack: List[Tuple[int, NodeInfo]] = []
    current_anim_node_name = None

    for line in lines:
        stripped = line.strip()
        if not stripped: continue
        if stripped.startswith("# Node Hierarchy"):
            section = 'HIERARCHY'
            continue
        elif stripped.startswith("# Animation"):
            section = 'ANIMATION'
            continue
        elif stripped.startswith("#"):
            continue

        if section == 'HIERARCHY':
            raw_line = line.rstrip('\n')
            indent_len = len(raw_line) - len(raw_line.lstrip(' '))
            depth = indent_len // 2
            
            content = raw_line.strip()
            match = re.search(r"\[\-?\d+\]\s*(?P<name>[^/]+?)(?:\s*/\s*(?P<vxm>.+))?$", content)
            if match:
                node_name = match.group("name").strip()
                vxm_part = match.group("vxm")
                if vxm_part:
                    vxm_part = vxm_part.strip()
                    if vxm_part.lower().endswith('.vxm'):
                        vxm_basename = vxm_part[:-4].lower()
                    else:
                        vxm_basename = vxm_part.lower()
                else:
                    vxm_basename = None
            else:
                content = normalize_node_label(content)
                if " / " in content:
                    node_name, vxm_part = content.split(" / ", 1)
                    node_name = node_name.strip()
                    vxm_part = vxm_part.strip()
                    if vxm_part.lower().endswith('.vxm'):
                        vxm_basename = vxm_part[:-4].lower()
                    else:
                        vxm_basename = vxm_part.lower()
                else:
                    node_name = content.strip()
                    vxm_basename = None

            new_node = NodeInfo(name=node_name, parent_name=None, vxm_basename=vxm_basename)
            node_dict[node_name] = new_node
            ordered_names.append(node_name)

            while stack and stack[-1][0] >= depth:
                stack.pop()
            
            if stack:
                parent_info = stack[-1][1]
                new_node.parent_name = parent_info.name
                parent_info.children.append(new_node)
            else:
                root_names.append(node_name)
            
            stack.append((depth, new_node))

        elif section == 'ANIMATION':
            header_name = parse_anim_header_name(stripped)
            if header_name:
                current_anim_node_name = header_name
                continue
            
            if current_anim_node_name and "KF =>" in stripped:
                parsed = parse_keyframe_line(stripped)
                if not parsed:
                    log(f"Warning: Failed to parse animation key line for {current_anim_node_name}: {stripped}")
                    continue

                frame, pos, rot, ease_codes, slerp = parsed
                max_frame = max(max_frame, frame)

                if current_anim_node_name in node_dict and frame == 0:
                    node_dict[current_anim_node_name].pos = pos
                    node_dict[current_anim_node_name].rot = rot

                anim_node = ensure_anim_node(anim_dict, current_anim_node_name)
                for idx, value in enumerate(pos):
                    anim_node.pos_channels[idx].append(
                        AnimScalarKey(frame=frame, value=value, ease=ease_codes[idx])
                    )
                for idx, value in enumerate(rot):
                    anim_node.rot_channels[idx].append(
                        AnimScalarKey(frame=frame, value=value, ease=ease_codes[idx + 3], slerp=slerp if idx == 0 else None)
                    )
                anim_node.slerp_by_frame[frame] = slerp

    for anim_node in anim_dict.values():
        for channels in (anim_node.pos_channels, anim_node.rot_channels):
            for channel in channels:
                channel.sort(key=lambda k: k.frame)

    return node_dict, root_names, ordered_names, anim_dict, max_frame


def apply_ease_curve(code: int, t: float) -> float:
    t = max(0.0, min(1.0, t))
    if code == 2:
        return t * t
    if code == 3:
        return 1.0 - (1.0 - t) * (1.0 - t)
    if code == 4:
        return 2.0 * t * t if t < 0.5 else 1.0 - 2.0 * (1.0 - t) * (1.0 - t)
    if code == 5:
        return t * t * t
    if code == 6:
        inv = 1.0 - t
        return 1.0 - inv * inv * inv
    if code == 7:
        return 4.0 * t * t * t if t < 0.5 else 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0
    return t


def channel_constant(keys: List[AnimScalarKey], default_value: float = 0.0) -> bool:
    if not keys:
        return True
    base = keys[0].value
    return all(abs(k.value - base) <= 1e-6 for k in keys)


def node_transform_is_static(node: NodeInfo, anim: Optional[NodeAnimData]) -> bool:
    if not anim:
        return True
    for channel in anim.pos_channels:
        if not channel_constant(channel):
            return False
    for channel in anim.rot_channels:
        if not channel_constant(channel):
            return False
    return True


def find_segment(keys: List[AnimScalarKey], frame: int) -> Tuple[AnimScalarKey, AnimScalarKey]:
    if not keys:
        raise ValueError("find_segment requires at least one key")
    if frame <= keys[0].frame:
        return keys[0], keys[0]
    if frame >= keys[-1].frame:
        return keys[-1], keys[-1]

    for idx in range(len(keys) - 1):
        a = keys[idx]
        b = keys[idx + 1]
        if a.frame <= frame <= b.frame:
            return a, b
    return keys[-1], keys[-1]


def evaluate_scalar_channel(keys: List[AnimScalarKey], frame: int, default_value: float = 0.0) -> float:
    if not keys:
        return default_value

    a, b = find_segment(keys, frame)
    if a.frame == b.frame or frame <= a.frame:
        return a.value
    if frame >= b.frame:
        return b.value

    t = (frame - a.frame) / float(b.frame - a.frame)
    tt = apply_ease_curve(a.ease, t)
    return a.value + (b.value - a.value) * tt


def vox_pos_to_blender(pos: Tuple[float, float, float]) -> Vector:
    px, py, pz = pos
    return Vector((px * VOXEL_SCALE, pz * VOXEL_SCALE, py * VOXEL_SCALE))


def vox_rot_to_blender_euler(rot: Tuple[float, float, float], is_empty: bool) -> Euler:
    base_x = 0.0 if is_empty else 90.0
    rx, ry, rz = rot
    nx = normalize_deg(rx)
    ny = normalize_deg(ry)
    nz = normalize_deg(rz)
    bx = base_x - nx
    by = -nz
    bz = -ny
    return Euler((math.radians(bx), math.radians(by), math.radians(bz)), 'XZY')


def evaluate_rotation_quaternion(node: NodeInfo, anim: Optional[NodeAnimData], frame: int, is_empty: bool):
    if not anim or not any(anim.rot_channels):
        return vox_rot_to_blender_euler(node.rot, is_empty).to_quaternion()

    if anim.rot_channels[0]:
        start_key, end_key = find_segment(anim.rot_channels[0], frame)
        slerp_flag = 0 if start_key.slerp is None else start_key.slerp
        if slerp_flag and end_key.frame > start_key.frame:
            start_frame = start_key.frame
            end_frame = end_key.frame
            start_rot = tuple(evaluate_scalar_channel(channel, start_frame) for channel in anim.rot_channels)
            end_rot = tuple(evaluate_scalar_channel(channel, end_frame) for channel in anim.rot_channels)
            t = (frame - start_frame) / float(end_frame - start_frame)
            tt = apply_ease_curve(start_key.ease, t)
            q0 = vox_rot_to_blender_euler(start_rot, is_empty).to_quaternion()
            q1 = vox_rot_to_blender_euler(end_rot, is_empty).to_quaternion()
            return q0.slerp(q1, tt)

    raw_rot = tuple(evaluate_scalar_channel(channel, frame) for channel in anim.rot_channels)
    return vox_rot_to_blender_euler(raw_rot, is_empty).to_quaternion()


def evaluate_local_transform(node: NodeInfo, anim: Optional[NodeAnimData], frame: int, is_empty: bool) -> Matrix:
    if anim:
        raw_pos = tuple(evaluate_scalar_channel(channel, frame) for channel in anim.pos_channels)
    else:
        raw_pos = node.pos
    loc = vox_pos_to_blender(raw_pos)
    rot_quat = evaluate_rotation_quaternion(node, anim, frame, is_empty)
    return Matrix.Translation(loc) @ rot_quat.to_matrix().to_4x4()


def mesh_correction_matrix(has_mesh: bool) -> Matrix:
    if not has_mesh:
        return Matrix.Identity(4)
    return Matrix.Rotation(math.radians(90.0), 4, 'X')


def compute_rest_local_matrices(
    ordered_names: List[str],
    survives: Dict[str, bool],
    surviving_parent: Dict[str, Optional[str]],
    joint_frame0_world: Dict[str, Matrix],
) -> Dict[str, Matrix]:
    rest_local: Dict[str, Matrix] = {}
    for name in ordered_names:
        if not survives.get(name):
            continue
        parent_name = surviving_parent.get(name)
        if parent_name and parent_name in joint_frame0_world:
            rest_local[name] = joint_frame0_world[parent_name].inverted() @ joint_frame0_world[name]
        else:
            rest_local[name] = joint_frame0_world[name].copy()
    return rest_local


def compute_bone_length(
    name: str,
    ordered_names: List[str],
    survives: Dict[str, bool],
    surviving_parent: Dict[str, Optional[str]],
    joint_frame0_world: Dict[str, Matrix],
) -> float:
    head = joint_frame0_world[name].to_translation()
    child_distances: List[float] = []
    for child_name in ordered_names:
        if not survives.get(child_name):
            continue
        if surviving_parent.get(child_name) != name:
            continue
        dist = (joint_frame0_world[child_name].to_translation() - head).length
        if dist > 1e-5:
            child_distances.append(dist)
    if child_distances:
        return max(min(child_distances) * 0.35, 0.05)
    return DEFAULT_BONE_LENGTH


def build_armature_rig(
    ordered_names: List[str],
    survives: Dict[str, bool],
    surviving_parent: Dict[str, Optional[str]],
    joint_frame0_world: Dict[str, Matrix],
) -> bpy.types.Object:
    log("Building armature rig...")
    arm_data = bpy.data.armatures.new(IMPORT_ROOT_NAME)
    arm_obj = bpy.data.objects.new(IMPORT_ROOT_NAME, arm_data)
    arm_obj.rotation_mode = 'QUATERNION'
    bpy.context.collection.objects.link(arm_obj)

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')

    edit_bones: Dict[str, bpy.types.EditBone] = {}
    for name in ordered_names:
        if not survives.get(name):
            continue
        eb = arm_data.edit_bones.new(name)
        mat = joint_frame0_world[name]
        head = mat.to_translation()
        rot = mat.to_quaternion()
        y_axis = rot @ Vector((0.0, 1.0, 0.0))
        z_axis = rot @ Vector((0.0, 0.0, 1.0))
        bone_len = compute_bone_length(name, ordered_names, survives, surviving_parent, joint_frame0_world)
        if y_axis.length < 1e-6:
            y_axis = Vector((0.0, 1.0, 0.0))
        eb.head = head
        eb.tail = head + y_axis.normalized() * bone_len
        try:
            eb.align_roll(z_axis)
        except Exception:
            pass
        edit_bones[name] = eb

    for name in ordered_names:
        if not survives.get(name):
            continue
        parent_name = surviving_parent.get(name)
        if parent_name and parent_name in edit_bones:
            edit_bones[name].parent = edit_bones[parent_name]
            edit_bones[name].use_connect = False

    bpy.ops.object.mode_set(mode='OBJECT')
    log(f"Armature rig ready with {len(edit_bones)} bones.")
    return arm_obj


def apply_object_transform(obj: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def bind_mesh_to_bone(mesh_obj: bpy.types.Object, arm_obj: bpy.types.Object, bone_name: str) -> None:
    log(f"Binding {mesh_obj.name} -> bone {bone_name}")
    apply_object_transform(mesh_obj)
    vg = mesh_obj.vertex_groups.get(bone_name)
    if vg is None:
        vg = mesh_obj.vertex_groups.new(name=bone_name)
    indices = [v.index for v in mesh_obj.data.vertices]
    if indices:
        vg.add(indices, 1.0, 'REPLACE')
    mod = mesh_obj.modifiers.new("Armature", 'ARMATURE')
    mod.object = arm_obj
    mesh_obj.parent = arm_obj
    mesh_obj.matrix_parent_inverse = Matrix.Identity(4)


def sample_world_matrices(
    node_dict: Dict[str, NodeInfo],
    ordered_names: List[str],
    anim_dict: Dict[str, NodeAnimData],
    node_is_empty: Dict[str, bool],
    frame: int,
) -> Dict[str, Matrix]:
    world: Dict[str, Matrix] = {}
    for name in ordered_names:
        node = node_dict[name]
        local = evaluate_local_transform(node, anim_dict.get(name), frame, node_is_empty[name])
        if node.parent_name and node.parent_name in world:
            world[name] = world[node.parent_name] @ local
        else:
            world[name] = local
    return world


def load_animation_templates(
    obj_dir: Path,
    pivots_norm: Dict[str, Tuple[float, float, float]],
    vox_sizes: Dict[str, Tuple[int, int, int]],
) -> Tuple[Dict[str, bpy.types.Object], Dict[str, bool]]:
    templates: Dict[str, bpy.types.Object] = {}
    template_emit: Dict[str, bool] = {}

    for obj_file in obj_dir.glob("*.obj"):
        basename = obj_file.stem.lower()
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=str(obj_file))
        else:
            bpy.ops.import_scene.obj(filepath=str(obj_file))

        mesh_obj = next((o for o in bpy.context.selected_objects if o.type == 'MESH'), None)
        if not mesh_obj:
            log(f"Warning: imported {obj_file.name} but found no mesh object.")
            continue

        if APPLY_PIVOT_WORLD and basename in pivots_norm:
            apply_pivot_origin(mesh_obj, pivots_norm[basename], vox_sizes.get(basename))

        bpy.context.view_layer.objects.active = mesh_obj
        mod = mesh_obj.modifiers.new("Decimate", 'DECIMATE')
        mod.decimate_type = 'DISSOLVE'
        mod.angle_limit = math.radians(1.0)
        try:
            mod.delimit = {'UV', 'NORMAL', 'SHARP', 'MATERIAL'}
        except Exception:
            pass
        bpy.ops.object.modifier_apply(modifier=mod.name)
        try:
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            try:
                bpy.ops.mesh.customdata_custom_splitnormals_clear()
            except Exception:
                pass
            try:
                bpy.ops.mesh.remove_doubles(threshold=0.000001)
            except Exception:
                try:
                    bpy.ops.mesh.merge_by_distance(distance=0.000001)
                except Exception:
                    pass
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            bpy.ops.object.shade_flat()
        except Exception:
            pass

        templates[basename] = mesh_obj
        template_emit[basename] = any(
            slot.material and "emit" in slot.material.name.lower()
            for slot in mesh_obj.material_slots
        )
        bpy.ops.object.select_all(action='DESELECT')

    return templates, template_emit


def resolve_template_key(node: NodeInfo, templates: Dict[str, bpy.types.Object]) -> Optional[str]:
    if not node.vxm_basename:
        return None
    if node.vxm_basename in templates:
        return node.vxm_basename
    wanted = norm_name(node.vxm_basename)
    for key in templates.keys():
        if norm_name(key) == wanted:
            return key
    return None


def build_frame0_node_objects(
    ordered_names: List[str],
    node_dict: Dict[str, NodeInfo],
    anim_dict: Dict[str, NodeAnimData],
    node_template_key: Dict[str, Optional[str]],
    node_is_empty: Dict[str, bool],
    templates: Dict[str, bpy.types.Object],
) -> Dict[str, bpy.types.Object]:
    node_objects: Dict[str, bpy.types.Object] = {}

    for name in ordered_names:
        template_key = node_template_key[name]
        if template_key:
            template_obj = templates[template_key]
            obj = template_obj.copy()
            obj.data = template_obj.data.copy()
            obj.name = name
            bpy.context.collection.objects.link(obj)
        else:
            obj = bpy.data.objects.new(name, None)
            obj.empty_display_type = 'PLAIN_AXES'
            bpy.context.collection.objects.link(obj)
        node_objects[name] = obj

    for name in ordered_names:
        parent_name = node_dict[name].parent_name
        if parent_name and parent_name in node_objects:
            obj = node_objects[name]
            parent_obj = node_objects[parent_name]
            obj.parent = parent_obj
            obj.matrix_parent_inverse = parent_obj.matrix_world.inverted()

    for name in ordered_names:
        node = node_dict[name]
        obj = node_objects[name]
        anim = anim_dict.get(name)
        if anim:
            raw_pos = tuple(evaluate_scalar_channel(channel, 0) for channel in anim.pos_channels)
            raw_rot = tuple(evaluate_scalar_channel(channel, 0) for channel in anim.rot_channels)
        else:
            raw_pos = node.pos
            raw_rot = node.rot

        obj.location = vox_pos_to_blender(raw_pos)
        obj.rotation_mode = 'XZY'
        obj.rotation_euler = vox_rot_to_blender_euler(raw_rot, node_is_empty[name])
        obj.scale = (1.0, 1.0, 1.0)

    bpy.context.view_layer.update()
    return node_objects


def prepare_animation_materials(dest_dir: Path) -> Tuple[Optional[Path], bpy.types.Material, bpy.types.Material]:
    src_raw = dest_dir.parent / "OutputOBJ" / "palette.png"
    dest_palette = dest_dir / "palette.png"
    if src_raw.exists():
        ensure_palette_png(src_raw, dest_palette)

    shared_image = None
    if dest_palette.exists():
        for img in bpy.data.images:
            if img.filepath == str(dest_palette):
                shared_image = img
                break
        if not shared_image:
            shared_image = bpy.data.images.load(str(dest_palette), check_existing=False)
        shared_image.filepath = str(dest_palette)
        shared_image.filepath_raw = str(dest_palette)
        try:
            shared_image.reload()
        except Exception:
            pass

    def get_mat(name: str):
        mat = bpy.data.materials.get(name)
        if not mat:
            mat = bpy.data.materials.new(name=name)
            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            nodes.clear()
            output = nodes.new('ShaderNodeOutputMaterial')
            output.location = (300, 0)
            bsdf = nodes.new('ShaderNodeBsdfPrincipled')
            bsdf.location = (0, 0)
            bsdf.inputs['Alpha'].default_value = 1.0
            links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
            if shared_image:
                tex = nodes.new('ShaderNodeTexImage')
                tex.image = shared_image
                tex.location = (-300, 0)
                tex.interpolation = 'Closest'
                links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
            mat.blend_method = 'OPAQUE'
        return mat

    return dest_palette if dest_palette.exists() else None, get_mat("pallete"), get_mat("pallete_emit")


def export_animation_outputs(
    final_objects: List[bpy.types.Object],
    dest_dir: Path,
    dest_palette: Optional[Path],
    include_all_actions: bool,
) -> None:
    log("Preparing armature export outputs...")
    out_blend = dest_dir / f"{EXPORT_BASE_NAME}.blend"
    out_fbx = dest_dir / f"{EXPORT_BASE_NAME}.fbx"
    log("Creating ASCII-safe staging directory...")
    export_dir = make_ascii_export_dir(dest_dir)
    log(f"Staging directory ready: {export_dir}")
    export_blend = export_dir / out_blend.name
    export_fbx = export_dir / out_fbx.name
    export_palette = export_dir / "palette.png"

    if dest_palette and dest_palette.exists():
        log(f"Copying palette to staging: {export_palette}")
        shutil.copy2(dest_palette, export_palette)

    for img in bpy.data.images:
        if "palette" in img.name.lower() and export_palette.exists():
            log(f"Retargeting image {img.name} -> {export_palette}")
            img.filepath = str(export_palette)
            img.filepath_raw = str(export_palette)
            try:
                img.reload()
            except Exception:
                pass

    try:
        log("Packing resources into blend...")
        bpy.ops.file.pack_all()
    except Exception as e:
        log(f"[OBJ2FBXanimation] Warning: pack_all failed: {e}")

    log(f"Saving blend to staging: {export_blend}")
    bpy.ops.wm.save_as_mainfile(filepath=str(export_blend))
    shutil.copy2(export_blend, out_blend)

    for img in bpy.data.images:
        if "palette" in img.name.lower():
            img.filepath = "//palette.png"
            img.filepath_raw = "//palette.png"

    if not final_objects:
        log("No animated objects to export.")
        return

    select_final_objects(final_objects)
    log(f"Exporting armature FBX to staging: {export_fbx}")
    bpy.ops.export_scene.fbx(
        filepath=str(export_fbx),
        use_selection=True,
        object_types={'ARMATURE', 'MESH'},
        apply_unit_scale=True,
        global_scale=0.01,
        bake_space_transform=False,
        axis_forward='-Z',
        axis_up='Y',
        embed_textures=True,
        path_mode='COPY',
        use_custom_props=True,
        add_leaf_bones=False,
        use_armature_deform_only=True,
        bake_anim=True,
        bake_anim_use_nla_strips=False,
        bake_anim_use_all_actions=include_all_actions,
        bake_anim_simplify_factor=0.0,
    )
    log("FBX export finished, copying staged file.")
    shutil.copy2(export_fbx, out_fbx)
    export_fbm = export_fbx.with_suffix(".fbm")
    out_fbm = out_fbx.with_suffix(".fbm")
    if export_fbm.exists():
        if out_fbm.exists():
            shutil.rmtree(out_fbm)
        shutil.copytree(export_fbm, out_fbm)
    log("Exporting armature GLB...")
    export_static_glb(
        final_objects,
        export_dir,
        dest_dir,
        export_palette if export_palette.exists() else dest_dir / "palette.png",
        include_all_actions=include_all_actions,
    )


def export_rbx_animation_clip(arm_obj: bpy.types.Object, dest_dir: Path, clip_name: str, max_frame: int) -> Path:
    out_fbx = dest_dir / make_animclip_export_name(clip_name)
    export_dir = make_ascii_export_dir(dest_dir)
    export_fbx = export_dir / out_fbx.name
    bpy.ops.object.select_all(action='DESELECT')
    arm_obj.select_set(True)
    bpy.context.view_layer.objects.active = arm_obj
    scene = bpy.context.scene
    previous_start = scene.frame_start
    previous_end = scene.frame_end
    previous_current = scene.frame_current
    clip_end = max(int(max_frame), 1)
    log(f"Exporting Roblox animation clip FBX: {export_fbx} (frames 0-{clip_end})")
    try:
        scene.frame_start = 0
        scene.frame_end = clip_end
        scene.frame_set(0)
        bpy.ops.export_scene.fbx(
            filepath=str(export_fbx),
            use_selection=True,
            object_types={'ARMATURE'},
            apply_unit_scale=True,
            global_scale=0.01,
            bake_space_transform=False,
            axis_forward='-Z',
            axis_up='Y',
            use_custom_props=False,
            add_leaf_bones=False,
            use_armature_deform_only=True,
            bake_anim=True,
            bake_anim_use_nla_strips=False,
            bake_anim_use_all_actions=False,
            bake_anim_simplify_factor=0.0,
        )
    finally:
        scene.frame_start = previous_start
        scene.frame_end = previous_end
        scene.frame_set(previous_current)
    shutil.copy2(export_fbx, out_fbx)
    log(f"Saved Roblox animation clip FBX to {out_fbx}")
    return out_fbx


def export_rbx_model_fbx(final_objects: List[bpy.types.Object], dest_dir: Path, dest_palette: Optional[Path]) -> Path:
    out_fbx = dest_dir / RBX_MODEL_FBX_NAME
    export_dir = make_ascii_export_dir(dest_dir)
    export_fbx = export_dir / out_fbx.name
    export_palette = export_dir / "palette.png"
    selected = select_final_objects(final_objects)
    if not selected:
        raise RuntimeError("No objects selected for Roblox model FBX export.")

    if dest_palette and dest_palette.exists():
        shutil.copy2(dest_palette, export_palette)
        log(f"Copied Roblox model palette to staging: {export_palette}")

    # Match the proven static FBX flow: stage the palette to an ASCII-safe path,
    # reload from there, then switch the serialized FBX metadata to a simple relative path.
    if export_palette.exists():
        for img in bpy.data.images:
            if "palette" in img.name.lower():
                img.filepath = str(export_palette)
                img.filepath_raw = str(export_palette)
                try:
                    img.reload()
                    log(f"Roblox model palette relinked to staging: {img.name} -> {export_palette}")
                except Exception as e:
                    log(f"Warning: Roblox model palette reload failed for {img.name}: {e}")

        for img in bpy.data.images:
            if "palette" in img.name.lower():
                img.filepath = "//palette.png"
                img.filepath_raw = "//palette.png"
                log(f"Roblox model palette switched to relative path: {img.name} -> //palette.png")

    log(f"Exporting Roblox model FBX: {export_fbx}")
    bpy.ops.export_scene.fbx(
        filepath=str(export_fbx),
        use_selection=True,
        object_types={'ARMATURE', 'MESH'},
        apply_unit_scale=True,
        global_scale=0.01,
        bake_space_transform=False,
        axis_forward='-Z',
        axis_up='Y',
        embed_textures=False,
        path_mode='COPY',
        use_custom_props=False,
        add_leaf_bones=False,
        use_armature_deform_only=True,
        bake_anim=False,
    )
    shutil.copy2(export_fbx, out_fbx)
    export_fbm = export_fbx.with_suffix(".fbm")
    out_fbm = out_fbx.with_suffix(".fbm")
    if export_fbm.exists():
        if out_fbm.exists():
            shutil.rmtree(out_fbm)
        shutil.copytree(export_fbm, out_fbm)
        log(f"Copied Roblox model FBX texture folder to {out_fbm}")
    log(f"Saved Roblox model FBX to {out_fbx}")
    return out_fbx


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

def export_static_obj(final_objects: List[bpy.types.Object], export_dir: Path, dest_dir: Path, palette_src: Path) -> Path:
    export_obj_dir = export_dir / OBJ_EXPORT_DIR_NAME
    out_obj_dir = dest_dir / OBJ_EXPORT_DIR_NAME
    prepare_clean_dir(export_obj_dir)
    if out_obj_dir.exists():
        shutil.rmtree(out_obj_dir)

    export_obj = export_obj_dir / f"{EXPORT_BASE_NAME}.obj"
    export_palette = export_obj_dir / "palette.png"
    if palette_src.exists():
        shutil.copy2(palette_src, export_palette)

    selected = select_final_objects(final_objects)
    if not selected:
        raise RuntimeError("No objects selected for OBJ export.")

    log(f"Exporting OBJ folder to: {export_obj_dir}")
    if hasattr(bpy.ops.wm, "obj_export"):
        try:
            bpy.ops.wm.obj_export(
                filepath=str(export_obj),
                export_selected_objects=True,
                export_materials=True,
                path_mode='RELATIVE',
                forward_axis='NEGATIVE_Z',
                up_axis='Y',
            )
        except TypeError:
            bpy.ops.wm.obj_export(
                filepath=str(export_obj),
                export_selected_objects=True,
                export_materials=True,
            )
    else:
        bpy.ops.export_scene.obj(
            filepath=str(export_obj),
            use_selection=True,
            use_materials=True,
            path_mode='RELATIVE',
            axis_forward='-Z',
            axis_up='Y',
        )

    if not export_obj.exists():
        raise FileNotFoundError(f"OBJ export did not create {export_obj}")
    if palette_src.exists() and not export_palette.exists():
        shutil.copy2(palette_src, export_palette)

    normalize_obj_material_texture_paths(export_obj_dir)
    shutil.copytree(export_obj_dir, out_obj_dir)
    log(f"Saved OBJ folder to {out_obj_dir} via ASCII staging")
    return out_obj_dir

def export_static_glb(
    final_objects: List[bpy.types.Object],
    export_dir: Path,
    dest_dir: Path,
    palette_src: Path,
    include_all_actions: bool = False,
) -> Path:
    export_glb = export_dir / GLB_EXPORT_FILE_NAME
    out_glb = dest_dir / GLB_EXPORT_FILE_NAME
    legacy_gltf_dir = dest_dir / f"{EXPORT_BASE_NAME}_gltf"
    if out_glb.exists():
        out_glb.unlink()
    if legacy_gltf_dir.exists():
        shutil.rmtree(legacy_gltf_dir)

    selected = select_final_objects(final_objects)
    if not selected:
        raise RuntimeError("No objects selected for GLB export.")

    log(f"Exporting GLB to: {export_glb}")
    gltf_kwargs = {
        "filepath": str(export_glb),
        "use_selection": True,
        "export_format": 'GLB',
        "export_yup": True,
        "export_extras": True,
    }
    if include_all_actions:
        gltf_kwargs.update({
            "export_animation_mode": 'ACTIONS',
            "export_anim_single_armature": True,
            "export_nla_strips": False,
            "export_animations": True,
        })

    try:
        bpy.ops.export_scene.gltf(**gltf_kwargs)
    except TypeError:
        fallback_kwargs = {
            "filepath": str(export_glb),
            "use_selection": True,
            "export_format": 'GLB',
        }
        if include_all_actions:
            fallback_kwargs.update({
                "export_nla_strips": False,
                "export_animations": True,
            })
        bpy.ops.export_scene.gltf(**fallback_kwargs)

    if not export_glb.exists():
        raise FileNotFoundError(f"GLB export did not create {export_glb}")

    shutil.copy2(export_glb, out_glb)
    log(f"Saved GLB to {out_glb} via ASCII staging")
    return out_glb

# =========================================================================================
# Main Logic
# =========================================================================================
def main(edit_dir: Path, script_dir: Path):
    log("========================================")
    log(" Starting OBJ2FBX Animation Armature Conversion")
    log("========================================")
    log(f"Edit Dir: {edit_dir}")
    pivot_txt = edit_dir / "Pivot.txt"
    pivots_norm = parse_pivot_txt(pivot_txt)
    vox_sizes = parse_vox_sizes(edit_dir / "OutputVOX")
    vxr_txt = edit_dir / "VXR&VXA.txt"
    node_dict, root_names, ordered_names, default_anim_dict, default_max_frame = parse_vxr_vxa(vxr_txt)
    log(f"Loaded default animation reference from {vxr_txt.name}")
    log(f"Hierarchy sample: " + ", ".join(ordered_names[:5]))
    default_anim_names = list(default_anim_dict.keys())
    log("Default animation sample: " + ", ".join(default_anim_names[:5]))
    matched_names = set(ordered_names) & set(default_anim_names)
    log(f"Default hierarchy/animation matched names: {len(matched_names)} / {len(ordered_names)}")
    if len(matched_names) != len(ordered_names):
        missing_anim = [name for name in ordered_names if name not in default_anim_dict]
        extra_anim = [name for name in default_anim_names if name not in node_dict]
        if missing_anim:
            log("Missing default animation nodes: " + ", ".join(missing_anim[:10]))
        if extra_anim:
            log("Default animation-only nodes: " + ", ".join(extra_anim[:10]))

    clip_txts = discover_animation_clip_txts(edit_dir)
    if not clip_txts:
        default_clip_name = sanitize_output_component(vxr_txt.stem)
        clip_txts = [(default_clip_name, vxr_txt)]
        log(f"No per-clip VXR&VXA.*.txt found. Falling back to default only: {default_clip_name}")
    else:
        log(f"Discovered {len(clip_txts)} animation clip txt file(s).")

    clip_infos: List[AnimationClipInfo] = []
    for clip_name, clip_txt in clip_txts:
        clip_node_dict, clip_root_names, clip_ordered_names, clip_anim_dict, clip_max_frame = parse_vxr_vxa(clip_txt)
        if clip_ordered_names != ordered_names:
            raise RuntimeError(f"Hierarchy mismatch in {clip_txt.name}; animation clips must share the same node order.")
        if clip_root_names != root_names:
            raise RuntimeError(f"Root hierarchy mismatch in {clip_txt.name}; animation clips must share the same roots.")
        clip_infos.append(
            AnimationClipInfo(
                name=clip_name,
                txt_path=clip_txt,
                node_dict=clip_node_dict,
                anim_dict=clip_anim_dict,
                max_frame=clip_max_frame,
            )
        )
        clip_anim_names = list(clip_anim_dict.keys())
        clip_matched = set(ordered_names) & set(clip_anim_names)
        log(f"Clip '{clip_name}': matched names {len(clip_matched)} / {len(ordered_names)}, max frame {clip_max_frame}")

    global_max_frame = max([default_max_frame] + [clip.max_frame for clip in clip_infos])

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = IMPORT_ROOT_NAME
    scene.render.fps = DEFAULT_ANIM_FPS
    scene.frame_start = 0
    scene.frame_end = max(global_max_frame, 1)

    obj_dir = edit_dir / "OutputOBJ"
    if not obj_dir.exists():
        raise FileNotFoundError(f"{obj_dir} does not exist")

    templates, template_emit = load_animation_templates(obj_dir, pivots_norm, vox_sizes)

    node_template_key: Dict[str, Optional[str]] = {}
    node_is_empty: Dict[str, bool] = {}
    node_emit: Dict[str, bool] = {}
    node_dynamic: Dict[str, bool] = {}
    survives: Dict[str, bool] = {}
    surviving_parent: Dict[str, Optional[str]] = {}

    for name in ordered_names:
        node = node_dict[name]
        template_key = resolve_template_key(node, templates)
        node_template_key[name] = template_key
        node_is_empty[name] = template_key is None
        node_emit[name] = bool(template_key and template_emit.get(template_key, False))
        node_dynamic[name] = any(
            not node_transform_is_static(clip.node_dict[name], clip.anim_dict.get(name))
            for clip in clip_infos
        )

    for name in ordered_names:
        survives[name] = (node_dict[name].parent_name is None) or node_dynamic[name]

    dynamic_names = [name for name in ordered_names if node_dynamic.get(name)]
    log(f"Dynamic nodes: {len(dynamic_names)} / {len(ordered_names)}")
    if dynamic_names:
        log("Dynamic node list: " + ", ".join(dynamic_names[:20]))
    surviving_names = [name for name in ordered_names if survives.get(name)]
    log(f"Surviving controllers: {len(surviving_names)} / {len(ordered_names)}")

    for name in ordered_names:
        parent_name = node_dict[name].parent_name
        while parent_name and not survives.get(parent_name, False):
            parent_name = node_dict[parent_name].parent_name
        surviving_parent[name] = parent_name

    joint_node_is_empty = {name: True for name in ordered_names}
    mesh_node_is_empty = dict(node_is_empty)
    joint_frame0_world = sample_world_matrices(node_dict, ordered_names, default_anim_dict, joint_node_is_empty, 0)
    mesh_frame0_world = sample_world_matrices(node_dict, ordered_names, default_anim_dict, mesh_node_is_empty, 0)
    rest_local = compute_rest_local_matrices(ordered_names, survives, surviving_parent, joint_frame0_world)

    node_objects = build_frame0_node_objects(
        ordered_names,
        node_dict,
        default_anim_dict,
        node_template_key,
        node_is_empty,
        templates,
    )

    mesh_groups: Dict[Tuple[str, bool], List[bpy.types.Object]] = {}
    for name in ordered_names:
        template_key = node_template_key[name]
        if not template_key:
            continue
        target_name = name if survives[name] else surviving_parent[name]
        if not target_name:
            target_name = name
        source_obj = node_objects[name]
        dup = source_obj.copy()
        dup.data = source_obj.data.copy()
        dup.name = f"{target_name}__{name}"
        bpy.context.collection.objects.link(dup)
        source_world = source_obj.matrix_world.copy()
        dup.data.transform(source_world)
        dup.data.update()
        dup.parent = None
        dup.matrix_parent_inverse = Matrix.Identity(4)
        dup.matrix_world = Matrix.Identity(4)
        dup.location = (0.0, 0.0, 0.0)
        dup.rotation_mode = 'QUATERNION'
        dup.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        dup.scale = (1.0, 1.0, 1.0)
        mesh_groups.setdefault((target_name, node_emit[name]), []).append(dup)

    for obj in node_objects.values():
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
    for tpl in templates.values():
        bpy.data.objects.remove(tpl, do_unlink=True)

    carrier_objects: List[bpy.types.Object] = []
    for (target_name, emit_flag), group in mesh_groups.items():
        if len(group) > 1:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in group:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = group[0]
            bpy.ops.object.join()
            carrier = bpy.context.view_layer.objects.active
        else:
            carrier = group[0]
        carrier.name = f"{target_name}{'_Emit' if emit_flag else ''}_Mesh"
        carrier_objects.append(carrier)

    arm_obj = build_armature_rig(ordered_names, survives, surviving_parent, joint_frame0_world)
    arm_obj["VE2RBX_CreatedBy"] = "Created by KisaragiKoubou"

    dest_dir = edit_dir / "OutputFBX"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_palette, mat_std, mat_emit = prepare_animation_materials(dest_dir)
    for carrier in carrier_objects:
        carrier.data.materials.clear()
        carrier.data.materials.append(mat_emit if carrier.name.endswith("_Emit_Mesh") else mat_std)
        target_name = carrier.name[:-5]
        if target_name.endswith("_Emit"):
            target_name = target_name[:-5]
        bind_mesh_to_bone(carrier, arm_obj, target_name)

    dynamic_survivors = [name for name in ordered_names if survives[name] and node_dynamic[name]]
    log(f"Animating {len(dynamic_survivors)} pose bones across {len(clip_infos)} clip(s)...")
    arm_obj.animation_data_create()
    pose_bones = arm_obj.pose.bones
    action_map: Dict[str, bpy.types.Action] = {}
    for clip in clip_infos:
        action_name = make_action_name(clip.name)
        action = bpy.data.actions.new(action_name)
        arm_obj.animation_data.action = action
        for frame in range(0, clip.max_frame + 1):
            scene.frame_set(frame)
            world = sample_world_matrices(clip.node_dict, ordered_names, clip.anim_dict, joint_node_is_empty, frame)
            for name in dynamic_survivors:
                pb = pose_bones[name]
                parent_name = surviving_parent[name]
                local = world[name].copy()
                if parent_name:
                    local = world[parent_name].inverted() @ local
                delta = rest_local[name].inverted() @ local
                loc, rot, _scale = delta.decompose()
                pb.location = loc
                pb.rotation_mode = 'QUATERNION'
                pb.rotation_quaternion = rot
                pb.keyframe_insert(data_path="location", frame=frame)
                pb.keyframe_insert(data_path="rotation_quaternion", frame=frame)
        action_map[clip.name] = action
        log(f"Built action '{clip.name}' ({action.name}) with {clip.max_frame + 1} frame(s).")

    if clip_infos:
        arm_obj.animation_data.action = action_map[clip_infos[0].name]

    scene.frame_set(0)
    final_objects = [arm_obj] + carrier_objects
    log(f"Final export set: {len(final_objects)} objects ({len(carrier_objects)} meshes + armature)")
    for obj in final_objects:
        try:
            obj["VE2RBX_CreatedBy"] = "Created by KisaragiKoubou"
        except Exception as e:
            log(f"Watermark set failed: {obj.name} : {e}")

    export_animation_outputs(final_objects, dest_dir, dest_palette, include_all_actions=len(action_map) > 1)
    export_rbx_model_fbx(final_objects, dest_dir, dest_palette)
    for clip in clip_infos:
        arm_obj.animation_data.action = action_map[clip.name]
        export_rbx_animation_clip(arm_obj, dest_dir, clip.name, clip.max_frame)
    log("Animation export done.")

if __name__ == "__main__":
    script_dir = Path(__file__).resolve().parent
    try:
        # Blender Side Argument Parsing
        edit_dir_arg, log_path_arg = parse_ve2rbx_args()
        
        if log_path_arg:
            setup_logger(log_path_arg)
            
        if edit_dir_arg:
            edit_dir = edit_dir_arg
        else:
            edit_dir = find_edit_dir(script_dir)
            
        main(edit_dir, script_dir)
    except Exception as e:
        log(f"Critical Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
