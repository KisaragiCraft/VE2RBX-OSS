import struct
import os
import sys
import re
import math
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict

# Check for Pillow
HAS_PIL = False
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    Image = None

# Constants
SCALE = 0.1
PALETTE_WIDTH = 256
PALETTE_BLOCK_SIZE = 4
PALETTE_TEXTURE_WIDTH = PALETTE_WIDTH * PALETTE_BLOCK_SIZE
PALETTE_TEXTURE_HEIGHT = PALETTE_TEXTURE_WIDTH
CENTER_GEOMETRY = False  # Default: Keep VoxEdit coordinates (0,0,0 based)
RECURSION_LIMIT = 5000
sys.setrecursionlimit(RECURSION_LIMIT * 2)

class VoxChunk:
    def __init__(self, char_id: str, content: bytes, children: List['VoxChunk']):
        self.id = char_id
        self.content = content
        self.children = children

class VoxModel:
    def __init__(self):
        self.size: Tuple[int, int, int] = (0, 0, 0)
        self.voxels: Dict[Tuple[int, int, int], int] = {}
        self.palette: List[Tuple[int, int, int, int]] = []
        self.pivot: Optional[Tuple[float, float, float]] = None
        self.emissive_indices: Set[int] = set()

def read_chunk(f) -> VoxChunk:
    chunk_id = f.read(4).decode('ascii')
    content_size = struct.unpack('<I', f.read(4))[0]
    children_size = struct.unpack('<I', f.read(4))[0]
    content = f.read(content_size)
    children = []
    read_bytes = 0
    while read_bytes < children_size:
        child_start = f.tell()
        child = read_chunk(f)
        children.append(child)
        read_bytes += (f.tell() - child_start)
    return VoxChunk(chunk_id, content, children)

def parse_dict(data: bytes) -> Tuple[Dict[str, str], int]:
    offset = 0
    num_pairs = struct.unpack_from('<I', data, offset)[0]
    offset += 4
    attributes = {}
    for _ in range(num_pairs):
        key_len = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        key = data[offset:offset+key_len].decode('ascii')
        offset += key_len
        val_len = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        val = data[offset:offset+val_len].decode('ascii')
        offset += val_len
        attributes[key] = val
    return attributes, offset

def parse_vox(file_path: str) -> Optional[VoxModel]:
    with open(file_path, 'rb') as f:
        header = f.read(4)
        if header != b'VOX ':
            return None
        version = struct.unpack('<I', f.read(4))[0]
        main_chunk = read_chunk(f)

    model = VoxModel()
    model.palette = [(255, 255, 255, 255)] * 256
    chunks = main_chunk.children

    for c in chunks:
        if c.id == 'RGBA':
            palette = []
            for i in range(256):
                pixel = struct.unpack_from('BBBB', c.content, i * 4)
                palette.append(pixel)
            model.palette = palette

    found_model = False
    for c in chunks:
        if c.id == 'SIZE':
            model.size = struct.unpack('<III', c.content)
            found_model = True
        elif c.id == 'XYZI' and found_model:
            num_voxels = struct.unpack('<I', c.content[:4])[0]
            for i in range(num_voxels):
                x, y, z, c_idx = struct.unpack_from('BBBB', c.content, 4 + i * 4)
                model.voxels[(x, y, z)] = c_idx

    for c in chunks:
        if c.id == 'MATL':
            offset = 0
            if len(c.content) < 4: continue
            mat_id = struct.unpack_from('<I', c.content, offset)[0]
            offset += 4
            mat_attribs, _ = parse_dict(c.content[offset:])
            if mat_attribs.get('_type') == '_emit':
                try:
                    if float(mat_attribs.get('_emit', '0')) > 0.0:
                        model.emissive_indices.add(mat_id)
                except ValueError: pass

    nx, ny, nz = model.size
    # Force pivot to 0 (No translation)
    model.pivot = (0.0, 0.0, 0.0)
    
    if len(model.voxels) == 0 and not found_model:
        return None
    return model

# --------------------------------------------------------
# Helper Functions
# --------------------------------------------------------

def get_uv_coords_for_color(color_idx: int):
    # Map MagicaVoxel color index to texture index:
    # 0 -> 255 (Effective Null/Transparent)
    # 1..255 -> 0..254
    if color_idx <= 0:
        tex_idx = 255
    else:
        tex_idx = color_idx - 1
        
    u = (tex_idx * PALETTE_BLOCK_SIZE + (PALETTE_BLOCK_SIZE * 0.5)) / float(PALETTE_TEXTURE_WIDTH)
    v = 0.5
    return u, v

