import sys
import os
import re
import math
import struct
import subprocess
import shutil
import tempfile
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
EXPORT_BASE_NAME = "ve2rbx_output"
OBJ_EXPORT_DIR_NAME = f"{EXPORT_BASE_NAME}_obj"
GLB_EXPORT_FILE_NAME = f"{EXPORT_BASE_NAME}.glb"
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


def make_ascii_export_dir(dest_dir: Path) -> Path:
    env_root = os.environ.get("VE2RBX_ASCII_EXPORT_ROOT", "").strip()
    if env_root:
        base_root = Path(env_root)
    else:
        base_root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "VE2RBX_Export"
    base_root.mkdir(parents=True, exist_ok=True)
    prefix = f"{ascii_safe_name(dest_dir.parent.name)}_{ascii_safe_name(dest_dir.name)}"
    for attempt in range(100):
        candidate = base_root / f"{prefix}_{attempt:02d}"
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
    raise RuntimeError(f"Failed to allocate ASCII export dir under {base_root}")

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

def parse_vxr_vxa(path: Path) -> Tuple[Dict[str, NodeInfo], List[str], List[str]]:
    log(f"Parsing VXR&VXA.txt: {path}")
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")

    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    node_dict: Dict[str, NodeInfo] = {}
    root_names: List[str] = []
    ordered_names: List[str] = []
    
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
            if content.startswith('└'):
                content = content[1:].strip()
            
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
            if "：" in stripped:
                current_anim_node_name = stripped.split("：")[0].strip()
                continue
            
            if current_anim_node_name and stripped.startswith("0KF =>"):
                if current_anim_node_name in node_dict:
                    try:
                        datapart = stripped.split("=>", 1)[1]
                        pos_part, rot_part = datapart.split("/", 1)
                        def parse_xyz(s):
                            x = float(re.search(r"X:([-\d\.]+)", s).group(1))
                            y = float(re.search(r"Y:([-\d\.]+)", s).group(1))
                            z = float(re.search(r"Z:([-\d\.]+)", s).group(1))
                            return x, y, z
                        px, py, pz = parse_xyz(pos_part)
                        rx, ry, rz = parse_xyz(rot_part)
                        node_dict[current_anim_node_name].pos = (px, py, pz)
                        node_dict[current_anim_node_name].rot = (rx, ry, rz)
                    except Exception as e:
                        log(f"Warning: Failed to parse animation 0KF for {current_anim_node_name}: {e}")
                current_anim_node_name = None

    return node_dict, root_names, ordered_names

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

def export_static_glb(final_objects: List[bpy.types.Object], export_dir: Path, dest_dir: Path, palette_src: Path) -> Path:
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
    try:
        bpy.ops.export_scene.gltf(
            filepath=str(export_glb),
            use_selection=True,
            export_format='GLB',
            export_yup=True,
            export_extras=True,
        )
    except TypeError:
        bpy.ops.export_scene.gltf(
            filepath=str(export_glb),
            use_selection=True,
            export_format='GLB',
        )

    if not export_glb.exists():
        raise FileNotFoundError(f"GLB export did not create {export_glb}")

    shutil.copy2(export_glb, out_glb)
    log(f"Saved GLB to {out_glb} via ASCII staging")
    return out_glb

