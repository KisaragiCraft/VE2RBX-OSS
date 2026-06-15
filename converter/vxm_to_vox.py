import os
import struct
import math
import glob
import re
import io
from pathlib import Path

# ==================================================================================
# Configuration & Constants (Hardened)
# ==================================================================================

def transform_coord(vx_x, vx_y, vx_z, size_x, size_y, size_z):
    """
    Map VXM voxel coordinates (x, y, z) to VOX coordinates so that
    VOX = (x, z, y). This matches VoxEdit's .vox export:
        VXM: (x, y, z)
        VOX: (x, z, y)
    """
    return vx_x, vx_z, vx_y

# ==================================================================================
# Utils
# ==================================================================================

def find_input_folder(base_dir, force_target=None):
    if force_target:
        path = os.path.join(base_dir, force_target)
        if os.path.isdir(path):
            return path
    candidates = []
    for d in os.listdir(base_dir):
        if d.startswith("OutputVXM") and os.path.isdir(d):
            m = re.match(r"OutputVXM\s*(\d+)", d)
            if m:
                num = int(m.group(1))
                candidates.append((num, d))
    if not candidates: return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]

def create_output_folder(base_dir):
    existing_nums = set()
    for d in os.listdir(base_dir):
        if d.startswith("OutputVOX ") and os.path.isdir(d):
            try:
                num = int(d.split(" ")[1])
                existing_nums.add(num)
            except ValueError:
                continue
    m = 1
    while m in existing_nums: m += 1
    out_name = f"OutputVOX {m}"
    os.makedirs(out_name, exist_ok=True)
    return out_name

# ==================================================================================
# VXM Parser (Hardened)
# ==================================================================================

class VxmData:
    def __init__(self, filename):
        self.filename = filename
        self.basename = os.path.splitext(os.path.basename(filename))[0]
        self.size = (0, 0, 0)
        self.pivot = (0.0, 0.0, 0.0)
        self.palette_rgba = [(0,0,0,0)] * 256
        self.palette_emissive_flag = [False] * 256
        self.voxel_grid = {} 

