"""ベースラインのゴールデンファイル生成。項目0で1回だけ実行し、結果をコミットする。"""
import sys
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "converter"))

import dumper
import vxm_to_vox
import vox2obj

GOLDEN = Path(__file__).parent / "golden"
GOLDEN.mkdir(exist_ok=True)


def synthetic_grid():
    # 4x4x4 の直方体 + 上に1ボクセルのエミッシブ。2色。
    normal = ((200, 60, 60, 255), False)
    emissive = ((40, 220, 255, 255), True)
    grid = {}
    for x in range(4):
        for y in range(4):
            for z in range(2):
                grid[(x, y, z)] = normal
    grid[(1, 1, 2)] = emissive
    return grid


def make_vox(path: Path):
    palette = [(200, 60, 60, 255), (40, 220, 255, 255)]
    is_em = [False, True]
    cache = {}
    for i, (c, e) in enumerate(zip(palette, is_em)):
        cache[(c[0], c[1], c[2], c[3], e)] = i
    vxm_to_vox.VoxWriter().write(
        str(path), "Body", (4, 4, 4), synthetic_grid(), palette, is_em, cache
    )


def main():
    work = Path(__file__).parent / "_golden_work"
    work.mkdir(exist_ok=True)

    # 1) VOX バイト列の固定（vxm_to_vox.VoxWriter）
    vox_path = work / "Body.vox"
    make_vox(vox_path)
    (GOLDEN / "body_vox_sha256.txt").write_text(
        hashlib.sha256(vox_path.read_bytes()).hexdigest() + "\n", encoding="utf-8"
    )

    # 2) OBJ テキストの固定（vox2obj: parse→greedy meshing→write_obj）
    model = vox2obj.parse_vox(str(vox_path))
    mesh = vox2obj.generate_mesh_data(model)
    obj_path = work / "Body.obj"
    vox2obj.write_obj(str(obj_path), mesh, "Body", "Body_obj.mtl")
    (GOLDEN / "body_obj.txt").write_text(
        obj_path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    # 3) dumper テキスト整形の固定（Node/AnimData を手組みして出力形式を固定）
    root = dumper.Node("Controller", 1, None, [dumper.Node("Body", 0, "Body.vxm", [])])
    kf = lambda f, v: dumper.KeyFrame(frame=f, ease=1, value=v)
    anim = dumper.AnimData(nodes=[
        dumper.NodeAnim("Controller", [[kf(0, 0.0)], [kf(0, 0.0)], [kf(0, 0.0)],
                                       [dumper.KeyFrame(0, 1, 0.0, slerp=0)], [kf(0, 0.0)], [kf(0, 0.0)]]),
        dumper.NodeAnim("Body", [[kf(0, 0.0), kf(24, 0.0)], [kf(0, 0.0), kf(24, 2.0)], [kf(0, 0.0)],
                                 [dumper.KeyFrame(0, 1, 0.0, slerp=0)], [kf(0, 0.0)], [kf(0, 90.0), kf(24, 90.0)]]),
    ])
    lines = dumper.build_dump_output_lines(root, anim)
    (GOLDEN / "dump_output.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("golden files written to", GOLDEN)


if __name__ == "__main__":
    main()
