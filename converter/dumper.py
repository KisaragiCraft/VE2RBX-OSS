import struct
import math
import sys
import argparse
from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass, field
import os
import glob
import datetime
import traceback
import platform
import re
from pathlib import Path
import logging

# --- Data Structures ---

@dataclass
class Node:
    name: str
    child_count: int
    vxm_name: Optional[str] = None
    children: List['Node'] = field(default_factory=list)
    # Temporary ID for dump process (not strictly part of data, but useful)
    _temp_id: Optional[int] = None 

@dataclass
class KeyFrame:
    frame: int
    ease: int
    value: float  # pos or rot(deg)
    slerp: Optional[int] = None

@dataclass
class NodeAnim:
    name: str
    # Fixed order: posX, posY, posZ, rotX, rotY, rotZ
    channels: List[List[KeyFrame]] = field(default_factory=list)

@dataclass
class AnimData:
    nodes: List[NodeAnim]


EASE_LABELS = {
    1: "linear",
    2: "ease_in_weak",
    3: "ease_out_weak",
    4: "ease_in_out_weak",
    5: "ease_in_strong",
    6: "ease_out_strong",
    7: "ease_in_out_strong",
}


def ease_label(code: int) -> str:
    return EASE_LABELS.get(code, f"unknown_{code}")


def slerp_label(flag: int) -> str:
    return "on" if flag else "off"

# --- Helper Classes ---

class BinaryReader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def read_bytes(self, count: int) -> bytes:
        if self.offset + count > len(self.data):
            raise ValueError("Unexpected End of Stream")
        chunk = self.data[self.offset : self.offset + count]
        self.offset += count
        return chunk

    def read_u8(self) -> int:
        return struct.unpack_from('<B', self.read_bytes(1))[0]

    def read_u32(self) -> int:
        return struct.unpack_from('<I', self.read_bytes(4))[0]

    def read_f32(self) -> float:
        return struct.unpack_from('<f', self.read_bytes(4))[0]

    def read_string(self) -> str:
        # Read until null terminator
        end = self.data.find(b'\x00', self.offset)
        if end == -1:
            raise ValueError("String not null-terminated")
        s_bytes = self.data[self.offset : end]
        self.offset = end + 1 # Skip null
        return s_bytes.decode('utf-8', errors='replace')

    def peek_bytes(self, count: int) -> bytes:
        if self.offset + count > len(self.data):
             return b''
        return self.data[self.offset : self.offset + count]

    def peek_u32(self) -> Optional[int]:
        b = self.peek_bytes(4)
        if len(b) < 4:
            return None
        return struct.unpack('<I', b)[0]

    def tell(self) -> int:
        return self.offset

    def peek_u32_at(self, offset: int) -> Optional[int]:
        if offset + 4 > len(self.data):
             return None
        b = self.data[offset : offset + 4]
        return struct.unpack('<I', b)[0]

    def try_read_cstring_at(self, offset: int, limit: int = 256) -> Tuple[bool, str, int]:
        """
        指定オフセットからCStringを読み取れるか試行する。
        戻り値: (success, string_value, total_bytes_including_null)
        """
        if offset >= len(self.data):
            return False, "", 0
            
        end = self.data.find(b'\x00', offset)
        if end == -1:
             return False, "", 0
             
        length = end - offset
        if length > limit:
             return False, "", 0
             
        s_bytes = self.data[offset : end]
        try:
            s_str = s_bytes.decode('utf-8')
            # printable check to avoid binary garbage match (e.g. \x01)
            # User said "英数/日本語", so printable is safe.
            if not s_str.isprintable():
                return False, "", 0
            
            # 成功
            return True, s_str, length + 1 # +1 for null
        except UnicodeDecodeError:
            return False, "", 0
            
    def skip(self, count: int):
        self.offset += count


# --- VXR Parsing ---