class VxmParser:
    def parse(self, filepath):
        print(f"Parsing: {filepath}")
        vxm = VxmData(filepath)
        
        with open(filepath, 'rb') as f:
            if f.read(4) != b'VXMC': 
                raise ValueError(f"[{vxm.basename}] Invalid magic header")
                
            vxm.size = struct.unpack('<III', f.read(12))
            sizeX, sizeY, sizeZ = vxm.size
            if sizeX > 256 or sizeY > 256 or sizeZ > 256:
                raise ValueError(f"[{vxm.basename}] Voxel size exceeds 256: {sizeX}x{sizeY}x{sizeZ}")
            total_voxels = sizeX * sizeY * sizeZ
            vxm.pivot = struct.unpack('<fff', f.read(12))

            # Robust P1/P2/P3 Detection
            f.seek(0)
            all_data = f.read()
            
            sig = b'\xFF\x00\xFF\xFF'
            black = b'\x00\x00\x00\xFF'
            sig_indices = []
            s_idx = 0
            while True:
                idx = all_data.find(sig, s_idx)
                if idx == -1: break
                sig_indices.append(idx)
                s_idx = idx + 4
            
            valid_candidates = []  # (p1_start, p3_start, p3_count) tuples
            
            for end_idx in sig_indices:
                p1_start = end_idx - 1020
                if p1_start < 0: continue
                
                # Check P1 (RGBA)
                p1_cand = all_data[p1_start : p1_start + 1024]
                if len(p1_cand) != 1024: continue
                if p1_cand[-4:] != b'\xFF\x00\xFF\xFF': continue 
                
                # Check P2 Basic Signature
                p2_start = p1_start + 1024
                p2_cand = all_data[p2_start : p2_start + 1024]
                if len(p2_cand) != 1024: continue
                if p2_cand[-4:] != b'\xFF\x00\xFF\xFF': continue
                
                # Check optional template table, then compact/full P3 header.
                p2_end = p2_start + 1024

                # Template palette table constant (32 bytes)
                # ASCII: skin/hair/eyes/accent with category metadata
                template_table = bytes.fromhex(
                    "04 73 6B 69 6E 00 00 07"
                    "68 61 69 72 00 07 03"
                    "65 79 65 73 00 0A 02"
                    "61 63 63 65 6E 74 00 0C 01"
                    "FF"
                )

                p3_header_start = p2_end
                if all_data[p2_end : p2_end + 32] == template_table:
                    p3_header_start = p2_end + 32
                    print(f"[{vxm.basename}] Template palette table detected between P2 and P3 (32 bytes).")

                p3_layouts = []
                # Compact P3 header is [0x00][count], followed by count * (BGRA + flag).
                p3_header = all_data[p3_header_start : p3_header_start + 2]
                if len(p3_header) == 2 and p3_header[0] == 0 and 1 <= p3_header[1] <= 255:
                    p3_layouts.append((p3_header_start + 2, p3_header[1]))
                # Some template-palette VXM files serialize the full active P3 table directly.
                p3_layouts.append((p3_header_start, 255))

                p1_entries = [p1_cand[i*4 : i*4+4] for i in range(256)]
                p2_entries = [p2_cand[i*4 : i*4+4] for i in range(256)]

                for p3_start, p3_count in p3_layouts:
                    p3_len = p3_count * 5
                    p3_cand = all_data[p3_start : p3_start + p3_len]
                    if len(p3_cand) != p3_len:
                        continue

                    # Compact files only serialize the active palette prefix in P3.
                    if any(entry == sig for entry in p1_entries[:p3_count]):
                        continue
                    if p3_count < 255 and any(entry != sig for entry in p1_entries[p3_count:255]):
                        continue

                    # Validate P3 and Extract Emissive Indices
                    is_p3_valid = True
                    emissive_indices = set()

                    for k in range(p3_count):
                        # P1 is RGBA
                        r,g,b,a = p1_entries[k]
                        # P3 is BGRA + Flag
                        b_p3, g_p3, r_p3, a_p3 = p3_cand[k*5 : k*5+4]
                        flag = p3_cand[k*5+4]

                        if (r,g,b,a) != (r_p3, g_p3, b_p3, a_p3):
                            is_p3_valid = False; break
                        if flag not in (0, 1):
                            is_p3_valid = False; break

                        if flag == 1:
                            emissive_indices.add(k)

                    if not is_p3_valid: continue

                    # Final Strict P2 & P3 Consistency Check
                    is_p2_valid = True
                    for i in range(p3_count):
                        rgba_p2 = p2_entries[i]
                        if i in emissive_indices:
                            # Emissive: P2 must match P1
                            rgba_p1 = p1_entries[i]
                            if rgba_p2 != rgba_p1:
                                is_p2_valid = False
                                # print(f"DEBUG: Mismatch at P2 Emissive Index {i}")
                                break
                        else:
                            # Non-emissive active palette slots are black in P2.
                            if rgba_p2 != black:
                                is_p2_valid = False
                                # print(f"DEBUG: Mismatch at P2 Non-Emissive (Should be Black) Index {i}")
                                break

                    if is_p2_valid and p3_count < 255:
                        for rgba_p2 in p2_entries[p3_count:255]:
                            if rgba_p2 != sig:
                                is_p2_valid = False
                                break

                    if not is_p2_valid: continue

                    valid_candidates.append((p1_start, p3_start, p3_count))
            
            if len(valid_candidates) == 0:
                 raise ValueError(f"[{vxm.basename}] No valid P1(RGBA)/P2/P3(BGRA) structure found.")
            if len(valid_candidates) > 1:
                raise ValueError(f"[{vxm.basename}] Multiple ambiguous P1/P2/P3 structures found.")
            
            p1_start, p3_start, p3_count = valid_candidates[0]
            
            p1_bytes = all_data[p1_start : p1_start + 1024]
            p3_len = p3_count * 5
            p3_bytes = all_data[p3_start : p3_start + p3_len]
            
            for i in range(256):
                 r, g, b, a = p1_bytes[i*4:i*4+4]
                 vxm.palette_rgba[i] = (r,g,b,a)
            
            for i in range(p3_count):
                vxm.palette_emissive_flag[i] = (p3_bytes[i*5+4] != 0)
            for i in range(p3_count, 255):
                vxm.palette_emissive_flag[i] = False
            vxm.palette_emissive_flag[255] = False
            
            layer_block_start = p3_start + p3_len
            f.seek(layer_block_start)
            
            try:
                lc_byte = f.read(1)
                layer_count = ord(lc_byte) if lc_byte else 0
            except: layer_count = 0
            
            for _ in range(layer_count):
                while True:
                    c = f.read(1)
                    if not c or c == b'\x00': break
                is_on = (f.read(1) == b'\x01')
                decoded_indices = []
                rle_decoded_len = 0
                while True:
                    len_b = f.read(1)
                    if not len_b: break
                    length = len_b[0]
                    if length == 0:
                        pos = f.tell()
                        term = f.read(1)
                        if term != b'\x00': f.seek(pos)
                        break
                    idx_b = f.read(1)
                    if not idx_b: break 
                    idx = idx_b[0]
                    decoded_indices.extend([idx] * length)
                    rle_decoded_len += length
                
                if rle_decoded_len != total_voxels:
                    raise ValueError(f"[{vxm.basename}] RLE len {rle_decoded_len} != {total_voxels}. Corrupted.")
                
                if is_on:
                    for i, color_idx in enumerate(decoded_indices):
                        if color_idx == 0xFF: continue
                        if color_idx >= 256: continue
                        z = i % sizeZ
                        rem = i // sizeZ
                        y = rem % sizeY
                        x = rem // sizeY
                        vxm.voxel_grid[(x, y, z)] = (vxm.palette_rgba[color_idx], vxm.palette_emissive_flag[color_idx])
            
            p3_em = sum(vxm.palette_emissive_flag)
            grid_em = sum(1 for _, is_em in vxm.voxel_grid.values() if is_em)
            if p3_em > 0:
                 print(f"  [{vxm.basename}] Emissive Flags: {p3_em}, Used Voxels: {grid_em}")
                            
        return vxm