def is_emissive(color_idx: int, model: VoxModel = None):
    # Check if color is emissive
    if model and color_idx in model.emissive_indices: return True
    return False

# --------------------------------------------------------
# Geometry Helpers
# --------------------------------------------------------

def cross_product_z(a, b, c):
    """ (b-a) x (c-a) """
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

def intersect_segments(p1, p2, p3, p4):
    """ Segment-Segment intersection checking. Returns True for strictly crossing OR overlapping interior. """
    if p1 == p3 or p1 == p4 or p2 == p3 or p2 == p4:
        return False

    if max(p1[0], p2[0]) < min(p3[0], p4[0]) or \
       max(p3[0], p4[0]) < min(p1[0], p2[0]) or \
       max(p1[1], p2[1]) < min(p3[1], p4[1]) or \
       max(p3[1], p4[1]) < min(p1[1], p2[1]):
        return False

    def ccw(a, b, c):
        val = (c[1]-a[1]) * (b[0]-a[0]) - (b[1]-a[1]) * (c[0]-a[0])
        if abs(val) < 1e-7: return 0
        return 1 if val > 0 else -1

    d1 = ccw(p3, p4, p1)
    d2 = ccw(p3, p4, p2)
    d3 = ccw(p1, p2, p3)
    d4 = ccw(p1, p2, p4)

    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True

    def on_segment(p, a, b):
        return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and \
               min(a[1], b[1]) <= p[1] <= max(a[1], b[1])

    if d1 == 0 and on_segment(p1, p3, p4): return True
    if d2 == 0 and on_segment(p2, p3, p4): return True
    if d3 == 0 and on_segment(p3, p1, p2): return True
    if d4 == 0 and on_segment(p4, p1, p2): return True

    return False



def triangle_respects_color(p1, p2, p3, grid, target_color):
    min_x = int(math.floor(min(p1[0], p2[0], p3[0]))); max_x = int(math.ceil(max(p1[0], p2[0], p3[0])))
    min_y = int(math.floor(min(p1[1], p2[1], p3[1]))); max_y = int(math.ceil(max(p1[1], p2[1], p3[1])))
    
    u_len = len(grid); v_len = len(grid[0])
    min_x = max(0, min_x); max_x = min(u_len, max_x)
    min_y = max(0, min_y); max_y = min(v_len, max_y)
    
    for u in range(min_x, max_x):
        for v in range(min_y, max_y):
            if grid[u][v] == target_color: continue
            cx = u + 0.5; cy = v + 0.5
            if point_in_triangle((cx, cy), p1, p2, p3): return False
    return True

def segment_respects_color(p1, p2, grid, target_color):
    min_x = int(math.floor(min(p1[0], p2[0]))); max_x = int(math.ceil(max(p1[0], p2[0])))
    min_y = int(math.floor(min(p1[1], p2[1]))); max_y = int(math.ceil(max(p1[1], p2[1])))
    
    u_len = len(grid); v_len = len(grid[0])
    min_x = max(0, min_x); max_x = min(u_len, max_x)
    min_y = max(0, min_y); max_y = min(v_len, max_y)
    
    epsilon = 1e-5
    for u in range(min_x, max_x):
        for v in range(min_y, max_y):
            if grid[u][v] == target_color: continue
            if clip_liang_barsky(p1, p2, (u + epsilon, v + epsilon), (u + 1 - epsilon, v + 1 - epsilon)): return False
    return True

def triangle_area(p1, p2, p3):
    return abs((p2[0]-p1[0])*(p3[1]-p1[1]) - (p2[1]-p1[1])*(p3[0]-p1[0])) * 0.5