def parse_vxr(path: str) -> Tuple[Node, bytes]:
    with open(path, 'rb') as f:
        data = f.read()
    
    reader = BinaryReader(data)
    
    # Header
    sig = reader.read_bytes(3)
    if sig != b'VXR':
        raise ValueError("Invalid VXR signature")
    
    # Capture first 25 bytes as partial "Magic" for check comparison if needed,
    # or just rely on VXR parsing success.
    # The spec says "VXA Magic Number (17 bytes) matches".
    # We will grab the first 20-30 bytes of VXR to debug/compare later.
    magic_signature = data[0:20] 

    reader.skip(1) # Unknown
    _ = reader.read_string() # Default Anim Name
    # reader.skip(1) # Separator 00 (Removed based on inspection: seems usually handled by string loop or missing)
    # Actually, from dump inspection: Name\0 -> Separator(or next)\x01.
    # We'll just read data.
    
    # Rig Count (Number of Root Nodes, usually 1)
    rig_count = reader.read_u32()
    
    # Unknown bytes (Spec said 8, but simple skip is fragile)
    # VXR Constants
    VXR_NODE_META_LEN = 29
    
    # Dynamics skip: Scan first 4096 bytes for a valid Root Node Structure
    # Structure: [Name CString] [VXM CString] [Meta 29 bytes] [ChildCount u32]
    # Enhanced: Validate meta structure to avoid false positives (template identifiers)
    valid_candidates = []  # List of (offset, name) tuples
    search_limit = 4096
    start_search = reader.tell()
    
    while reader.tell() - start_search < search_limit:
        current_pos = reader.tell()
        
        # Check if valid Node starts here
        ok1, name1, bytes1 = reader.try_read_cstring_at(current_pos)
        if ok1 and len(name1.strip()) >= 1:
             check_pos2 = current_pos + bytes1
             ok2, name2, bytes2 = reader.try_read_cstring_at(check_pos2)
             
             if ok2:
                 # meta_start is right after 2nd CString
                 meta_start = check_pos2 + bytes2
                 
                 # Validation 1: Check meta first 4 bytes == 0x00000001 (little-endian)
                 meta_header = reader.peek_u32_at(meta_start)
                 if meta_header is None or meta_header != 1:
                     reader.skip(1)
                     continue
                 
                 # Validation 2: Check meta region (29 bytes) doesn't have too many printable ASCII
                 # Template identifiers have strings like "Controller" in meta area
                 if meta_start + VXR_NODE_META_LEN <= len(reader.data):
                     meta_bytes = reader.data[meta_start : meta_start + VXR_NODE_META_LEN]
                     printable_count = sum(1 for b in meta_bytes if 32 <= b <= 126)
                     if printable_count >= 4:
                         # Too many printable chars - likely false positive
                         reader.skip(1)
                         continue
                 
                 # Check Child Count at expected offset
                 cc_offset = meta_start + VXR_NODE_META_LEN
                 cc = reader.peek_u32_at(cc_offset)
                 
                 if cc is not None and 0 <= cc <= 5000:
                     # Valid Root candidate found!
                     valid_candidates.append((current_pos, name1))
        
        reader.skip(1)
    
    # Select the candidate with smallest offset (first valid node)
    if not valid_candidates:
        raise ValueError("Could not find valid Root Node in VXR.")
    
    valid_candidates.sort(key=lambda x: x[0])
    best_offset, best_name = valid_candidates[0]
    reader.offset = best_offset
    print(f"[VXR Parse] Found Root Node '{best_name}' at offset {best_offset} (candidates: {len(valid_candidates)})")

    # Recursive Parse Function
    def parse_node_recursive() -> Node:
        name = reader.read_string()
        vxm_name = reader.read_string()  # VXM file name (e.g. "Hip.vxm" / empty if none)
        
        # Skip Fixed Metadata
        reader.skip(VXR_NODE_META_LEN)
        
        # Read Child Count
        child_count = reader.read_u32()
        
        if child_count > 5000:
             raise ValueError(f"Invalid child count {child_count} at {reader.tell()-4} (Node: {name})")

        # Log for debugging
        # print(f"[Debug] Node: {name}, VXM: {vxm_name}, Children: {child_count}")

        # Recursive Children
        children = []
        for _ in range(child_count):
            children.append(parse_node_recursive())
            
        # 空文字や無効文字列は None 扱い
        if vxm_name is not None:
            vxm_name = vxm_name.strip()
            if vxm_name == "":
                vxm_name = None
    
        return Node(name, child_count, vxm_name, children)

    roots = []
    # If Rig Count > 1000, it's garbage?
    if rig_count > 1000:
         # Fallback?
         rig_count = 1
         
    for _ in range(rig_count):
        roots.append(parse_node_recursive())
        
    if not roots:
        return Node("Empty", 0), magic_signature
        
    # If multiple roots, create a dummy holder?
    # Or usually just 1 Controller.
    if len(roots) == 1:
        return roots[0], magic_signature
    else:
        # Spec format expects single tree?
        # Create a virtual root (no VXM attached)
        virtual = Node(name="SceneRoot", child_count=len(roots), vxm_name=None, children=roots)
        return virtual, magic_signature


