import sys
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "converter"))

import dumper
import vxm_to_vox
import vox2obj

GOLDEN = Path(__file__).parent / "golden"

# gen_golden.py と完全に同じ入力を作るため、同モジュールの関数を再利用する
import gen_golden


def test_vox_bytes_stable(tmp_path):
    vox_path = tmp_path / "Body.vox"
    gen_golden.make_vox(vox_path)
    expected = (GOLDEN / "body_vox_sha256.txt").read_text(encoding="utf-8").strip()
    assert hashlib.sha256(vox_path.read_bytes()).hexdigest() == expected


def test_obj_text_stable(tmp_path):
    vox_path = tmp_path / "Body.vox"
    gen_golden.make_vox(vox_path)
    model = vox2obj.parse_vox(str(vox_path))
    mesh = vox2obj.generate_mesh_data(model)
    obj_path = tmp_path / "Body.obj"
    vox2obj.write_obj(str(obj_path), mesh, "Body", "Body_obj.mtl")
    assert obj_path.read_text(encoding="utf-8") == (GOLDEN / "body_obj.txt").read_text(encoding="utf-8")


def test_dump_output_stable():
    root = dumper.Node("Controller", 1, None, [dumper.Node("Body", 0, "Body.vxm", [])])
    kf = lambda f, v: dumper.KeyFrame(frame=f, ease=1, value=v)
    anim = dumper.AnimData(nodes=[
        dumper.NodeAnim("Controller", [[kf(0, 0.0)], [kf(0, 0.0)], [kf(0, 0.0)],
                                       [dumper.KeyFrame(0, 1, 0.0, slerp=0)], [kf(0, 0.0)], [kf(0, 0.0)]]),
        dumper.NodeAnim("Body", [[kf(0, 0.0), kf(24, 0.0)], [kf(0, 0.0), kf(24, 2.0)], [kf(0, 0.0)],
                                 [dumper.KeyFrame(0, 1, 0.0, slerp=0)], [kf(0, 0.0)], [kf(0, 90.0), kf(24, 90.0)]]),
    ])
    got = "\n".join(dumper.build_dump_output_lines(root, anim)) + "\n"
    assert got == (GOLDEN / "dump_output.txt").read_text(encoding="utf-8")


def test_vxm_parse_fixture_if_available():
    fixture = ROOT / "runtime" / "unit_probe_vxm_warning" / "partial" / "HeadT.vxm"
    if not fixture.exists():
        pytest.skip("local fixture not available")
    data = vxm_to_vox.VxmParser().parse(str(fixture))
    assert data.size == (24, 24, 24)
    assert len(data.voxel_grid) == 732
    assert data.pivot == (0.5000002384185791, 0.041666656732559204, 0.5)