# =========================================================================================
# Main Logic
# =========================================================================================
def main(edit_dir: Path, script_dir: Path):
    # Logging initiated by entry point
    log("========================================")
    log(" Starting OBJ2FBX Conversion")
    log("========================================")
    log(f"Edit Dir: {edit_dir}")
    log(f"APPLY_PIVOT_WORLD: {APPLY_PIVOT_WORLD}")

    # 1. Parse Metadata
    pivot_txt = edit_dir / "Pivot.txt"
    pivots_norm = parse_pivot_txt(pivot_txt)
    vox_sizes = parse_vox_sizes(edit_dir / "OutputVOX")
    
    vxr_txt = edit_dir / "VXR&VXA.txt"
    node_dict, root_names, ordered_names = parse_vxr_vxa(vxr_txt)
    log(f"Loaded {len(node_dict)} nodes from VXR&VXA.txt")

    # 2. Blender Init
    log("Initializing Blender Scene...")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.name = IMPORT_ROOT_NAME

    # 3. Import & Template Creation
    log("Importing OBJ templates...")
    obj_dir = edit_dir / "OutputOBJ"
    if not obj_dir.exists():
        raise FileNotFoundError(f"{obj_dir} does not exist")

    templates: Dict[str, bpy.types.Object] = {}
    obj_files = list(obj_dir.glob("*.obj"))

    for obj_file in obj_files:
        basename = os.path.splitext(obj_file.name)[0].lower()
        
        # Import
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=str(obj_file))
        else:
            bpy.ops.import_scene.obj(filepath=str(obj_file))
        
        # Find Mesh
        selected = bpy.context.selected_objects
        mesh_obj = None
        for o in selected:
            if o.type == 'MESH':
                mesh_obj = o
                break
        
        if mesh_obj:
            # PIVOT (Preserved)
            if APPLY_PIVOT_WORLD and basename in pivots_norm:
                log(f"Applying Pivot for template '{basename}'...")
                apply_pivot_origin(mesh_obj, pivots_norm[basename], vox_sizes.get(basename))

            # --- OPTIMIZATION START (VE2RBX) ---
            # Restore User Spec: Apply Decimate(Dissolve) to Template ONLY.
            # This reduces planar geometry while protecting UVs, before any joining occurs.
            log(f"[Optimize] {basename}: Decimate(Planar 5deg) + Delimit UV")
            bpy.context.view_layer.objects.active = mesh_obj
            
            # (A) Decimate: Planar (= DISSOLVE) - REFINED
            mod = mesh_obj.modifiers.new("Decimate", 'DECIMATE')
            mod.decimate_type = 'DISSOLVE'
            mod.angle_limit = math.radians(1.0) # Reduced from 5.0 to 1.0 to prevent shading artifacts
            try:
                # Protect ALL boundaries to prevent shading bleed
                mod.delimit = {'UV', 'NORMAL', 'SHARP', 'MATERIAL'} 
            except Exception:
                pass
            bpy.ops.object.modifier_apply(modifier=mod.name)

            # (B) Normal Fix & Cleanup (Strict)
            # Remove artifacts caused by Decimate
            
            # 1) Clear Custom Normals & Merge tiny doubles
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            try:
                bpy.ops.mesh.customdata_custom_splitnormals_clear()
            except: pass
            
            try:
                # Remove micro-gaps created by float precision
                bpy.ops.mesh.remove_doubles(threshold=0.000001)
            except:
                try: bpy.ops.mesh.merge_by_distance(distance=0.000001)
                except: pass
                
            bpy.ops.object.mode_set(mode='OBJECT')

            # 2) Shade Flat Force
            try: bpy.ops.object.shade_flat()
            except: pass

            log(f"[Optimize] {basename}: done")
            # --- OPTIMIZATION END ---
            
            templates[basename] = mesh_obj
            bpy.ops.object.select_all(action='DESELECT')
        else:
            log(f"Warning: imported {obj_file.name} but found no mesh object.")

    # 4. Tree Construction
    log("Constructing Node Tree...")
    node_objects: Dict[str, bpy.types.Object] = {}

    for name in ordered_names:
        node = node_dict[name]
        target_template = None
        assigned = False
        template_key = ""

        if node.vxm_basename:
            if node.vxm_basename in templates:
                target_template = templates[node.vxm_basename]
                template_key = node.vxm_basename
                assigned = True
            else:
                n_base = norm_name(node.vxm_basename)
                for key in templates.keys():
                    if norm_name(key) == n_base:
                        target_template = templates[key]
                        template_key = key
                        assigned = True
                        break
        
        log(f"[TEMPLATE] {name} | vxm={node.vxm_basename} | key={template_key} | assigned={assigned}")

        if target_template:
            obj = target_template.copy()
            obj.data = target_template.data.copy()
            obj.name = node.name
            bpy.context.collection.objects.link(obj)
        else:
            obj = bpy.data.objects.new(node.name, None)
            obj.empty_display_type = 'PLAIN_AXES'
            bpy.context.collection.objects.link(obj)
        
        node_objects[name] = obj
    
    for name in ordered_names:
        node = node_dict[name]
        obj = node_objects[name]
        if node.parent_name and node.parent_name in node_objects:
            parent_obj = node_objects[node.parent_name]
            obj.parent = parent_obj
            obj.matrix_parent_inverse = parent_obj.matrix_world.inverted()

    # 5. Apply Transforms
    log("Applying Transforms...")
    for name in ordered_names:
        node = node_dict[name]
        obj = node_objects[name]
        is_empty = (obj.data is None)
        
        if any(node.pos):
            px, py, pz = node.pos
            bx = px * VOXEL_SCALE
            by = pz * VOXEL_SCALE
            bz = py * VOXEL_SCALE
            obj.location = (bx, by, bz)
        else:
            obj.location = (0.0, 0.0, 0.0)

        base_x = 0.0 if is_empty else 90.0
        
        # Enforce XZY mode strictly (Fix for axis flipping)
        obj.rotation_mode = 'XZY'

        if any(node.rot):
            rx, ry, rz = node.rot
            nx = normalize_deg(rx)
            ny = normalize_deg(ry)
            nz = normalize_deg(rz)

            bx = base_x - nx
            by = nz
            bz = -ny
            by = -by
            
            # Log debug info for key nodes
            if any(t in node.name for t in ['MR4', 'MR5', 'SR15', 'SR16', 'SR17', 'SR18', 'GR1']):
                log(f"[RotXZY] {node.name}: Raw({rx},{ry},{rz}) -> EulerXZY({bx:.2f}, {by:.2f}, {bz:.2f})")

            obj.rotation_euler = (math.radians(bx), math.radians(by), math.radians(bz))
        else:
            bx = base_x
            obj.rotation_euler = (math.radians(bx), 0.0, 0.0)

    # 6. Cleanup Templates
    for tpl in templates.values():
        bpy.data.objects.remove(tpl, do_unlink=True)

    # ----------------------------------------------------
    # 7 & 8. Bake, Join, Materials, Export (Unified OutputFBX)
    # ----------------------------------------------------
    final_objects = []

    if not KEEP_HIERARCHY_MODE:
        log("Starting Final Process: Material Split & Join...")
        all_meshes = [o for o in node_objects.values() if o.type == 'MESH']
        bpy.context.view_layer.update()
        
        # --- UNIFIED OUTPUT DIR ---
        dest_dir = edit_dir / "OutputFBX"
        try:
            if not dest_dir.exists():
                dest_dir.mkdir(parents=True, exist_ok=True)
            log(f"Output Dir: {dest_dir}")
        except Exception as e:
            log(f"Error creating OutputFBX dir: {e}")
            
        # 1. Prepare Palette in Destination (Crucial for Relative/Copy mode)
        src_raw = edit_dir / "OutputOBJ" / "palette.png"
        dest_palette = dest_dir / "palette.png"
        
        if src_raw.exists():
            try:
                 ensure_palette_png(src_raw, dest_palette)
                 log(f"Prepared deterministic palette -> {dest_palette}")
            except Exception as e: log(f"Copy Error: {e}")
        else:
            log(f"Palette not found at {src_raw}")

        # 3. Load Shared Image from DESTINATION
        shared_image = None
        if dest_palette.exists():
            try:
                # Load or Find
                is_loaded = False
                for img in bpy.data.images:
                    if img.filepath == str(dest_palette):
                        shared_image = img
                        is_loaded = True
                        break
                
                if not shared_image:
                    shared_image = bpy.data.images.load(str(dest_palette), check_existing=False) # Load new
                
                # Enforce Path & Reload
                shared_image.filepath = str(dest_palette)
                shared_image.filepath_raw = str(dest_palette)
                try:
                    shared_image.reload()
                    log(f"Loaded & Reloaded Palette: {dest_palette}")
                except:
                    log(f"Warning: Failed to reload image {shared_image.name}")

            except Exception as e:
                log(f"Error loading palette: {e}")

        # Canonical Materials
        def get_mat(name, is_emit):
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
                # Force OPAQUE - Fix Translucency
                bsdf.inputs['Alpha'].default_value = 1.0
                
                links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
                if shared_image:
                    tex = nodes.new('ShaderNodeTexImage')
                    tex.image = shared_image
                    tex.location = (-300, 0)
                    tex.interpolation = 'Closest'
                    # CONNECT COLOR ONLY - DO NOT CONNECT ALPHA
                    links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
            
            # Helper: Force Blend Mode OPAQUE (Eevee/Viewport)
            mat.blend_method = 'OPAQUE'
            return mat

        mat_std = get_mat("pallete", False)
        mat_emit = get_mat("pallete_emit", True)
        
        # Normalize Objects
        for obj in all_meshes:
            use_emit = False
            for slot in obj.material_slots:
                if slot.material and "emit" in slot.material.name.lower():
                    use_emit = True
                    break
            obj.data.materials.clear()
            if use_emit: obj.data.materials.append(mat_emit)
            else: obj.data.materials.append(mat_std)
                
        # Bake (Matrix Burn-In & Determinant Fix)
        log("Baking Transforms (Matrix-Burn-In Mode)...")
        bpy.context.view_layer.update()
        
        import bmesh
        from mathutils import Matrix

        for obj in all_meshes:
            if not obj or obj.type != 'MESH' or not obj.data:
                continue
                
            M = obj.matrix_world.copy()
            obj.data.transform(M)
            
            det = M.determinant()
            if det < 0:
                log(f"  [Bake] {obj.name}: Negative Determinant ({det:.4f}) -> Flipping Faces.")
                bm = bmesh.new()
                bm.from_mesh(obj.data)
                for f in bm.faces:
                    f.normal_flip() 
                bm.to_mesh(obj.data)
                bm.free()
            
            obj.parent = None
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_world = Matrix.Identity(4)
            
            obj.location = (0.0, 0.0, 0.0)
            obj.rotation_euler = (0.0, 0.0, 0.0)
            obj.scale = (1.0, 1.0, 1.0)
            
            obj.data.update()
            
        bpy.context.view_layer.update()
        
        # Cleanup
        for obj in bpy.data.objects:
            if obj.type != 'MESH' and obj.name not in ['Camera', 'Light']:
                 if obj not in all_meshes:
                     bpy.data.objects.remove(obj, do_unlink=True)
                     
        # Join (Split by Tri Limit)
        log("Joining Meshes (Split by 19500 tris limit)...")

        TRI_LIMIT = 19500

        def estimate_tris(obj):
            # Triangulation-safe estimate: sum over polygons (n-2)
            if not obj or obj.type != 'MESH' or not obj.data:
                return 0
            tris = 0
            for p in obj.data.polygons:
                n = len(p.vertices)
                if n >= 3:
                    tris += (n - 2)
            return tris

        def split_into_groups_by_tris(objs, tri_limit):
            groups = []
            cur = []
            cur_tris = 0
            for o in objs:
                t = estimate_tris(o)
                # If single object exceeds limit, keep it alone (best-effort) and warn
                if t > tri_limit:
                    if cur:
                        groups.append(cur)
                        cur = []
                        cur_tris = 0
                    groups.append([o])
                    log(f"Warning: single object '{o.name}' estimated tris {t} > limit {tri_limit}. Keeping as its own group.")
                    continue

                if cur and (cur_tris + t) > tri_limit:
                    groups.append(cur)
                    cur = [o]
                    cur_tris = t
                else:
                    cur.append(o)
                    cur_tris += t

            if cur:
                groups.append(cur)
            return groups

        def join_group(group, name):
            bpy.ops.object.select_all(action='DESELECT')
            for o in group:
                if o and o.name in bpy.data.objects:
                    o.select_set(True)
            bpy.context.view_layer.objects.active = group[0]
            bpy.ops.object.join()
            res = bpy.context.view_layer.objects.active
            res.name = name
            return res

        list_std = []
        list_emit = []
        for obj in all_meshes:
            if obj.material_slots and obj.material_slots[0].material and obj.material_slots[0].material.name == "pallete_emit":
                list_emit.append(obj)
            else:
                list_std.append(obj)

        # Split std into multiple joined objects
        if list_std:
            std_groups = split_into_groups_by_tris(list_std, TRI_LIMIT)
            for i, g in enumerate(std_groups, start=1):
                group_tris = sum(estimate_tris(x) for x in g)
                log(f"[JoinGroup] STD {i:03d}: items={len(g)} est_tris={group_tris}")
                res = join_group(g, f"{IMPORT_ROOT_NAME}_Model_{i:03d}")
                final_objects.append(res)

        # Split emit into multiple joined objects
        if list_emit:
            emit_groups = split_into_groups_by_tris(list_emit, TRI_LIMIT)
            for i, g in enumerate(emit_groups, start=1):
                group_tris = sum(estimate_tris(x) for x in g)
                log(f"[JoinGroup] EMIT {i:03d}: items={len(g)} est_tris={group_tris}")
                res = join_group(g, f"{IMPORT_ROOT_NAME}_Model_Emit_{i:03d}")
                final_objects.append(res)
    else:
        log("KEEP_HIERARCHY_MODE: True (Skip Bake/Join)")
        final_objects = list(node_objects.values())
        
        # Ensure dest dir exists even in hierarchy mode
        dest_dir = edit_dir / "OutputFBX"
        dest_dir.mkdir(parents=True, exist_ok=True)


    # --- Watermark / Ownership Tag (FBX Custom Properties) ---
    WATERMARK_KEY = "VE2RBX_CreatedBy"
    WATERMARK_VAL = "Created by KisaragiKoubou"

    target_for_watermark = final_objects if not KEEP_HIERARCHY_MODE else node_objects.values()
    count_wm = 0
    for o in target_for_watermark:
        if o:
            try:
                o[WATERMARK_KEY] = WATERMARK_VAL
                count_wm += 1
            except Exception as e:
                log(f"Watermark set failed: {o.name} : {e}")
    log(f"Watermark injected: {WATERMARK_KEY}='{WATERMARK_VAL}' (count={count_wm})")

    # Final Outputs
    out_blend = dest_dir / f"{EXPORT_BASE_NAME}.blend"
    out_fbx = dest_dir / f"{EXPORT_BASE_NAME}.fbx"
    export_dir = make_ascii_export_dir(dest_dir)
    export_blend = export_dir / out_blend.name
    export_fbx = export_dir / out_fbx.name
    export_palette = export_dir / "palette.png"
    
    log(f"Preparing Export to: {out_fbx}")
    log(f"ASCII Export staging dir: {export_dir}")
    shutil.copy2(dest_dir / "palette.png", export_palette)
    log(f"Copied palette to ASCII staging: {export_palette}")
    
    # Texture Final Check (Reload palette instances from safe absolute path first)
    for img in bpy.data.images:
        if "palette" in img.name.lower():
            target_abs = str(export_palette)
            if img.filepath != target_abs:
                img.filepath = target_abs
                img.filepath_raw = target_abs
                try:
                    img.reload()
                    log(f"Confirmed palette absolute relink: {img.name} -> {target_abs}")
                except:
                     pass

    # Pack all external files (textures) into .blend to fix references when moved
    try:
        bpy.ops.file.pack_all()
        log("[OBJ2FBX] Packed all external files into .blend")
    except Exception as e:
        log(f"[OBJ2FBX] Warning: pack_all failed: {e}")

    # Save Blend (Unified Path)
    bpy.ops.wm.save_as_mainfile(filepath=str(export_blend))
    shutil.copy2(export_blend, out_blend)
    log(f"Saved .blend to {out_blend} via ASCII staging")

    # Repoint texture paths to relative before FBX export so non-ASCII absolute paths
    # do not leak into FBX texture metadata.
    target_rel = "//palette.png"
    for img in bpy.data.images:
        if "palette" in img.name.lower():
            img.filepath = target_rel
            img.filepath_raw = target_rel
            log(f"Confirmed palette relative relink: {img.name} -> {target_rel}")

    # Export FBX
    if final_objects:
        select_final_objects(final_objects)
        
        # Debug Material Refs
        for m in bpy.data.materials:
                 if m.use_nodes:
                     for n in m.node_tree.nodes:
                         if n.type == 'TEX_IMAGE' and n.image:
                             log(f"Export Material '{m.name}' Image Ref: {n.image.filepath}")

        # Export with EMBED + COPY
        bpy.ops.export_scene.fbx(
            filepath=str(export_fbx),
            use_selection=True,
            apply_unit_scale=True,
            global_scale=0.01,
            bake_space_transform=False,
            axis_forward='-Z',
            axis_up='Y',
            embed_textures=True,  # ENABLED
            path_mode='COPY',     # ENABLED
            use_custom_props=True
        )
        shutil.copy2(export_fbx, out_fbx)
        export_fbm = export_fbx.with_suffix(".fbm")
        out_fbm = out_fbx.with_suffix(".fbm")
        if export_fbm.exists():
            if out_fbm.exists():
                shutil.rmtree(out_fbm)
            shutil.copytree(export_fbm, out_fbm)
            log(f"Copied ASCII export texture folder to {out_fbm}")
        log(f"Saved .fbx with textures to {out_fbx} via ASCII staging")

        export_static_obj(final_objects, export_dir, dest_dir, export_palette)
        export_static_glb(final_objects, export_dir, dest_dir, export_palette)
    else:
        log("No objects to export.")

    log("Done.")

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