def triangulate_magica_recursive(poly, grid, color, edge_blockers, global_edges):
    triangles = []
    
    if not poly: return []
    
    # Ensure positive winding (area > 0)
    cur_coords = [(p[1], p[2]) for p in poly]
    if polygon_signed_area(cur_coords) < 0:
        poly.reverse()
        
    # Pre-calculate bbox for scoring
    min_x = min(p[1] for p in poly); max_x = max(p[1] for p in poly)
    min_y = min(p[2] for p in poly); max_y = max(p[2] for p in poly)
    bbox_cx = (min_x + max_x) * 0.5; bbox_cy = (min_y + max_y) * 0.5
    
    while len(poly) > 3:
        best_k = -1
        # Priority: 1. Min Area, 2. Max Dist from Center (Outer), 3. Min Diag Length
        # Score tuple: (area, -dist_to_center_sq, diag_len_sq) -> Minimize this tuple
        best_score = (float('inf'), float('inf'), float('inf'))
        
        num = len(poly)
        duplicate_or_spike_handled = False
        
        # Candidate scan
        for k in range(num):
            prev = poly[(k-1)%num]; curr = poly[k]; next_p = poly[(k+1)%num]
            P = (prev[1], prev[2]); C = (curr[1], curr[2]); N = (next_p[1], next_p[2])
            
            # 1. Clean Duplicates
            if P == C or C == N:
                poly.pop(k)
                duplicate_or_spike_handled = True
                break
                
            # 2. Spike Optimization
            if P == N:
                 triangles.append((prev[0], curr[0], next_p[0]))
                 poly.pop(k)
                 duplicate_or_spike_handled = True
                 break

            # 3. Geometric Validations
            if cross_product_z(P, C, N) <= 1e-7: continue # Reflex or collinear
            
            area = triangle_area(P, C, N)
            if area <= 1e-9: continue # Ignore non-area triangles
            
            # Empty Ear Check
            has_point_inside = False
            for node in poly:
                if node[0] in (prev[0], curr[0], next_p[0]): continue
                if point_in_triangle((node[1], node[2]), P, C, N):
                    has_point_inside = True; break
            if has_point_inside: continue
            
            # Edge Safety Check (Strict + Global)
            blocked = False
            # Check strictly against edge_blockers (local)
            for (e1, e2) in edge_blockers:
                if (e1 == P or e1 == N or e2 == P or e2 == N): continue
                if intersect_segments(P, N, e1, e2): blocked = True; break
            if blocked: continue
            
            # Check against GLOBAL edges
            if intersects_global(P, N, global_edges): continue

            # Color Check
            if not triangle_respects_color(P, C, N, grid, color): continue

            # Scoring
            diag_len_sq = (P[0] - N[0])**2 + (P[1] - N[1])**2
            mid_x = (P[0] + N[0]) * 0.5; mid_y = (P[1] + N[1]) * 0.5
            dist_to_center_sq = (mid_x - bbox_cx)**2 + (mid_y - bbox_cy)**2
            
            # Tuple comparison: (Area, Outer-ness, Shortness)
            # We want Small Area (Minimize area)
            # We want Outer (Maximize dist -> Minimize -dist)
            # We want Short (Minimize diag)
            score = (area, -dist_to_center_sq, diag_len_sq)
            
            if score < best_score:
                best_score = score
                best_k = k
        
        if duplicate_or_spike_handled:
            continue
            
        if best_k != -1:
            k = best_k
            prev = poly[(k-1)%num]; curr = poly[k]; next_p = poly[(k+1)%num]
            P = (prev[1], prev[2]); N = (next_p[1], next_p[2])
            
            triangles.append((prev[0], curr[0], next_p[0]))
            edge_blockers.append((P, N))
            global_edges.append((P, N)) # Register to global
            
            poly.pop(k)
            continue
            
        # No ear found
        split_tris = split_polygon_fallback(poly, grid, color, edge_blockers, global_edges)
        if split_tris is None:
            # Dropping leftover polygon
            print(f"[WARN] triangulate_magica: leftover polygon with {len(poly)} vertices; dropping.")
            return triangles # Return whatever we found so far
        return triangles + split_tris

    if len(poly) == 3:
        # Final triangle check
        p0=poly[0]; p1=poly[1]; p2=poly[2]
        P=(p0[1], p0[2]); C=(p1[1], p1[2]); N=(p2[1], p2[2])
        
        if triangle_respects_color(P, C, N, grid, color):
            triangles.append((p0[0], p1[0], p2[0]))
        else:
            print(f"[WARN] Final triangle failed color check. Dropping.")
        
    return triangles

def split_polygon_fallback(poly, grid, color, edge_blockers, global_edges):
    best_diag = find_best_diagonal_geometric(poly, grid, color, edge_blockers, global_edges)
        
    if best_diag:
        i, j, A, B = best_diag
        edge_blockers.append((A, B))
        global_edges.append((A, B)) # Register to global
        
        if i > j: i, j = j, i
        polyA = poly[i:j+1]
        polyB = poly[j:] + poly[:i+1]
        
        tA = triangulate_magica_recursive(polyA, grid, color, edge_blockers, global_edges)
        tB = triangulate_magica_recursive(polyB, grid, color, edge_blockers, global_edges)
        
        if tA is not None and tB is not None:
            return tA + tB
        return None
    
    return None