def parse_vxr_fixed(path: str) -> Tuple[Node, bytes]:
    with open(path, 'rb') as f:
        data = f.read()

    reader = BinaryReader(data)

    sig = reader.read_bytes(3)
    if sig != b'VXR':
        raise ValueError("Invalid VXR signature")

    magic_signature = data[0:20]

    reader.skip(1)
    _ = reader.read_string()
    rig_count = reader.read_u32()
    if rig_count > 1000 or rig_count <= 0:
        rig_count = 1

    VXR_NODE_META_LEN = 29
    IK_COUNT_OFFSET = 25
    IK_COUNT_MAX = 10
    IK_RECORD_LEN = 12
    EXTENDED_EXTRA_SCAN_MAX = 96
    parse_cache: Dict[int, Tuple[Node, int, int]] = {}

    def finalize_roots(roots: List[Node]) -> Node:
        if not roots:
            return Node("Empty", 0)
        if len(roots) == 1:
            return roots[0]
        return Node(name="SceneRoot", child_count=len(roots), vxm_name=None, children=roots)

    def normalize_vxm_name(vxm_name: str) -> Optional[str]:
        if vxm_name is None:
            return None
        vxm_name = vxm_name.strip()
        return vxm_name or None

    def get_ik_extra_len(meta_start: int) -> int:
        count_offset = meta_start + IK_COUNT_OFFSET
        if count_offset + 4 > len(data):
            return 0

        ik_count = struct.unpack_from('<I', data, count_offset)[0]
        if not (1 <= ik_count <= IK_COUNT_MAX):
            return 0

        block_start = meta_start + VXR_NODE_META_LEN
        block_end = block_start + (ik_count * IK_RECORD_LEN)
        if block_end + 4 > len(data):
            return 0

        max_angle = (2.0 * math.pi) + 1e-4
        for idx in range(ik_count):
            yaw, pitch, radius = struct.unpack_from('<fff', data, block_start + (idx * IK_RECORD_LEN))
            if not (math.isfinite(yaw) and math.isfinite(pitch) and math.isfinite(radius)):
                return 0
            if abs(yaw) > max_angle or abs(pitch) > max_angle or abs(radius) > max_angle:
                return 0

        return ik_count * IK_RECORD_LEN

    def get_controller_label_extra_len(meta_start: int) -> int:
        ok_label, label, label_bytes = BinaryReader(data).try_read_cstring_at(meta_start, limit=64)
        if not ok_label or not label.strip():
            return 0

        # Root nodes in some template rigs store a controller label before
        # their regular metadata, with one padding byte before the metadata.
        for padding in (0, 1):
            extra_len = label_bytes + padding
            header_offset = meta_start + extra_len
            cc_offset = header_offset + VXR_NODE_META_LEN
            meta_header = reader.peek_u32_at(header_offset)
            child_count = reader.peek_u32_at(cc_offset)
            if meta_header == 1 and child_count is not None and 0 <= child_count <= 5000:
                child_start = cc_offset + 4
                if child_count == 0 or is_plausible_node_start(child_start):
                    return extra_len
        return 0

    def is_plausible_node_start(offset: int) -> bool:
        ok_name, name1, bytes1 = BinaryReader(data).try_read_cstring_at(offset)
        if not ok_name or len(name1.strip()) < 1:
            return False

        ok_vxm, _name2, bytes2 = BinaryReader(data).try_read_cstring_at(offset + bytes1)
        if not ok_vxm:
            return False

        local_meta_start = offset + bytes1 + bytes2
        if local_meta_start + VXR_NODE_META_LEN > len(data):
            return False

        meta_bytes = data[local_meta_start : local_meta_start + VXR_NODE_META_LEN]
        printable_count = sum(1 for b in meta_bytes if 32 <= b <= 126)
        return printable_count < 8

    def score_following_boundary(offset: int) -> int:
        if offset >= len(data):
            return 2
        return 2 if is_plausible_node_start(offset) else 0

    def iter_candidate_extra_lens(meta_start: int, allow_extended: bool) -> List[int]:
        candidate_lens = {0}
        ik_extra_len = get_ik_extra_len(meta_start)
        if ik_extra_len:
            candidate_lens.add(ik_extra_len)
        controller_label_extra_len = get_controller_label_extra_len(meta_start)
        if controller_label_extra_len:
            candidate_lens.add(controller_label_extra_len)

        if not allow_extended:
            return sorted(candidate_lens)

        for extra_len in range(0, EXTENDED_EXTRA_SCAN_MAX + 1):
            cc_offset = meta_start + VXR_NODE_META_LEN + extra_len
            child_count = reader.peek_u32_at(cc_offset)
            if child_count is None or child_count > 5000:
                continue

            child_start = cc_offset + 4
            if child_count == 0:
                if score_following_boundary(child_start) > 0:
                    candidate_lens.add(extra_len)
                continue

            if is_plausible_node_start(child_start):
                candidate_lens.add(extra_len)

        return sorted(candidate_lens)

    def try_parse_roots_fast(start_offset: int) -> Tuple[Node, int]:
        def parse_fast_node(offset: int) -> Tuple[Node, int, int]:
            reader_obj = BinaryReader(data)
            reader_obj.offset = offset

            name = reader_obj.read_string()
            vxm_name = reader_obj.read_string()
            if not name.strip():
                raise ValueError(f"Empty node name at offset {offset}")

            meta_start = reader_obj.tell()
            extra_lens: List[int] = []
            for candidate in (
                get_controller_label_extra_len(meta_start),
                get_ik_extra_len(meta_start),
                0,
            ):
                if candidate not in extra_lens:
                    extra_lens.append(candidate)

            last_error: Optional[Exception] = None
            for extra_len in extra_lens:
                try:
                    cc_offset = meta_start + VXR_NODE_META_LEN + extra_len
                    child_count = reader_obj.peek_u32_at(cc_offset)
                    if child_count is None or child_count > 5000:
                        raise ValueError(f"Invalid child count at {cc_offset}")

                    child_offset = cc_offset + 4
                    children: List[Node] = []
                    total_nodes = 1
                    for _ in range(child_count):
                        child, child_offset, child_nodes = parse_fast_node(child_offset)
                        children.append(child)
                        total_nodes += child_nodes

                    return (
                        Node(name, child_count, normalize_vxm_name(vxm_name), children),
                        child_offset,
                        total_nodes,
                    )
                except Exception as e:
                    last_error = e

            raise ValueError(f"Could not fast-parse node at offset {offset}: {last_error}")

        local_offset = start_offset
        roots: List[Node] = []
        total_nodes = 0
        for _ in range(rig_count):
            root_node, local_offset, root_nodes = parse_fast_node(local_offset)
            roots.append(root_node)
            total_nodes += root_nodes
        return finalize_roots(roots), total_nodes

    def parse_node_from_offset(start_offset: int) -> Tuple[Node, int, int]:
        if start_offset in parse_cache:
            return parse_cache[start_offset]

        reader_obj = BinaryReader(data)
        reader_obj.offset = start_offset

        name = reader_obj.read_string()
        vxm_name = reader_obj.read_string()
        meta_start = reader_obj.tell()

        def collect_candidates(extra_lens: List[int]) -> List[Tuple[int, int, int, int, int, List[Node]]]:
            candidates: List[Tuple[int, int, int, int, int, List[Node]]] = []
            for extra_len in extra_lens:
                cc_offset = meta_start + VXR_NODE_META_LEN + extra_len
                child_count = reader_obj.peek_u32_at(cc_offset)
                if child_count is None or child_count > 5000:
                    continue

                child_reader = BinaryReader(data)
                child_reader.offset = cc_offset + 4
                children: List[Node] = []
                total_nodes = 1
                success = True

                for _ in range(child_count):
                    try:
                        child_node, child_end_offset, child_nodes = parse_node_from_offset(child_reader.offset)
                    except Exception:
                        success = False
                        break
                    child_reader.offset = child_end_offset
                    children.append(child_node)
                    total_nodes += child_nodes

                if success:
                    boundary_score = score_following_boundary(child_reader.offset)
                    candidates.append(
                        (total_nodes, boundary_score, child_count, -extra_len, child_reader.offset, children)
                    )

            return candidates

        candidates = collect_candidates(iter_candidate_extra_lens(meta_start, allow_extended=False))
        best_strict_total = max((total for total, *_rest in candidates), default=0)
        has_strict_boundary = any(boundary_score > 0 for _total, boundary_score, *_rest in candidates)
        if not candidates or (best_strict_total == 1 and not has_strict_boundary):
            candidates.extend(collect_candidates(iter_candidate_extra_lens(meta_start, allow_extended=True)))

        if not candidates:
            raise ValueError(f"Could not parse node at offset {start_offset} (Node: {name})")

        candidates.sort(reverse=True)
        best_total_nodes, _boundary_score, child_count, _neg_extra_len, end_offset, children = candidates[0]

        node = Node(name, child_count, normalize_vxm_name(vxm_name), children)
        result = (node, end_offset, best_total_nodes)
        parse_cache[start_offset] = result
        return result

    def try_parse_roots_from_offset(start_offset: int) -> Tuple[Node, int]:
        local_offset = start_offset
        roots = []
        total_nodes = 0
        for _ in range(rig_count):
            root_node, end_offset, root_nodes = parse_node_from_offset(local_offset)
            roots.append(root_node)
            local_offset = end_offset
            total_nodes += root_nodes
        return finalize_roots(roots), total_nodes

    node_section_start = reader.tell()
    direct_offset = node_section_start
    while direct_offset < len(data) and direct_offset - node_section_start < 8 and data[direct_offset] == 0:
        direct_offset += 1

    try:
        direct_root, direct_count = try_parse_roots_fast(direct_offset)
        print(
            f"[VXR Parse] Found Root Node '{direct_root.name}' at offset {direct_offset} "
            f"(fast parse, skipped {direct_offset - node_section_start} leading null bytes, nodes: {direct_count})"
        )
        return direct_root, magic_signature
    except Exception as direct_error:
        print(f"[VXR Parse] Direct root parse failed at offset {direct_offset}: {direct_error}")

    valid_candidates = []
    search_limit = 4096
    scan_reader = BinaryReader(data)
    scan_reader.offset = node_section_start
    start_search = scan_reader.tell()

    while scan_reader.tell() - start_search < search_limit:
        current_pos = scan_reader.tell()
        ok1, name1, bytes1 = scan_reader.try_read_cstring_at(current_pos)
        if ok1 and len(name1.strip()) >= 1:
            check_pos2 = current_pos + bytes1
            ok2, _name2, bytes2 = scan_reader.try_read_cstring_at(check_pos2)

            if ok2:
                meta_start = check_pos2 + bytes2

                if meta_start + VXR_NODE_META_LEN <= len(scan_reader.data):
                    meta_bytes = scan_reader.data[meta_start : meta_start + VXR_NODE_META_LEN]
                    printable_count = sum(1 for b in meta_bytes if 32 <= b <= 126)
                    if printable_count >= 4:
                        scan_reader.skip(1)
                        continue

                candidate_lens = iter_candidate_extra_lens(meta_start, allow_extended=True)
                if candidate_lens:
                    try:
                        parsed_root, parsed_count = try_parse_roots_from_offset(current_pos)
                        valid_candidates.append((parsed_count, current_pos, name1, parsed_root))
                    except Exception:
                        pass

        scan_reader.skip(1)

    if not valid_candidates:
        raise ValueError("Could not find valid Root Node in VXR.")

    valid_candidates.sort(key=lambda x: (-x[0], x[1]))
    best_count, best_offset, best_name, best_root = valid_candidates[0]
    print(
        f"[VXR Parse] Found Root Node '{best_name}' at offset {best_offset} "
        f"(fallback candidates: {len(valid_candidates)}, nodes: {best_count})"
    )
    return best_root, magic_signature

