"""Blenderステージの疑似E2E。使い方: python tests/e2e_blender.py <出力先dir>"""
import os
import subprocess
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "converter"))

import modular_runner
import vox2obj
import gen_golden

VXR_VXA_TEXT = """# Node Hierarchy
 [0] Controller
  └ [1] Body / Body.vxm

# Animation
[0] Controller：
0KF => POS X:0.000000,Y:0.000000,Z:0.000000  / ROT X:0.000000,Y:0.000000,Z:0.000000 | EASE PX:1(linear),PY:1(linear),PZ:1(linear),RX:1(linear),RY:1(linear),RZ:1(linear) | SLERP:0(off)

[1] Body：
0KF => POS X:0.000000,Y:0.000000,Z:0.000000  / ROT X:0.000000,Y:0.000000,Z:90.000000 | EASE PX:1(linear),PY:1(linear),PZ:1(linear),RX:1(linear),RY:1(linear),RZ:1(linear) | SLERP:0(off)
24KF => POS X:0.000000,Y:2.000000,Z:0.000000  / ROT X:0.000000,Y:0.000000,Z:90.000000 | EASE PX:1(linear),PY:1(linear),PZ:1(linear),RX:1(linear),RY:1(linear),RZ:1(linear) | SLERP:0(off)
"""


def build_edit_dir(edit_dir: Path) -> None:
    for sub in ("log", "OutputVOX", "OutputOBJ", "OutputFBX"):
        (edit_dir / sub).mkdir(parents=True, exist_ok=True)
    (edit_dir / "VXR&VXA.txt").write_text(VXR_VXA_TEXT, encoding="utf-8")
    (edit_dir / "Pivot.txt").write_text("Body.vxm: 0.5 0.0 0.5\n", encoding="utf-8")
    gen_golden.make_vox(edit_dir / "OutputVOX" / "Body.vox")
    vox2obj.run_ve2rbx(edit_dir)


def run_blender(script: str, edit_dir: Path, ascii_root: Path) -> None:
    blender = modular_runner.get_blender_exe()
    if not blender:
        print("SKIP: Blender not found. E2E不可 → 計画のゲート判断に従うこと")
        sys.exit(2)
    env = os.environ.copy()
    env["VE2RBX_ASSET_NAME"] = "SynthAsset"
    env["VE2RBX_ASCII_EXPORT_ROOT"] = str(ascii_root)
    log = edit_dir / "log" / f"{Path(script).stem}.log"
    cmd = [blender, "-b", "-P", str(ROOT / "converter" / script), "--",
           modular_runner.to_short_path(edit_dir), "--log_path", str(log)]
    print("EXEC:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def strip_comments(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.startswith("#"))


def main():
    out = Path(sys.argv[1]).resolve()
    if out.exists():
        shutil.rmtree(out)
    edit_dir = out / "edit"
    ascii_root = out / "ascii"
    ascii_root.mkdir(parents=True, exist_ok=True)
    build_edit_dir(edit_dir)

    run_blender("OBJ2FBX.py", edit_dir, ascii_root)
    run_blender("OBJ2FBXanimation.py", edit_dir, ascii_root)

    fbx_dir = edit_dir / "OutputFBX"
    expected = [
        "ve2rbx_output.fbx", "ve2rbx_output.blend", "ve2rbx_output.glb", "palette.png",
        "ve2rbx_output_anim.fbx", "ve2rbx_output_anim.blend", "ve2rbx_output_anim.glb",
        "ve2rbx_output_anim_model.fbx",
    ]
    missing = [n for n in expected if not (fbx_dir / n).exists()]
    if not (fbx_dir / "ve2rbx_output_obj" / "ve2rbx_output.obj").exists():
        missing.append("ve2rbx_output_obj/ve2rbx_output.obj")
    clips = list(fbx_dir.glob("ve2rbx_output_anim_animclip_*.fbx"))
    if not clips:
        missing.append("animclip fbx")
    if missing:
        print("FAILED. missing:", missing)
        sys.exit(1)

    snap = strip_comments((fbx_dir / "ve2rbx_output_obj" / "ve2rbx_output.obj").read_text(encoding="utf-8"))
    (Path(__file__).parent / "golden" / "e2e_blender_obj.txt").write_text(snap + "\n", encoding="utf-8")
    print("OK. files:", sorted(p.name for p in fbx_dir.iterdir()))


if __name__ == "__main__":
    main()