def find_best_diagonal_geometric(poly, grid, color, edge_blockers, global_edges):
    best_diag = None
    min_dist_sq = float('inf')
    n = len(poly)
    poly_coords = [(p[1], p[2]) for p in poly]
    
    for i in range(n):
        for j in range(i + 2, n):
            if (i == 0 and j == n - 1): continue
            
            p1 = poly[i]; p2 = poly[j]
            A = (p1[1], p1[2]); B = (p2[1], p2[2])
            
            # blocked check (Strict Local)
            blocked = False
            for (e1, e2) in edge_blockers:
                if (e1 == A or e1 == B or e2 == A or e2 == B): continue
                if intersect_segments(A, B, e1, e2): blocked = True; break
            if blocked: continue
            
            # blocked check (Global)
            if intersects_global(A, B, global_edges): continue
            
            # Color check
            if not segment_respects_color(A, B, grid, color): continue
            
            # Midpoint in poly check
            mid = ((A[0]+B[0])*0.5, (A[1]+B[1])*0.5)
            if not is_point_in_poly(mid, poly_coords): continue

            dist_sq = (A[0]-B[0])**2 + (A[1]-B[1])**2
            if dist_sq < min_dist_sq:
                min_dist_sq = dist_sq
                best_diag = (i, j, A, B)
                
    return best_diag

# --------------------------------------------------------
# Main
# --------------------------------------------------------