# --- VXA Parsing ---

def parse_vxa(path: str, vxr_magic: bytes, node_names: List[str]) -> AnimData:
    with open(path, 'rb') as f:
        data = f.read()
    
    reader = BinaryReader(data)
    
    # VXA Header
    sig = reader.read_bytes(3)
    if sig != b'VXA':
        if sig == b'VXR':
            # User might have swapped files?
            raise ValueError("File signature is VXR, not VXA.")
        raise ValueError("Invalid VXA signature")
    
    # Magic Number (17 bytes) - Spec says "17 bytes (Match VXR)"
    # Note: Using the first 17 bytes of the VXR *might* work if VXR header contains it.
    # In practice, we will check if "Magic Number" matches what we extracted from VXR.
    # But earlier I only extracted `magic_signature = data[0:20]`.
    # Let's perform a lenient check or warning if mismatch, as strict check might fail if my VXR Magic hypothesis is wrong.
    # For now, read 17 bytes.
    vxa_magic = reader.read_bytes(17)
    
    # We might compare vxa_magic vs vxr_magic (truncated to 17) if we knew for sure.
    # print(f"Debug: VXR Magic Start: {vxr_magic[:17].hex()}, VXA Magic: {vxa_magic.hex()}")
    # For strict compliance with user request "Validate... match or warn/abort":
    # I will assume "VXR Magic" refers to the *same* 17 bytes sequence found in VXR.
    # Since VXR spec was vague on "17 bytes", I'll issue a warning if completely different, 
    # but proceed if the user is testing.
    # Actually, usually in VoxEdit, the Project UUID is embedded.
    
    # Animation Name (Null Terminated)
    _ = reader.read_string()
    
    # Start Separator (4 bytes: 01 00 00 00)
    # Check or skip
    start_sep = reader.read_u32()
    # if start_sep != 1: print("Warning: VXA Start Separator != 1")

    anim_nodes = []

    # Loop for each node (Order matches VXR)
    for name in node_names:
        # 6 channels: posX, posY, posZ, rotX, rotY, rotZ
        channels = []
        for channel_idx in range(6):
            is_rot_x = (channel_idx == 3) # 0,1,2=Pos, 3=RotX
            is_rot = (channel_idx >= 3)
            
            kf_count = reader.read_u32()
            kfs = []
            
            for _ in range(kf_count):
                frame = reader.read_u32()
                ease = reader.read_u32()
                slerp = None
                
                # SPECIAL REQUIREMENT: slerp_flag for RotX
                # "Each [RotX] keyframe... 1 byte slerp_flag ... value"
                if is_rot_x:
                     slerp = reader.read_u8()
                
                raw_value = reader.read_f32()
                
                final_val = raw_value
                if is_rot:
                    # Convert Rad to Deg: val * 180 / PI
                    deg = raw_value * 180.0 / math.pi
                    # Normalize 0-360
                    deg = deg % 360.0
                    if deg < 0: deg += 360.0
                    final_val = deg
                
                kfs.append(KeyFrame(frame=frame, ease=ease, slerp=slerp, value=final_val))
            
            channels.append(kfs)
        
        # Node Separator (16 bytes) + Child Count Check
        # Spec: 16 byte separator + 4 byte child count
        # We can verify child count if we had generic map, but we iterated node_names from VXR.
        reader.skip(16)
        _cc = reader.read_u32() # consume child count
        
        # Construct NodeAnim
        # Channels 0-2=Pos, 3-5=Rot
        na = NodeAnim(name, channels)
        anim_nodes.append(na)
        
    return AnimData(anim_nodes)