# ==================================================================================
# Palette Unification
# ==================================================================================

def median_cut_quantization(colors, max_colors):
    if not colors: return []
    if len(colors) <= max_colors: return list(colors)
    def get_max_range(bucket):
        if not bucket: return -1
        r=[c[0] for c in bucket]; g=[c[1] for c in bucket]; b=[c[2] for c in bucket]
        return max(max(r)-min(r), max(g)-min(g), max(b)-min(b))
    buckets = [list(colors)]
    while len(buckets) < max_colors:
        best_i = -1; max_rng = -1
        for i, b in enumerate(buckets):
            rng = get_max_range(b)
            if rng > max_rng: max_rng = rng; best_i = i
        if best_i == -1 or max_rng <= 0: break
        bucket = buckets.pop(best_i)
        r=[c[0] for c in bucket]; g=[c[1] for c in bucket]; b=[c[2] for c in bucket]
        rr=max(r)-min(r); gg=max(g)-min(g); bb=max(b)-min(b)
        axis = 0
        if gg >= rr and gg >= bb: axis = 1
        elif bb >= rr and bb >= gg: axis = 2
        bucket.sort(key=lambda c: c[axis])
        mid = len(bucket)//2
        buckets.append(bucket[:mid])
        buckets.append(bucket[mid:])
    final_pal = []
    for b in buckets:
        if not b: continue
        sr=sum(c[0] for c in b); sg=sum(c[1] for c in b); sb=sum(c[2] for c in b)
        n = len(b)
        final_pal.append((int(sr/n), int(sg/n), int(sb/n), 255))
    return final_pal

def generate_unified_palette(all_vxm_data):
    normal_colors = set()
    emissive_colors = set()
    for vxm in all_vxm_data:
        for (rgba, is_em) in vxm.voxel_grid.values():
            if is_em: emissive_colors.add(rgba)
            else: normal_colors.add(rgba)
    total_slots = 255
    nn = len(normal_colors); ne = len(emissive_colors)
    if nn + ne <= total_slots: max_n = nn; max_e = ne
    else:
        ratio = 0.5 if nn+ne==0 else nn/(nn+ne)
        max_n = max(1, int(total_slots * ratio))
        max_e = total_slots - max_n
    p_norm = median_cut_quantization(list(normal_colors), max_n)
    p_emit = median_cut_quantization(list(emissive_colors), max_e)
        
    unified_palette = p_norm + p_emit
    unified_is_emissive = [False]*len(p_norm) + [True]*len(p_emit)
    return unified_palette, unified_is_emissive

def color_distance_sq(c1, c2):
    return (c1[0]-c2[0])**2 + (c1[1]-c2[1])**2 + (c1[2]-c2[2])**2