def generate_mesh_data(model: VoxModel):
    def get_color(x, y, z): return model.voxels.get((x, y, z))
    def is_solid(c): return c is not None and c != 0
    px, py, pz = model.pivot
    vertices = []; uvs = []; uv_is_emissive = []; normals = []; faces = []
    vertex_cache = {}; uv_cache = {}
    
    def get_uv_index(u_val, v_val, is_emit):
        key = (round(u_val, 6), round(v_val, 6), is_emit)
        idx = uv_cache.get(key)
        if idx is not None: return idx
        new_idx = len(uvs)
        uvs.append((u_val, v_val))
        uv_is_emissive.append(is_emit)
        uv_cache[key] = new_idx
        return new_idx

    def get_vertex_index(gx, gy, gz):
        key = (int(gx*100), int(gy*100), int(gz*100))
        idx = vertex_cache.get(key)
        if idx is not None: return idx
        
        # Pure Coordinate Scaling (No Pivot Subtraction)
        rx = gx * SCALE; ry = gy * SCALE; rz = gz * SCALE
        
        # Rotation: (x, y, z) -> (x, z, -y) preserved
        # But NO Translation

        
        # X-axis -90 degree rotation for Blender alignment
        # (x, y, z) -> (x, z, -y)
        vertices.append((rx, rz, -ry))
        
        vertex_cache[key] = len(vertices)-1
        return len(vertices)-1

    # Direction Configs for Greedy Meshing
    # Order: +X, -X, +Y, -Y, +Z, -Z
    # Axis Mapping: (u_axis, v_axis, depth_axis)
    # We iterate depth, then u, v.
    # Quad corners relative to (u, v, depth) in that system.
    dirs = [
        # Name, Normal, Offset, u_axis, v_axis, d_axis, is_pos_d
        ('x+', (1, 0, 0), (1, 0, 0), 2, 1, 0, True),  # U=Z, V=Y, D=X
        ('x-', (-1, 0, 0),(-1, 0, 0), 2, 1, 0, False), # U=Z, V=Y, D=X
        ('y+', (0, 1, 0), (0, 1, 0), 0, 2, 1, True),  # U=X, V=Z, D=Y
        ('y-', (0, -1, 0), (0, -1, 0), 0, 2, 1, False), # U=X, V=Z, D=Y
        ('z+', (0, 0, 1), (0, 0, 1), 0, 1, 2, True),  # U=X, V=Y, D=Z
        ('z-', (0, 0, -1), (0, 0, -1), 0, 1, 2, False)  # U=X, V=Y, D=Z
    ]
    
    # Pre-register normals
    for d in dirs: normals.append(d[1])
    
    # Determine bounds
    if not model.voxels: return {'vertices':[], 'uvs':[], 'normals':[], 'faces':[], 'uv_is_emissive':[]}
    
    keys = list(model.voxels.keys())
    # Note: model.size might be larger than bounding box of actua voxels.
    # We should iterate model.size range to be safe or bbox? 
    # Use model.size as iteration space.
    size_x, size_y, size_z = model.size

    print(f"[DEBUG] Greedy Meshing Start: {len(model.voxels)} voxels. Size: {model.size}")

    for dir_idx, (name, n_vec, n_off, u_ax, v_ax, d_ax, is_pos) in enumerate(dirs):
        # Axis lengths
        dims = [size_x, size_y, size_z]
        len_u = dims[u_ax]
        len_v = dims[v_ax]
        len_d = dims[d_ax]
        
        # Iterate Depth Slices
        for d in range(len_d):
            # 1. Build Mask for this slice
            mask = [[None] * len_v for _ in range(len_u)]
            has_data = False
            
            for u in range(len_u):
                for v in range(len_v):
                    # Construct current voxel position
                    pos = [0, 0, 0]
                    pos[u_ax] = u
                    pos[v_ax] = v
                    pos[d_ax] = d
                    c = get_color(pos[0], pos[1], pos[2])
                    
                    if is_solid(c):
                        # Check neighbor
                        npos = [pos[0] + n_off[0], pos[1] + n_off[1], pos[2] + n_off[2]]
                        nc = get_color(npos[0], npos[1], npos[2])
                        if not is_solid(nc):
                             mask[u][v] = c
                             has_data = True
            
            if not has_data: continue

            # 2. Greedy Meshing on Mask
            visited = [[False] * len_v for _ in range(len_u)]
            
            for v in range(len_v):
                for u in range(len_u):
                    if visited[u][v]: continue
                    color = mask[u][v]
                    if color is None: continue
                    
                    # Compute Width
                    w = 1
                    while (u + w < len_u) and (not visited[u + w][v]) and (mask[u + w][v] == color):
                        w += 1
                        
                    # Compute Height
                    h = 1
                    row_ok = True
                    while (v + h < len_v) and row_ok:
                        for k in range(u, u + w):
                            if visited[k][v + h] or mask[k][v + h] != color:
                                row_ok = False
                                break
                        if row_ok: h += 1
                    
                    # Mark Visited
                    for jj in range(v, v + h):
                        for ii in range(u, u + w):
                            visited[ii][jj] = True
                            
                    # 3. Emit Quad
                    # Corners in (U, V) space: (u, v), (u+w, v), (u+w, v+h), (u, v+h)
                    # Convert to (X, Y, Z)
                    
                    # Mapping (u,v) -> 3D coords
                    # We need the 4 corners of the quad. 
                    # The quad lies on the face of the voxel.
                    # Base voxel is at 'd'. Face is at d+1 if is_pos else d.
                    
                    # Let's define the 4 corners in LOCAL (u,v) space relative to the slice origin
                    # (0,0), (w,0), (w,h), (0,h)
                    # But we need their 3D coords.
                    
                    # For a quad of size w*h at (u,v) on generic axes:
                    # c0: (u, v)
                    # c1: (u+w, v)
                    # c2: (u+w, v+h)
                    # c3: (u, v+h)
                    
                    # We need to map these to (X,Y,Z).
                    # Also, the "Depth" coordinate of the face geometry.
                    # If direction is positive (e.g. +X), face is at x + 1.
                    # If direction is negative (e.g. -X), face is at x.
                    
                    d_geom = d + 1 if is_pos else d
                    
                    face_verts = []
                    corner_uvs = [(u, v), (u+w, v), (u+w, v+h), (u, v+h)] # Logic Check later
                    
                    # Fix Winding Order based on Standard Face defs
                    # We entered the function with generic u,v loops.
                    # We need to ensure Counter-Clockwise winding for the normal.
                    
                    # Standard mapping:
                    # +X: U=Z, V=Y. Normal=(1,0,0). CCW on YZ plane looking from +X.
                    #     Right=Z? Up=Y? 
                    #     Wait, Magica/Previous code:
                    #     +X: Vertices [(1,0,0), (1,1,0), (1,1,1), (1,0,1)] (relative to origin)
                    #     This is (x+1, y, z), (x+1, y+1, z), (x+1, y+1, z+1), (x+1, y, z+1) ? 
                    #     Actually my previous definitions were:
                    #     +X: (1,0,0), (1,1,0), (1,1,1), (1,0,1).
                    #     Checking coordinates:
                    #     Let's stick to the axes defined in 'dirs':
                    #     x+: U=2(Z), V=1(Y). 
                    #     If we iterate u(Z) and v(Y), we are filling the ZY plane.
                    #     Quad from (z,y) to (z+w, y+h).
                    #     Corners: (z,y), (z+w, y), (z+w, y+h), (z, y+h).
                    #     Map back to (X, Y, Z):
                    #     P0: X=d+1, Y=y,   Z=z
                    #     P1: X=d+1, Y=y,   Z=z+w  (since u is Z)
                    #     P2: X=d+1, Y=y+h, Z=z+w
                    #     P3: X=d+1, Y=y+h, Z=z
                    #     Winding: P0->P1->P2->P3. 
                    #     Normal (1,0,0).
                    #     Vector P0->P1 is (0,0,w) i.e. +Z.
                    #     Vector P0->P3 is (0,h,0) i.e. +Y.
                    #     Cross(+Z, +Y) = (-1, 0, 0).
                    #     Wait, we want (+1, 0, 0).
                    #     So P0-P1 should be +Y or order should be reversed?
                    #     Cross(Y, Z) = (0,1,0)x(0,0,1) = (1,0,0). Correct.
                    #     So we need winding to go Y then Z?
                    #     Or just re-order the vertices: (z,y) -> (z,y+h) -> (z+w, y+h) -> (z+w, y).
                    #     Let's explicitly define winding per face direction to be safe/consistent.
                    
                    # Correct Winding Map per direction
                    # (u, v) usually implies (0,0) -> (1,0) -> (1,1) -> (0,1) in that 2D plane.
                    # We just need to map that to 3D and check normal.
                    
                    # Let's calculate the 3D points for the "Standard" 2D quad (0,0)-(W,H)
                    # c0=(u,v), c1=(u+w, v), c2=(u+w, v+h), c3=(u, v+h)
                    
                    raw_corners = [(u, v), (u+w, v), (u+w, v+h), (u, v+h)]
                    
                    # Convert to 3D
                    pts = []
                    for (cu, cv) in raw_corners:
                        p = [0,0,0]
                        p[u_ax] = cu
                        p[v_ax] = cv
                        p[d_ax] = d_geom
                        pts.append(tuple(p))
                        
                    # Check Normal
                    v1 = (pts[1][0]-pts[0][0], pts[1][1]-pts[0][1], pts[1][2]-pts[0][2])
                    v2 = (pts[3][0]-pts[0][0], pts[3][1]-pts[0][1], pts[3][2]-pts[0][2])
                    # Cross v1, v2
                    cx = v1[1]*v2[2] - v1[2]*v2[1]
                    cy = v1[2]*v2[0] - v1[0]*v2[2]
                    cz = v1[0]*v2[1] - v1[1]*v2[0]
                    
                    # Normalize logic check
                    # We want dot(cross, normal) > 0
                    dot = cx*n_vec[0] + cy*n_vec[1] + cz*n_vec[2]
                    
                    if dot < 0:
                        # Winding is reversed relative to normal. Flip it.
                        pts = [pts[0], pts[3], pts[2], pts[1]]
                        
                    # Get Indices
                    for p in pts:
                        face_verts.append(get_vertex_index(p[0], p[1], p[2]))
                        
                    # UVs
                    center_x, center_y = get_uv_coords_for_color(color)
                    is_emit = is_emissive(color, model)
                    uv_idx = get_uv_index(center_x, center_y, is_emit)
                    
                    # Add Triangles
                    faces.append(((face_verts[0], uv_idx, dir_idx), (face_verts[1], uv_idx, dir_idx), (face_verts[2], uv_idx, dir_idx)))
                    faces.append(((face_verts[0], uv_idx, dir_idx), (face_verts[2], uv_idx, dir_idx), (face_verts[3], uv_idx, dir_idx)))

    print(f"[DEBUG] Quad Meshing Done: {len(faces)//2} quads generated.")

    if vertices:
        xs = [v[0] for v in vertices]; ys = [v[1] for v in vertices]; zs = [v[2] for v in vertices]
        cx = 0.5 * (min(xs) + max(xs)); cz = 0.5 * (min(zs) + max(zs)); by = min(ys)
        
        if CENTER_GEOMETRY:
            # 1. Center and Ground (Y-up space)
            vertices = [(v[0]-cx, v[1]-by, v[2]-cz) for v in vertices]
        else:
             # Keep original coordinates (but still shift Y if ground logic was intended? No, keep pure.)
             pass
        
        # Rotation logic removed. Output is pure Y-up.


    return {
        'vertices': vertices,
        'uvs': uvs,
        'normals': normals,
        'faces': faces,
        'uv_is_emissive': uv_is_emissive
    }