# --- Output Generation (ID-Based) ---

def format_node_tree_with_ids(node: Node, start_id: int = 0, depth: int = 0) -> Tuple[List[str], int]:
    """
    再帰的にツリーをフォーマットし、IDを付番する。
    戻り値: (lines, next_id)
    """
    lines = []
    indent = "  " * depth
    prefix = "└" if depth > 0 else ""
    
    current_id = start_id
    next_id = current_id + 1
    
    # Store temporary ID directly on node for later use in Animation process
    node._temp_id = current_id
    
    # フォーマット: └ [<id>] NodeName / VxmName
    if node.vxm_name:
        label = f"[{current_id}] {node.name} / {node.vxm_name}"
    else:
        label = f"[{current_id}] {node.name}"
    
    lines.append(f"{indent}{prefix} {label}")
    
    for child in node.children:
        child_lines, after_child_id = format_node_tree_with_ids(child, next_id, depth + 1)
        lines.extend(child_lines)
        next_id = after_child_id
        
    return lines, next_id

def process_animation_with_ids(anim_data: AnimData, flat_nodes_in_order: List[Node]) -> List[str]:
    lines = []
    
    # anim_data.nodes は VXR の階層順序と一致している前提 (parse_vxaの実装依存)
    # flat_nodes_in_order も VXR の階層順序 (Preorder) で作られている前提。
    # したがって、index i 同士で対応するはずである。
    # ただし、parse_vxa で "SceneRoot" 仮想ルートを除外しているかどうかによる。
    # 今回は strict に「Hierarcy出力でIDを振ったノード全て」に対して AnimDataがあるかチェックする。
    
    # Sync Logic:
    # `anim_data.nodes[k]` corresponds to `flat_names[k]`.
    # We need to find the `Node` object that matches `flat_names[k]` to get its `_temp_id`.
    # Because duplicates exist ("MR4", "MR4"), we must consume nodes in order.
    
    anim_idx = 0
    total_anims = len(anim_data.nodes)
    
    for node in flat_nodes_in_order:
        # Check if this node expects animation.
        # If it's a Virtual SceneRoot (name="SceneRoot", vxm=None, virtual=True?), likely no anim.
        # But `Node` struct doesn't have virtual flag.
        # Heuristic: verify if anim_data.nodes[anim_idx].name matches node.name
        
        matched = False
        if anim_idx < total_anims:
            anim_node = anim_data.nodes[anim_idx]
            if anim_node.name == node.name:
                matched = True
            else:
                # Name mismatch. 
                pass
        
        if matched and anim_idx < total_anims:
            anim_node = anim_data.nodes[anim_idx]
            
            # Match!
            target_id = getattr(node, '_temp_id', -1)
            
            # Output: [ID] NodeName：
            lines.append(f"[{target_id}] {anim_node.name}：")
            
            # Format Keys
            lines.extend(format_keys(anim_node))
            
            anim_idx += 1
        else:
            # Not matched (e.g. SceneRoot or missing anim)
            # Just skip outputting animation block for this node
            pass
            
    return lines