# ==================================================================================
# VOX Writer (Spec B & A)
# ==================================================================================

class VoxWriter:
    def write(self, filepath, basename, size, voxel_grid, unified_palette, unified_is_emissive, color_map_cache):
        sizeX, sizeY, sizeZ = size

        # VoxEdit's VOX has VXM's (X, Y, Z) in (X, Z, Y) order
        vox_sizeX = sizeX
        vox_sizeY = sizeZ
        vox_sizeZ = sizeY
        
        with open(filepath, 'wb') as f:
            f.write(b'VOX ')
            f.write(struct.pack('<I', 150))
            
            chunks_buffer = bytearray()
            
            # 1. SIZE
            sizeContent = struct.pack('<III', vox_sizeX, vox_sizeY, vox_sizeZ)
            chunks_buffer.extend(b'SIZE')
            chunks_buffer.extend(struct.pack('<II', len(sizeContent), 0))
            chunks_buffer.extend(sizeContent)
            
            # 2. XYZI
            xyzi_records = bytearray()
            num_voxels = 0
            used_indices = set()
            
            for (x, y, z), (rgba, is_em) in voxel_grid.items():
                vox_x, vox_y, vox_z = transform_coord(x, y, z, size[0], size[1], size[2])
                
                if not (0 <= vox_x < 256 and 0 <= vox_y < 256 and 0 <= vox_z < 256):
                     continue 

                key = (rgba[0], rgba[1], rgba[2], rgba[3], is_em)
                if key in color_map_cache: idx = color_map_cache[key]
                else: 
                     best_idx=0; min_d=float('inf'); found=False
                     for i, (pal_c, pal_em) in enumerate(zip(unified_palette, unified_is_emissive)):
                         if pal_em == is_em:
                             d = color_distance_sq(rgba, pal_c)
                             if d < min_d: 
                                 min_d = d; best_idx = i; found = True; 
                                 if d == 0: break
                     idx = best_idx if found else 0
                     color_map_cache[key] = idx
                
                used_indices.add(idx)
                xyzi_records.extend(struct.pack('BBBB', vox_x, vox_y, vox_z, idx + 1))
                num_voxels += 1
            
            xyziContent = struct.pack('<I', num_voxels) + xyzi_records
            chunks_buffer.extend(b'XYZI')
            chunks_buffer.extend(struct.pack('<II', len(xyziContent), 0))
            chunks_buffer.extend(xyziContent)
            
            # Spec B: Scene Graph (Fixed Reserved Fields)
            
            # nTRN Root
            t0_content = bytearray()
            t0_content.extend(struct.pack('<I', 0)) # ID
            t0_content.extend(struct.pack('<I', 0)) # Attrs Dict Size
            t0_content.extend(struct.pack('<I', 1)) # Child ID
            t0_content.extend(struct.pack('<I', 0xFFFFFFFF)) # Reserved
            t0_content.extend(struct.pack('<I', 0)) # Layer
            t0_content.extend(struct.pack('<I', 1)) # 1 Frame
            t0_content.extend(struct.pack('<I', 0)) # Frame Attrs size
            
            chunks_buffer.extend(b'nTRN')
            chunks_buffer.extend(struct.pack('<II', len(t0_content), 0))
            chunks_buffer.extend(t0_content)
            
            # nGRP Group
            g1_content = bytearray()
            g1_content.extend(struct.pack('<I', 1)) # ID
            g1_content.extend(struct.pack('<I', 0)) # Attrs
            g1_content.extend(struct.pack('<I', 1)) # Num Children
            g1_content.extend(struct.pack('<I', 2)) # Child ID 2
            
            chunks_buffer.extend(b'nGRP')
            chunks_buffer.extend(struct.pack('<II', len(g1_content), 0))
            chunks_buffer.extend(g1_content)
            
            # nTRN Model
            t2_content = bytearray()
            t2_content.extend(struct.pack('<I', 2)) # ID
            name_bytes = basename.encode('ascii', errors='ignore')
            t2_attrs = bytearray()
            t2_attrs.extend(struct.pack('<I', 5)); t2_attrs.extend(b'_name')
            t2_attrs.extend(struct.pack('<I', len(name_bytes))); t2_attrs.extend(name_bytes)
            
            t2_content.extend(struct.pack('<I', 1)) # 1 Pair
            t2_content.extend(t2_attrs)
            
            t2_content.extend(struct.pack('<I', 3)) # Child ID
            t2_content.extend(struct.pack('<I', 0xFFFFFFFF)) # Reserved
            t2_content.extend(struct.pack('<I', 0))
            t2_content.extend(struct.pack('<I', 1))
            t2_content.extend(struct.pack('<I', 0))
            
            chunks_buffer.extend(b'nTRN')
            chunks_buffer.extend(struct.pack('<II', len(t2_content), 0))
            chunks_buffer.extend(t2_content)
            
            # nSHP Shape
            s3_content = bytearray()
            s3_content.extend(struct.pack('<I', 3)) # ID
            s3_content.extend(struct.pack('<I', 0)) # Attrs
            s3_content.extend(struct.pack('<I', 1)) # Num Models
            s3_content.extend(struct.pack('<I', 0)) # Model ID 0
            s3_content.extend(struct.pack('<I', 0)) # Model Attrs
            
            chunks_buffer.extend(b'nSHP')
            chunks_buffer.extend(struct.pack('<II', len(s3_content), 0))
            chunks_buffer.extend(s3_content)
            
            # RGBA
            rgbaContent = bytearray()
            for c in unified_palette:
                rgbaContent.extend(struct.pack('BBBB', c[0], c[1], c[2], c[3]))
            pad = 255 - len(unified_palette)
            for _ in range(pad): rgbaContent.extend(b'\x00\x00\x00\xFF')
            rgbaContent.extend(struct.pack('BBBB', 255, 0, 255, 255))
            chunks_buffer.extend(b'RGBA')
            chunks_buffer.extend(struct.pack('<II', len(rgbaContent), 0))
            chunks_buffer.extend(rgbaContent)
            
            # MATL (Spec A, C) - Global consistency
            for i, is_em in enumerate(unified_is_emissive):
                if is_em:
                    # Global consistency: Write MATL even if not used in this specific VOX
                    mat_id = i + 1
                    props_dict = {
                        b'_type': b'_emit',
                        b'_emit': b'0.5',
                        b'_flux': b'2', 
                        b'_ldr': b'0.5' 
                    }
                    mat_content = bytearray()
                    mat_content.extend(struct.pack('<I', mat_id))
                    mat_content.extend(struct.pack('<I', len(props_dict)))
                    for k, v in props_dict.items():
                        mat_content.extend(struct.pack('<I', len(k)))
                        mat_content.extend(k)
                        mat_content.extend(struct.pack('<I', len(v)))
                        mat_content.extend(v)
                    chunks_buffer.extend(b'MATL')
                    chunks_buffer.extend(struct.pack('<II', len(mat_content), 0))
                    chunks_buffer.extend(mat_content)
            
            f.write(b'MAIN')
            f.write(struct.pack('<II', 0, len(chunks_buffer)))
            f.write(chunks_buffer)