def write_obj(filepath: str, mesh_data, obj_name: str, mtl_filename: str):
    vertices = mesh_data['vertices']
    uvs = mesh_data['uvs']
    uv_is_emissive = mesh_data.get('uv_is_emissive', [False]*len(uvs))
    normals = mesh_data['normals']
    faces = mesh_data['faces']
    
    with open(filepath, 'w') as f:
        f.write("# Generated by vox2obj\n"); f.write(f"mtllib {mtl_filename}\n"); f.write(f"o {obj_name}\n"); f.write("s off\n")
        for vx, vy, vz in vertices: f.write(f"v {vx:.6f} {vy:.6f} {vz:.6f}\n")
        for u, v in uvs: f.write(f"vt {u:.6f} {v:.6f}\n")
        for nx, ny, nz in normals: f.write(f"vn {nx:.4f} {ny:.4f} {nz:.4f}\n")
        current_mtl = None
        for tri in faces:
            is_emit = uv_is_emissive[tri[0][1]]; desired_mtl = "palette_emit" if is_emit else "palette"
            if desired_mtl != current_mtl: f.write(f"usemtl {desired_mtl}\n"); current_mtl = desired_mtl
            f.write(f"f {' '.join([f'{v+1}/{vt+1}/{vn+1}' for v, vt, vn in tri])}\n")