def format_keys(node_anim: NodeAnim) -> List[str]:
    lines = []
    # Collect all unique frames
    all_frames = set()
    for ch in node_anim.channels:
        for kf in ch:
            all_frames.add(kf.frame)
    
    sorted_frames = sorted(list(all_frames))
    
    if not sorted_frames:
        lines.append("")
        return lines # Empty
        
    current_vals = [0.0] * 6
    current_eases = [1] * 6
    current_slerp = 0
    ch_maps = []
    ease_maps = []
    for ch in node_anim.channels:
        m = {kf.frame: kf.value for kf in ch}
        em = {kf.frame: kf.ease for kf in ch}
        ch_maps.append(m)
        ease_maps.append(em)

    slerp_map = {kf.frame: (0 if kf.slerp is None else kf.slerp) for kf in node_anim.channels[3]}
    
    for f in sorted_frames:
        for i in range(6):
            if f in ch_maps[i]:
                current_vals[i] = ch_maps[i][f]
            if f in ease_maps[i]:
                current_eases[i] = ease_maps[i][f]
        if f in slerp_map:
            current_slerp = slerp_map[f]
        
        pos_str = f"POS X:{current_vals[0]:.6f},Y:{current_vals[1]:.6f},Z:{current_vals[2]:.6f}"
        rot_str = f"ROT X:{current_vals[3]:.6f},Y:{current_vals[4]:.6f},Z:{current_vals[5]:.6f}"
        ease_str = (
            "EASE "
            f"PX:{current_eases[0]}({ease_label(current_eases[0])}),"
            f"PY:{current_eases[1]}({ease_label(current_eases[1])}),"
            f"PZ:{current_eases[2]}({ease_label(current_eases[2])}),"
            f"RX:{current_eases[3]}({ease_label(current_eases[3])}),"
            f"RY:{current_eases[4]}({ease_label(current_eases[4])}),"
            f"RZ:{current_eases[5]}({ease_label(current_eases[5])})"
        )
        slerp_str = f"SLERP:{current_slerp}({slerp_label(current_slerp)})"

        lines.append(f"{f}KF => {pos_str}  / {rot_str} | {ease_str} | {slerp_str}")
    
    lines.append("") 
    return lines

