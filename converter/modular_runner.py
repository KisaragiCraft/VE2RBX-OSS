import argparse
import ctypes
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def sanitize_name(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", name)
    safe = safe.rstrip(". ")
    if not safe:
        safe = "Unnamed"
    if len(safe) > 60:
        safe = safe[:60]
    return safe


def get_base_root() -> Path:
    default_root = Path(os.environ.get("VE2RBX_OSS_OUTPUT_ROOT", os.path.expandvars(r"%USERPROFILE%\Documents\VE2RBXoutput")))
    config_path = default_root / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            custom_root = data.get("output_root")
            if custom_root:
                return Path(os.path.expandvars(custom_root))
        except Exception:
            pass
    default_root.mkdir(parents=True, exist_ok=True)
    return default_root


def load_config() -> dict:
    config_path = get_base_root() / "config.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_blender_exe() -> str | None:
    cfg = load_config()
    configured = str(cfg.get("blender_exe") or "").strip()
    if configured:
        expanded = os.path.expandvars(configured)
        if os.path.isfile(expanded):
            return expanded
        found = shutil.which(expanded)
        if found:
            return found

    for cmd in ("blender", "blender.exe"):
        found = shutil.which(cmd)
        if found:
            return found

    search_roots = [
        r"C:\Program Files\Blender Foundation",
        r"C:\Program Files (x86)\Blender Foundation",
    ]
    version_pattern = re.compile(r"Blender\s+(\d+)(?:\.(\d+))?")
    candidates: list[tuple[tuple[int, int], str]] = []
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for folder_name in os.listdir(root):
            blender_exe = os.path.join(root, folder_name, "blender.exe")
            if not os.path.isfile(blender_exe):
                continue
            match = version_pattern.match(folder_name)
            if match:
                candidates.append(((int(match.group(1)), int(match.group(2) or 0)), blender_exe))
            else:
                candidates.append(((0, 0), blender_exe))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def copy_file_with_fbm(src_fbx: Path, dst_fbx: Path) -> None:
    shutil.copy2(src_fbx, dst_fbx)
    src_fbm = src_fbx.with_suffix(".fbm")
    dst_fbm = dst_fbx.with_suffix(".fbm")
    if src_fbm.exists():
        if dst_fbm.exists():
            shutil.rmtree(dst_fbm)
        shutil.copytree(src_fbm, dst_fbm)


def make_unique_dir(parent: Path, base_name: str) -> tuple[Path, int]:
    safe_base = sanitize_name(base_name)
    for idx in range(1, 1000):
        folder_name = safe_base if idx == 1 else f"{safe_base}_{idx:02d}"
        candidate = parent / folder_name
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate, idx
    raise RuntimeError(f"Failed to allocate output folder for {safe_base} under {parent}")


def _rename_file_if_needed(src: Path, dst: Path) -> None:
    if not src.exists() or src == dst:
        return
    if dst.exists():
        dst.unlink()
    src.rename(dst)


def copy_obj_dir_with_base(src_obj_dir: Path, dst_obj_dir: Path, output_base_name: str) -> None:
    if dst_obj_dir.exists():
        shutil.rmtree(dst_obj_dir)
    shutil.copytree(src_obj_dir, dst_obj_dir)

    src_base_name = src_obj_dir.name.removesuffix("_obj")
    dst_obj = dst_obj_dir / f"{output_base_name}.obj"
    dst_mtl = dst_obj_dir / f"{output_base_name}.mtl"
    src_obj = dst_obj_dir / f"{src_base_name}.obj"
    src_mtl = dst_obj_dir / f"{src_base_name}.mtl"

    _rename_file_if_needed(src_mtl, dst_mtl)
    _rename_file_if_needed(src_obj, dst_obj)

    if dst_obj.exists() and src_mtl.name != dst_mtl.name:
        text = dst_obj.read_text(encoding="utf-8", errors="replace")
        text = text.replace(f"mtllib {src_mtl.name}", f"mtllib {dst_mtl.name}")
        dst_obj.write_text(text, encoding="utf-8")


def luau_long_string(value: str) -> str:
    for equals_count in range(0, 8):
        marker = "=" * equals_count
        if f"]{marker}]" not in value:
            return f"[{marker}[{value}]{marker}]"
    return "[[Unnamed]]"


def write_roblox_post_import_script(bundle_dir: Path, asset_name: str) -> Path:
    script_path = bundle_dir / f"{asset_name}_roblox_post_import.luau"
    root_name = luau_long_string(asset_name)
    script_path.write_text(
        "\n".join(
            [
                "-- VE2RBX post-import helper for Roblox Studio.",
                "-- Run this in the command bar after importing when the asset is used as a visual model.",
                f"local ROOT_NAME = {root_name}",
                "local roots = {}",
                "for _, child in ipairs(workspace:GetChildren()) do",
                "    if child.Name == ROOT_NAME then",
                "        table.insert(roots, child)",
                "    end",
                "end",
                "if #roots == 0 then",
                "    error(('VE2RBX imported model not found: %s'):format(ROOT_NAME))",
                "end",
                "",
                "for _, root in ipairs(roots) do",
                "    for _, inst in ipairs(root:GetDescendants()) do",
                "        if inst:IsA('BasePart') then",
                "            inst.Anchored = true",
                "            inst.CanCollide = false",
                "            inst.CanTouch = false",
                "            inst.CanQuery = true",
                "        end",
                "    end",
                "end",
                "",
                "print(('VE2RBX post-import settings applied to %d model(s) named %s'):format(#roots, ROOT_NAME))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return script_path


def create_edit_dir(project_dir: Path, base_root: Path | None = None, asset_name: str | None = None) -> tuple[Path, str, str, Path, int]:
    base_root = base_root or get_base_root()
    work_root = base_root / "Work Data"
    work_root.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    asset_base = sanitize_name(asset_name or project_dir.name)
    idx = 1
    while True:
        work_dir_name = f"{asset_base}_{timestamp}" if idx == 1 else f"{asset_base}_{timestamp}_{idx:02d}"
        edit_dir = work_root / work_dir_name
        if not edit_dir.exists():
            break
        idx += 1

    (edit_dir / "log").mkdir(parents=True, exist_ok=True)
    (edit_dir / "OutputVOX").mkdir(parents=True, exist_ok=True)
    (edit_dir / "OutputOBJ").mkdir(parents=True, exist_ok=True)
    (edit_dir / "OutputFBX").mkdir(parents=True, exist_ok=True)
    with open(edit_dir / "project_info.txt", "w", encoding="utf-8") as f:
        f.write(f"Asset Name: {asset_base}\n")
        f.write(f"Run Name: {edit_dir.name}\n")
        f.write(f"Project Name: {project_dir.name}\n")
        f.write(f"Project Path: {project_dir}\n")
    return edit_dir, timestamp, asset_base, base_root, idx


def write_log_header(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Created by KisaragiKoubou | Modular Runner\n")


def to_short_path(path: Path) -> str:
    raw = str(path)
    if os.name != "nt":
        return raw
    buffer_size = 4096
    buffer = ctypes.create_unicode_buffer(buffer_size)
    result = ctypes.windll.kernel32.GetShortPathNameW(raw, buffer, buffer_size)
    if result == 0 or result > buffer_size:
        return raw
    return buffer.value


def append_process_output(log_path: Path, result: subprocess.CompletedProcess[str]) -> None:
    if not (result.stdout or result.stderr):
        return
    with open(log_path, "a", encoding="utf-8") as f:
        if result.stdout:
            f.write("\n=== Blender stdout ===\n")
            f.write(result.stdout)
        if result.stderr:
            f.write("\n=== Blender stderr ===\n")
            f.write(result.stderr)


def restore_runner_streams(stdout, stderr) -> None:
    sys.stdout = stdout
    sys.stderr = stderr


def main() -> int:
    parser = argparse.ArgumentParser(description="Run VE2RBX modules directly without the packaged exe")
    parser.add_argument("project_dir", help="VoxEdit project directory")
    parser.add_argument("--skip-animation", action="store_true", help="Skip OBJ2FBXanimation export")
    parser.add_argument("--output-root", help="Override output root directory")
    parser.add_argument("--work-root", help="Override temporary work root directory")
    parser.add_argument("--output-dir", help="Exact final output bundle directory")
    parser.add_argument("--asset-name", help="Output base name derived from the VoxEdit project")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"Project directory not found: {project_dir}", file=sys.stderr)
        return 1

    source_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(source_dir))

    import dumper
    import vxm_to_vox
    import vox2obj

    output_root = Path(args.output_root).resolve() if args.output_root else get_base_root()
    work_root = Path(args.work_root).resolve() if args.work_root else output_root
    explicit_output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    edit_dir, timestamp_str, run_name, _work_root, run_idx = create_edit_dir(project_dir, work_root, args.asset_name)
    main_log_path = edit_dir / "log" / "modular_runner.log"
    write_log_header(main_log_path)
    runner_stdout = sys.stdout
    runner_stderr = sys.stderr

    def log(message: str) -> None:
        print(message)
        with open(main_log_path, "a", encoding="utf-8") as f:
            f.write(message + "\n")

    timings: dict[str, float] = {}

    def timed(stage_name: str, fn):
        start = time.perf_counter()
        result = fn()
        timings[stage_name] = time.perf_counter() - start
        log(f"[Timing] {stage_name}: {timings[stage_name]:.2f}s")
        return result

    log("========================================")
    log(" VE2RBX Modular Runner")
    log("========================================")
    log(f"Project Dir: {project_dir}")
    log(f"Asset Name : {run_name}")
    log(f"Edit Dir   : {edit_dir}")
    log(f"Work Root  : {work_root}")
    log(f"Output Root: {output_root}")
    if explicit_output_dir:
        log(f"Output Dir : {explicit_output_dir}")

    dumper_log = edit_dir / "log" / "dumper.log"
    log("\n[1/6] Running dumper...")
    timed("dumper", lambda: dumper.run_ve2rbx(project_dir, edit_dir, dumper_log))

    log("\n[2/6] Running vxm_to_vox...")
    restore_runner_streams(runner_stdout, runner_stderr)
    vxm_to_vox.setup_logger(str(edit_dir / "log" / "vxm2vox.log"))
    timed("vxm_to_vox", lambda: vxm_to_vox.run_ve2rbx(project_dir, edit_dir))
    restore_runner_streams(runner_stdout, runner_stderr)

    log("\n[3/6] Running vox2obj...")
    restore_runner_streams(runner_stdout, runner_stderr)
    vox2obj.setup_logger(str(edit_dir / "log" / "vox2obj.log"))
    timed("vox2obj", lambda: vox2obj.run_ve2rbx(edit_dir))
    restore_runner_streams(runner_stdout, runner_stderr)

    blender_exe = get_blender_exe()
    if not blender_exe:
        log("Blender executable could not be resolved.")
        return 1
    log(f"[Blender] Auto-selected: {blender_exe}")

    obj2fbx_script = Path(to_short_path(source_dir / "OBJ2FBX.py"))
    obj2fbx_anim_script = Path(to_short_path(source_dir / "OBJ2FBXanimation.py"))
    edit_dir_for_blender = to_short_path(edit_dir)
    ascii_stage_root = work_root / "_ascii_stage"
    ascii_stage_root.mkdir(parents=True, exist_ok=True)
    ascii_stage_root_short = to_short_path(ascii_stage_root)
    blender_env = os.environ.copy()
    blender_env["VE2RBX_ASCII_EXPORT_ROOT"] = ascii_stage_root_short
    blender_env["VE2RBX_ASSET_NAME"] = run_name

    log("\n[4/6] Running OBJ2FBX (Blender)...")
    obj2fbx_log = edit_dir / "log" / "obj2fbx.log"
    cmd_fbx = [
        blender_exe,
        "-b",
        "-P",
        str(obj2fbx_script),
        "--",
        edit_dir_for_blender,
        "--log_path",
        str(obj2fbx_log),
    ]
    log(f"Exec: {' '.join(cmd_fbx)}")
    result = timed(
        "obj2fbx",
        lambda: subprocess.run(
            cmd_fbx,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=blender_env,
        ),
    )
    append_process_output(obj2fbx_log, result)

    obj2fbx_anim_log = edit_dir / "log" / "obj2fbx_animation.log"
    if not args.skip_animation and obj2fbx_anim_script.exists():
        log("\n[5/6] Running OBJ2FBXanimation (Blender)...")
        cmd_anim = [
            blender_exe,
            "-b",
            "-P",
            str(obj2fbx_anim_script),
            "--",
            edit_dir_for_blender,
            "--log_path",
            str(obj2fbx_anim_log),
        ]
        log(f"Exec: {' '.join(cmd_anim)}")
        anim_result = timed(
            "obj2fbx_animation",
            lambda: subprocess.run(
                cmd_anim,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=blender_env,
            ),
        )
        append_process_output(obj2fbx_anim_log, anim_result)
    else:
        log("\n[5/6] Skipping OBJ2FBXanimation.")

    log("\n[6/6] Finalizing outputs...")
    src_dir = edit_dir / "OutputFBX"
    src_fbx = src_dir / "ve2rbx_output.fbx"
    src_blend = src_dir / "ve2rbx_output.blend"
    src_png = src_dir / "palette.png"
    src_glb = src_dir / "ve2rbx_output.glb"
    src_obj_dir = src_dir / "ve2rbx_output_obj"
    src_anim_fbx = src_dir / "ve2rbx_output_anim.fbx"
    src_anim_blend = src_dir / "ve2rbx_output_anim.blend"
    src_anim_glb = src_dir / "ve2rbx_output_anim.glb"
    src_anim_model_fbx = src_dir / "ve2rbx_output_anim_model.fbx"
    src_animclip_files = sorted(src_dir.glob("ve2rbx_output_anim_animclip_*.fbx"), key=lambda p: p.name.lower())

    missing = [path.name for path in (src_fbx, src_blend, src_png, src_glb, src_obj_dir) if not path.exists()]
    if missing:
        log(f"Critical error: missing output files: {', '.join(missing)}")
        return 1

    if explicit_output_dir:
        bundle_dir = explicit_output_dir
        bundle_dir.mkdir(parents=True, exist_ok=True)
    else:
        bundle_dir, _bundle_idx = make_unique_dir(output_root, run_name)

    dst_fbx = bundle_dir / f"{run_name}.fbx"
    dst_blend = bundle_dir / f"{run_name}.blend"
    dst_png = bundle_dir / f"{run_name}.png"
    dst_glb = bundle_dir / f"{run_name}.glb"
    dst_obj_dir = bundle_dir / f"{run_name}_obj"
    dst_anim_fbx = bundle_dir / f"{run_name}_anim.fbx"
    dst_anim_blend = bundle_dir / f"{run_name}_anim.blend"
    dst_anim_glb = bundle_dir / f"{run_name}_anim.glb"
    dst_anim_model_fbx = bundle_dir / f"{run_name}_model.fbx"

    copy_file_with_fbm(src_fbx, dst_fbx)
    shutil.copy2(src_blend, dst_blend)
    shutil.copy2(src_png, dst_png)
    shutil.copy2(src_glb, dst_glb)
    copy_obj_dir_with_base(src_obj_dir, dst_obj_dir, run_name)
    post_import_script = write_roblox_post_import_script(bundle_dir, run_name)

    if src_anim_fbx.exists():
        copy_file_with_fbm(src_anim_fbx, dst_anim_fbx)
    if src_anim_blend.exists():
        shutil.copy2(src_anim_blend, dst_anim_blend)
    if src_anim_glb.exists():
        shutil.copy2(src_anim_glb, dst_anim_glb)
    if src_anim_model_fbx.exists():
        copy_file_with_fbm(src_anim_model_fbx, dst_anim_model_fbx)

    exported_animclips: list[Path] = []
    for src_animclip in src_animclip_files:
        clip_suffix = src_animclip.stem.removeprefix("ve2rbx_output_anim_animclip_")
        dst_animclip = bundle_dir / f"{run_name}_animclip_{clip_suffix}.fbx"
        copy_file_with_fbm(src_animclip, dst_animclip)
        exported_animclips.append(dst_animclip)

    log("----------------------------------------")
    log("All processes completed successfully.")
    log(f"Final Output Folder: {bundle_dir}")
    log(f"Final FBX: {dst_fbx}")
    log(f"Final GLB: {dst_glb}")
    log(f"Final OBJ Folder: {dst_obj_dir}")
    log(f"Roblox Post-Import Helper: {post_import_script}")
    if exported_animclips:
        log(f"Animation Clip Count: {len(exported_animclips)}")
    total_time = sum(timings.values())
    log(f"Stage Timing Summary: {json.dumps({k: round(v, 2) for k, v in timings.items()}, ensure_ascii=False)}")
    log(f"Total Timed Runtime: {total_time:.2f}s")
    log("----------------------------------------")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