def _write_png_rgba(filepath: str, width: int, height: int, rgba_rows: List[bytes]):
    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    raw = b"".join(b"\x00" + row for row in rgba_rows)
    png = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            _chunk(b"IDAT", zlib.compress(raw)),
            _chunk(b"IEND", b""),
        ]
    )
    with open(filepath, "wb") as f:
        f.write(png)


def write_palette(filepath: str, palette: List[Tuple[int, int, int, int]]):
    # Generate a square palette texture where each color is 4 px wide and tiled vertically.
    if HAS_PIL and Image is not None:
        img = Image.new('RGBA', (PALETTE_TEXTURE_WIDTH, PALETTE_TEXTURE_HEIGHT), (0, 0, 0, 0))
        pixels = img.load()
        max_entries = min(PALETTE_WIDTH - 1, len(palette))
        for color_idx in range(max_entries):
            c = palette[color_idx]
            x0 = color_idx * PALETTE_BLOCK_SIZE
            for y0 in range(0, PALETTE_TEXTURE_HEIGHT, PALETTE_BLOCK_SIZE):
                for y in range(y0, y0 + PALETTE_BLOCK_SIZE):
                    for x in range(x0, x0 + PALETTE_BLOCK_SIZE):
                        pixels[x, y] = c
        img.save(filepath)
        return

    rgba_rows: List[bytes] = []
    max_entries = min(PALETTE_WIDTH - 1, len(palette))
    transparent = bytes((0, 0, 0, 0))
    for _ in range(PALETTE_TEXTURE_HEIGHT):
        row = bytearray()
        for color_idx in range(PALETTE_WIDTH):
            color = palette[color_idx] if color_idx < max_entries else None
            pixel = bytes(color) if color else transparent
            row.extend(pixel * PALETTE_BLOCK_SIZE)
        rgba_rows.append(bytes(row))
    _write_png_rgba(filepath, PALETTE_TEXTURE_WIDTH, PALETTE_TEXTURE_HEIGHT, rgba_rows)

def convert_vox_folder(in_vox_dir, out_obj_dir):
    if not os.path.exists(in_vox_dir): return
    os.makedirs(out_obj_dir, exist_ok=True)
    mtl_filename = f"{os.path.basename(out_obj_dir)}.mtl"
    for vf in os.listdir(in_vox_dir):
        if not vf.lower().endswith(".vox"): continue
        model = parse_vox(os.path.join(in_vox_dir, vf))
        if not model: continue
        write_palette(os.path.join(out_obj_dir, "palette.png"), model.palette)
        with open(os.path.join(out_obj_dir, mtl_filename), 'w') as m: m.write("newmtl palette\nmap_Kd palette.png\n\nnewmtl palette_emit\nmap_Kd palette.png\nKe 1 1 1\n")
        mesh = generate_mesh_data(model)
        write_obj(os.path.join(out_obj_dir, f"{os.path.splitext(vf)[0]}.obj"), mesh, os.path.splitext(vf)[0], mtl_filename)
        print(f"Exported {vf}")