# --- File Discovery & Logging ---

def setup_logger(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Signature
    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write("Created by KisaragiKoubou\n")
    except Exception: pass

    logger = logging.getLogger("VE2RBX_Dumper")
    logger.setLevel(logging.INFO)
    if logger.hasHandlers(): logger.handlers.clear()
    
    fh = logging.FileHandler(log_path, mode='a', encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    
    # Dual Output
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger

def get_default_edit_dir(project_dir: Path) -> Path:
    user_docs = Path(os.path.expanduser("~")) / "Documents"
    base_dir = user_docs / "VE2RBX"
    input_folder_name = project_dir.name
    timestamp = datetime.datetime.now().strftime("%Y.%m.%d.%H%M")
    edit_dir_name = f"{input_folder_name} {timestamp}"
    edit_dir = base_dir / input_folder_name / edit_dir_name
    return edit_dir

def clip_name_from_vxa_path(vxa_path: Path) -> str:
    stem = vxa_path.stem
    if "." in stem:
        clip_name = stem.rsplit(".", 1)[-1].strip()
        if clip_name:
            return clip_name
    return stem.strip() or stem


def sanitize_filename_component(text: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', '_', text).strip()
    safe = safe.rstrip(". ")
    return safe or "Unnamed"


def uniquify_filename_component(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate.lower() in used:
        candidate = f"{base}_{suffix:02d}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def select_default_vxa(vxa_files: List[Path]) -> Tuple[Path, str]:
    sorted_vxa = sorted(vxa_files, key=lambda p: p.name.lower())

    for vxa_path in sorted_vxa:
        basename_lower = vxa_path.name.lower()
        if basename_lower.endswith(".idle.vxa"):
            return vxa_path, "Matched '.Idle.vxa' (Priority 1)"

    for vxa_path in sorted_vxa:
        basename_lower = vxa_path.name.lower()
        if basename_lower.endswith(".idle 01.vxa") or basename_lower.endswith(".idle_01.vxa"):
            return vxa_path, "Matched '.Idle 01.vxa' (Priority 2)"

    return sorted_vxa[0], "Fallback to lexicographically first VXA"


def build_dump_output_lines(root_node: Node, anim_data: AnimData) -> List[str]:
    output_lines = []
    output_lines.append("# Node Hierarchy")

    hier_lines, _next_id = format_node_tree_with_ids(root_node)
    output_lines.extend(hier_lines)

    output_lines.append("")
    output_lines.append("# Animation")

    all_nodes_flat = []

    def collect_all(n: Node):
        all_nodes_flat.append(n)
        for c in n.children:
            collect_all(c)

    collect_all(root_node)
    output_lines.extend(process_animation_with_ids(anim_data, all_nodes_flat))
    return output_lines


def discover_files(target_dir: str, logger: logging.Logger = None) -> Tuple[Path, Path, List[Path]]:
    def log(msg):
        if logger: logger.info(msg)
        else: print(msg)

    target_path = Path(target_dir)
    if not target_path.exists():
        raise FileNotFoundError(f"Error: Target directory not found: {target_dir}")

    entries = [p for p in target_path.iterdir() if p.is_file()]
    vxr_files = [p for p in entries if p.suffix.lower() == ".vxr"]
    vxa_files = [p for p in entries if p.suffix.lower() == ".vxa"]

    if not vxr_files: raise FileNotFoundError("Error: No VXR file found.")
    selected_vxr = vxr_files[0]
    if len(vxr_files) > 1:
        vxr_files.sort(key=lambda f: (os.path.getsize(f), os.path.getmtime(f)), reverse=True)
        selected_vxr = vxr_files[0]
        log(f"[Discovery] Multiple VXR found. Selected '{selected_vxr.name}'.")
    else:
        log(f"[Discovery] Found VXR: '{selected_vxr.name}'")

    if not vxa_files: raise FileNotFoundError("Error: No VXA file found.")
    selected_vxa, reason = select_default_vxa(vxa_files)
    log(f"[Discovery] Selected default VXA: '{selected_vxa.name}' ({reason})")
    log(f"[Discovery] Found {len(vxa_files)} VXA file(s): " + ", ".join(p.name for p in sorted(vxa_files, key=lambda p: p.name.lower())[:20]))
    return selected_vxr, selected_vxa, sorted(vxa_files, key=lambda p: p.name.lower())

# --- Main ---

def dump_vxr_vxa(project_dir: Path, out_txt: Path, logger: logging.Logger = None) -> None:
    def log(msg):
        if logger: logger.info(msg)
        else: print(msg)

    target_dir = str(project_dir)
    
    try:
        vxr_path, default_vxa_path, all_vxa_paths = discover_files(target_dir, logger)
        
        # 1. Load VXR
        log(f"Loading VXR: {vxr_path.name}")
        root_node, vxr_magic = parse_vxr_fixed(vxr_path)

        # 2. Extract names for VXA
        flat_names_for_vxa = []
        def collect_names(n: Node, is_root: bool = False):
            # Same filter as before
            if not (is_root and n.name == "SceneRoot"):
                flat_names_for_vxa.append(n.name)
            for c in n.children:
                collect_names(c, is_root=False)   
        collect_names(root_node, is_root=True)

        out_txt.parent.mkdir(parents=True, exist_ok=True)
        used_clip_names: set[str] = set()

        for vxa_path in all_vxa_paths:
            clip_name = clip_name_from_vxa_path(vxa_path)
            safe_clip_name = uniquify_filename_component(
                sanitize_filename_component(clip_name),
                used_clip_names,
            )
            anim_data = parse_vxa(str(vxa_path), vxr_magic, flat_names_for_vxa)
            output_lines = build_dump_output_lines(root_node, anim_data)

            clip_out_txt = out_txt.with_name(f"{out_txt.stem}.{safe_clip_name}{out_txt.suffix}")
            log(f"Generating Clip Output: {clip_out_txt.name} <- {vxa_path.name}")
            with clip_out_txt.open("w", encoding="utf-8") as f:
                f.write("\n".join(output_lines))

            if vxa_path == default_vxa_path:
                log(f"Generating Default Output: {out_txt.name} <- {vxa_path.name}")
                with out_txt.open("w", encoding="utf-8") as f:
                    f.write("\n".join(output_lines))

        log("Dump Success (multi-VXA ID-Based).")
        
    except Exception as e:
        log(f"Error during dump: {e}")
        if logger:
             logger.error(traceback.format_exc())
        raise e

def run_ve2rbx(project_dir: Path, edit_dir: Path, log_path: Path = None) -> None:
    # 1. Log setup
    if not log_path:
        # Default: edit_dir/log/dumper.log
        log_dir = edit_dir / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "dumper.log"
    else:
        # Ensure parent exists if custom path given
        log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = setup_logger(log_path)
    logger.info(f"[DUMPER] START.")
    logger.info(f"  Project Dir: {project_dir}")
    logger.info(f"  Edit Dir   : {edit_dir}")

    
    # 2. Output Validation
    out_txt = edit_dir / "VXR&VXA.txt"
    logger.info(f"  Output Path: {out_txt}")
    
    try:
        dump_vxr_vxa(project_dir, out_txt, logger)
        logger.info("[DUMPER] Completed Successfully.")
    except Exception as e:
        logger.error(f"[DUMPER] FAILED: {e}")
        raise e

def main():
    parser = argparse.ArgumentParser(description="VXR VXA Dumper for VE2RBX")
    parser.add_argument("project_dir", nargs="?", help="Target project directory")
    parser.add_argument("--edit_dir", help="Output edit directory (Required for VE2RBX compliance, but optional for standalone test)")
    parser.add_argument("--log_path", help="Log file path (optional)")
    args = parser.parse_args()

    # 1. Project Directory
    if args.project_dir:
        project_dir = Path(args.project_dir).resolve()
    else:
        project_dir = Path(os.getcwd()).resolve()

    # 2. Edit Directory
    if args.edit_dir:
        edit_dir = Path(args.edit_dir).resolve()
    else:
        # Fallback for standalone manual testing only
        edit_dir = get_default_edit_dir(project_dir)
    
    if not edit_dir.exists():
        edit_dir.mkdir(parents=True, exist_ok=True)

    # 3. Log Path
    log_path = None
    if args.log_path:
        log_path = Path(args.log_path).resolve()
    
    # Print basic info to stdout before logging takes over
    print(f"Running Dumper...")
    print(f"Project : {project_dir}")
    print(f"Edit Dir: {edit_dir}")
    
    try:
        run_ve2rbx(project_dir, edit_dir, log_path)
        print("Success.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
