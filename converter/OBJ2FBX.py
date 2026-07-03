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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ve2rbx_blender_common import (
    IN_BLENDER, bpy, Matrix, Vector, Euler, HAS_PIL, Image, VOXEL_SCALE,
    setup_logger, log, parse_ve2rbx_args, find_edit_dir, ensure_palette_png,
    NodeInfo, norm_name, normalize_deg, apply_pivot_origin_from_bbox,
    read_vox_size, parse_vox_sizes, pivot_norm_to_blender_local, apply_pivot_origin,
    ascii_safe_name, parse_pivot_txt, select_final_objects, prepare_clean_dir,
    normalize_obj_material_texture_paths, write_palette_png_deterministic, run_launcher_mode,
    export_static_obj, export_static_glb,
)

# =========================================================================================
# Configuration
# =========================================================================================
# Config
APPLY_PIVOT_WORLD = True   # Enable VXM-pivot-based origin application
KEEP_HIERARCHY_MODE = False # Skip Bake & Join, export full hierarchy
OBJ_IS_PIVOT_CENTERED = False # False = OBJ origin is VoxEdit (0,0,0). True = OBJ origin is Pivot.
IMPORT_ROT_AS_BASE = True  # True=Keep Import Rot as Base, False=Bake Import Rot (Not recommended yet)
EXPORT_BASE_NAME = "ve2rbx_output"
OBJ_EXPORT_DIR_NAME = f"{EXPORT_BASE_NAME}_obj"
GLB_EXPORT_FILE_NAME = f"{EXPORT_BASE_NAME}.glb"
IMPORT_ROOT_NAME = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", os.environ.get("VE2RBX_ASSET_NAME", "").strip()).rstrip(". ") or "VE2RBX"

# =========================================================================================
# Environment Detection & Imports
# =========================================================================================

# Try importing Pillow for External Normalization

# =========================================================================================
# Shared Globals (for Logging)
# =========================================================================================




# =========================================================================================
# Shared Utils (Available to Launcher & Blender)
# =========================================================================================

# =========================================================================================
# PNG Utils
# =========================================================================================





# =========================================================================================
# Launcher Mode (Standard Python)
# =========================================================================================
if not IN_BLENDER:
    run_launcher_mode(Path(__file__).resolve())


# =========================================================================================
# Data Structures (Blender Side)
# =========================================================================================

# =========================================================================================
# Utils
# =========================================================================================










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
            except Exception: pass
            
            try:
                # Remove micro-gaps created by float precision
                bpy.ops.mesh.remove_doubles(threshold=0.000001)
            except Exception:
                try: bpy.ops.mesh.merge_by_distance(distance=0.000001)
                except Exception: pass
                
            bpy.ops.object.mode_set(mode='OBJECT')

            # 2) Shade Flat Force
            try: bpy.ops.object.shade_flat()
            except Exception: pass

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
                except Exception:
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
                except Exception:
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

        export_static_obj(final_objects, export_dir, dest_dir, export_palette, EXPORT_BASE_NAME, OBJ_EXPORT_DIR_NAME)
        export_static_glb(final_objects, export_dir, dest_dir, export_palette, EXPORT_BASE_NAME, GLB_EXPORT_FILE_NAME)
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