def run_ve2rbx(edit_dir):
    """
    VE2RBX パイプライン用エントリポイント。

    入力:
        edit_dir/OutputVOX/*.vox

    出力:
        edit_dir/OutputOBJ/
            - <basename>.obj
            - <out_dir_name>.mtl
            - palette.png
    """
    # edit_dir を Path に正規化
    if not isinstance(edit_dir, Path):
        edit_dir = Path(edit_dir)

    in_vox_dir = edit_dir / "OutputVOX"
    out_obj_dir = edit_dir / "OutputOBJ"

    if not in_vox_dir.exists():
        print(f"[vox2obj/run_ve2rbx] Input VOX dir not found: {in_vox_dir}")
        return

    out_obj_dir.mkdir(parents=True, exist_ok=True)

    # 既存の変換ロジックを再利用
    convert_vox_folder(str(in_vox_dir), str(out_obj_dir))

    print(f"[vox2obj/run_ve2rbx] Converted VOX -> OBJ in: {out_obj_dir}")

def find_latest_input_dir(base_dir: str):
    max_n = -1; best = None; pat = re.compile(r'^OutputVox (\d+)$', re.IGNORECASE)
    for d in os.listdir(base_dir):
        if os.path.isdir(d):
            m = pat.match(d)
            if m and int(m.group(1))>max_n: max_n=int(m.group(1)); best=d
    return best


import argparse
import traceback

def setup_logger(log_path: str):
    if not log_path:
        return
    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write("Created by KisaragiKoubou\n")
        
        log_file = open(log_path, 'a', encoding='utf-8')
        
        class DualWriter:
            def __init__(self, file, original):
                self.file = file
                self.original = original

            def write(self, text):
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
    parser = argparse.ArgumentParser(description="VOX to OBJ Converter")
    parser.add_argument("--edit_dir", help="Edit directory for VE2RBX pipeline (contains OutputVOX)", default=None)
    parser.add_argument("--log_path", help="Path to log file", default=None)
    parser.add_argument("in_dir", nargs="?", help="Legacy Input Directory", default=None)

    args = parser.parse_args()
    
    setup_logger(args.log_path)
    
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
    
    try:
        if args.edit_dir:
            # VE2RBX Pipeline
            run_ve2rbx(args.edit_dir)
        else:
            # Standalone Centralized Mode
            print("Standalone Mode: Generating centralized edit_dir...")
            
            project_dir = Path(os.getcwd())
            if args.in_dir:
                # If explicit dir provided, might be project OR input vox folder
                potential_in = Path(args.in_dir).resolve()
                if potential_in.exists():
                     project_dir = potential_in
                     
            # Input Detection
            # Pipeline expects 'OutputVOX' in edit_dir.
            # Here we need to find existing VOX files.
            in_vox_dir = project_dir
            if not list(in_vox_dir.glob("*.vox")):
                 # Check subfolders
                 cands = list(in_vox_dir.glob("OutputVOX*")) # e.g. OutputVOX 1, OutputVOX (legacy)
                 if cands:
                     in_vox_dir = sorted(cands)[-1]
                     print(f"Auto-detected input subfolder: {in_vox_dir.name}")
            
            # Generate Output Location
            edit_dir = create_edit_dir_local(project_dir)
            print(f"Edit Dir Created: {edit_dir}")
            
            if not args.log_path:
                log_dir = edit_dir / "log"
                log_dir.mkdir(parents=True, exist_ok=True)
                local_log = log_dir / "vox2obj.log"
                setup_logger(str(local_log))
                print(f"Log started at: {local_log}")

            out_obj_dir = edit_dir / "OutputOBJ"
            out_obj_dir.mkdir(parents=True, exist_ok=True)
            
            print(f"Input VOX Dir: {in_vox_dir}")
            print(f"Output OBJ Dir: {out_obj_dir}")
            
            convert_vox_folder(str(in_vox_dir), str(out_obj_dir))
            print(f"Converted VOX -> OBJ in: {out_obj_dir}")
            
        print("Done.")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