# ==================================================================================
# Main
# ==================================================================================

def collect_referenced_vxm_names(vxr_txt: Path):
    if not vxr_txt or not vxr_txt.exists():
        return None

    used_full = set()
    used_stem = set()
    section = None

    with vxr_txt.open("r", encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.strip()
            if stripped == "# Node Hierarchy":
                section = "HIERARCHY"
                continue
            if stripped == "# Animation":
                break
            if section != "HIERARCHY" or " / " not in stripped:
                continue

            content = stripped
            if content.startswith("└"):
                content = content[1:].strip()

            _, vxm_part = content.split(" / ", 1)
            vxm_name = os.path.basename(vxm_part.strip())
            if not vxm_name or not vxm_name.lower().endswith(".vxm"):
                continue

            used_full.add(vxm_name.lower())
            used_stem.add(Path(vxm_name).stem.lower())

    if not used_full:
        return None
    return used_full, used_stem


def select_vxm_files(project_dir: Path, vxr_txt: Path = None):
    vxm_files = sorted(project_dir.glob("*.vxm"))
    if not vxm_files:
        raise FileNotFoundError("No .vxm files found in project_dir.")

    referenced = collect_referenced_vxm_names(vxr_txt)
    if not referenced:
        if vxr_txt and vxr_txt.exists():
            print(f"[VXM Filter] No referenced .vxm entries found in {vxr_txt.name}; using all {len(vxm_files)} files.")
        else:
            print(f"[VXM Filter] {vxr_txt.name if vxr_txt else 'VXR&VXA.txt'} not found; using all {len(vxm_files)} files.")
        return vxm_files

    used_full, used_stem = referenced
    filtered = [p for p in vxm_files if p.name.lower() in used_full or p.stem.lower() in used_stem]
    if not filtered:
        print(f"[VXM Filter] Referenced .vxm names did not match local files; using all {len(vxm_files)} files.")
        return vxm_files

    skipped = len(vxm_files) - len(filtered)
    print(f"[VXM Filter] Using {len(filtered)}/{len(vxm_files)} .vxm files referenced by {vxr_txt.name}.")
    if skipped:
        print(f"[VXM Filter] Skipping {skipped} unreferenced .vxm files.")

    available_names = {p.name.lower() for p in vxm_files}
    missing = sorted(name for name in used_full if name not in available_names)
    if missing:
        preview = ", ".join(missing[:10])
        suffix = " ..." if len(missing) > 10 else ""
        print(f"[VXM Filter] Warning: {len(missing)} referenced .vxm files were not found locally: {preview}{suffix}")

    return filtered


def convert_vxm_folder(project_dir: Path, out_vox_dir: Path, pivot_txt: Path, vxr_txt: Path = None) -> None:
    """
    project_dir 内の対象 .vxm を out_vox_dir に .vox として出力し、
    ピボット情報を pivot_txt に書き出す。

    - .vxm が1つもない場合は FileNotFoundError
    - out_vox_dir / pivot_txt の親フォルダは自動作成
    """
    out_vox_dir.mkdir(parents=True, exist_ok=True)

    for stale_vox in out_vox_dir.glob("*.vox"):
        stale_vox.unlink()

    vxm_files = select_vxm_files(project_dir, vxr_txt)

    parser = VxmParser()
    all_vxm_data = []
    
    for path in vxm_files:
        # 既存 main() 内のロジックから「入力/出力フォルダ周り」を書き換える形で使用
        data = parser.parse(str(path))
        all_vxm_data.append(data)

    unified_palette, unified_is_emissive = generate_unified_palette(all_vxm_data)
    
    color_map_cache = {}
    for i, (c, em) in enumerate(zip(unified_palette, unified_is_emissive)):
        color_map_cache[(c[0], c[1], c[2], c[3], em)] = i

    writer = VoxWriter()
    pivot_lines = []
    
    for vxm in all_vxm_data:
        out_name = out_vox_dir / f"{vxm.basename}.vox"
        writer.write(
            str(out_name),
            vxm.basename,
            vxm.size,
            vxm.voxel_grid,
            unified_palette,
            unified_is_emissive,
            color_map_cache,
        )
        pivot_lines.append(f"{vxm.basename}.vxm: {vxm.pivot[0]} {vxm.pivot[1]} {vxm.pivot[2]}")

    pivot_txt.parent.mkdir(parents=True, exist_ok=True)
    with pivot_txt.open("w", encoding="utf-8") as f:
        f.write("\n".join(pivot_lines))


def run_ve2rbx(project_dir: Path, edit_dir: Path) -> None:
    """
    VE2RBX ランチャー用のエントリ。
    edit_dir/OutputVOX に .vox、
    edit_dir/Pivot.txt にピボット情報を出力する。
    """
    out_vox_dir = edit_dir / "OutputVOX"
    pivot_txt = edit_dir / "Pivot.txt"
    vxr_txt = edit_dir / "VXR&VXA.txt"
    convert_vxm_folder(project_dir, out_vox_dir, pivot_txt, vxr_txt)



import sys
import argparse
import traceback

def setup_logger(log_path: str):
    if not log_path:
        return
    try:
        # Signature
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write("Created by KisaragiKoubou\n")
        
        log_file = open(log_path, 'a', encoding='utf-8')
        
        class DualWriter:
            def __init__(self, file, original):
                self.file = file
                self.original = original

            def write(self, text):
                # original が None のケース（windowed exe 等）を許容
                try:
                    if self.original and hasattr(self.original, "write"):
                        self.original.write(text)
                except Exception:
                    pass

                try:
                    self.file.write(text)
                    self.file.flush()
                except Exception:
                    pass

            def flush(self):
                try:
                    if self.original and hasattr(self.original, "flush"):
                        self.original.flush()
                except Exception:
                    pass

                try:
                    self.file.flush()
                except Exception:
                    pass

        # None を握らない: フォールバックとして __stdout__/__stderr__ を使う
        stdout_orig = sys.stdout if sys.stdout is not None else sys.__stdout__
        stderr_orig = sys.stderr if sys.stderr is not None else sys.__stderr__

        sys.stdout = DualWriter(log_file, stdout_orig)
        sys.stderr = DualWriter(log_file, stderr_orig)

    except Exception as e:
        try:
            if sys.__stderr__ and hasattr(sys.__stderr__, "write"):
                sys.__stderr__.write(f"Failed to setup logging: {e}\n")
                sys.__stderr__.flush()
        except Exception:
            pass

def main():
    parser = argparse.ArgumentParser(description="VXM to VOX Converter")
    parser.add_argument("dir", nargs="?", help="Project Directory", default=os.getcwd())
    parser.add_argument("--edit_dir", help="Output directory for VE2RBX pipeline", default=None)
    parser.add_argument("--log_path", help="Path to log file", default=None)
    
    args = parser.parse_args()
    
    # Resolve Directory
    project_dir = Path(args.dir).resolve()
    
    # Determine Edit Dir (Strict Priority)
    edit_dir = None
    if args.edit_dir:
        edit_dir = Path(args.edit_dir)
    
    # Logger Setup
    log_path = args.log_path
    if not log_path and edit_dir:
        # Default fixed name if edit_dir is known
        log_dir = edit_dir / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / "vxm2vox.log")
        
    setup_logger(log_path)
    
    print("--- VXM to VOX Converter (Production v5.8) ---")

    # Standalone Fallback Helpers (Only if edit_dir unavailable)
    if not edit_dir:
        # Standalone Centralized Logic Helpers
        import re
        import datetime

        def sanitize_name_local(name: str) -> str:
            safe = re.sub(r'[<>:"/\\|?*]', '_', name)
            safe = safe.rstrip(". ")
            if not safe: safe = "Unnamed"
            return safe

        def create_edit_dir_local(project_dir: Path) -> Path:
            user_docs = Path(os.path.expandvars(r"%USERPROFILE%\Documents"))
            base_root = user_docs / "VE2RBX"
            name_safe = sanitize_name_local(project_dir.name)
            pj_root = base_root / name_safe
            if not pj_root.exists(): pj_root.mkdir(parents=True, exist_ok=True)
            
            now = datetime.datetime.now()
            timestamp = now.strftime("%Y.%m.%d.%H%M%S")
            run_name = f"{name_safe} {timestamp}"
            edit_dir = pj_root / run_name
            
            if edit_dir.exists():
                for i in range(1, 100):
                    sf = f"_{i:02d}"
                    cand = pj_root / f"{run_name}{sf}"
                    if not cand.exists():
                        edit_dir = cand
                        break
            edit_dir.mkdir(parents=True, exist_ok=True)
            return edit_dir
            
        print("Standalone Mode: Generating centralized edit_dir...")
        
        # Smart Input Detection for Standalone
        input_dir = project_dir
        if not list(input_dir.glob("*.vxm")):
            candidates = list(input_dir.glob("OutputVXM*"))
            if candidates:
                input_dir = sorted(candidates)[-1]
                print(f"Auto-detected input subfolder: {input_dir.name}")
        
        edit_dir = create_edit_dir_local(project_dir)
        print(f"Edit Dir Created: {edit_dir}")
        
        # Setup Standalone Logger
        log_dir = edit_dir / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        local_log = log_dir / "vxm2vox.log"
        setup_logger(str(local_log))
        print(f"Log started at: {local_log}")

        # Run conversion
        out_vox_dir = edit_dir / "OutputVOX"
        pivot_txt = edit_dir / "Pivot.txt"
        
        print(f"Input: {input_dir}")
        print(f"Output VOX: {out_vox_dir}")
        
        try:
            convert_vxm_folder(input_dir, out_vox_dir, pivot_txt)
            print("--- Completed ---")
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
            
    else:
        # WE HAVE EDIT DIR (VE2RBX Mode or Explicit Args)
        try:
            run_ve2rbx(project_dir, edit_dir)
            print("--- Completed ---")
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

if __name__ == "__main__":
    main()
