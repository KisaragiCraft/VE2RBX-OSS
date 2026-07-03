import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "converter"))

from app.server import paths
from app.server.jobs import LocalJob, WARNING_PREFIX
from app.server.main import _parse_multipart, _parse_content_disposition
import modular_runner


# ---- paths.py ----

def test_sanitize_output_name():
    assert paths.sanitize_output_name("My:Model*") == "My_Model_"
    assert paths.sanitize_output_name("   ") == "Unnamed"
    assert paths.sanitize_output_name("...") == "Unnamed"
    assert paths.sanitize_output_name("a" * 100) == "a" * 80
    assert paths.sanitize_output_name("name. ") == "name"


def test_safe_relative_path_ok():
    assert paths.safe_relative_path("a/b.txt") == Path("a/b.txt")
    assert paths.safe_relative_path("./x") == Path("x")
    assert paths.safe_relative_path("dir\\file.vxm") == Path("dir/file.vxm")


@pytest.mark.parametrize("bad", ["../evil", "..\\evil", "/abs/path", "C:evil", "", "   "])
def test_safe_relative_path_rejects(bad):
    with pytest.raises(paths.UploadError):
        paths.safe_relative_path(bad)


def test_detect_project_root_single(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.vxr").write_bytes(b"x")
    (proj / "a.vxa").write_bytes(b"x")
    assert paths.detect_project_root(tmp_path) == proj.resolve()


def test_detect_project_root_none(tmp_path):
    (tmp_path / "only.vxr").write_bytes(b"x")
    assert paths.detect_project_root(tmp_path) is None


def test_detect_project_root_ambiguous(tmp_path):
    for name in ("p1", "p2"):
        d = tmp_path / name
        d.mkdir()
        (d / "a.vxr").write_bytes(b"x")
        (d / "a.vxa").write_bytes(b"x")
    with pytest.raises(paths.UploadError):
        paths.detect_project_root(tmp_path)


def test_safe_extract_zip(tmp_path):
    zp = tmp_path / "in.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("sub/f.txt", "hello")
    out = paths.safe_extract_zip(zp, tmp_path / "out")
    assert (tmp_path / "out" / "sub" / "f.txt").read_text() == "hello"
    assert len(out) == 1


def test_safe_extract_zip_traversal(tmp_path):
    zp = tmp_path / "evil.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(paths.UploadError):
        paths.safe_extract_zip(zp, tmp_path / "out")


# ---- main.py multipart ----

def test_parse_content_disposition():
    got = _parse_content_disposition('form-data; name="files"; filename="a.vxr"')
    assert got["name"] == "files"
    assert got["filename"] == "a.vxr"


def test_parse_multipart():
    body = (
        b'--XBOUND\r\n'
        b'Content-Disposition: form-data; name="include_animation"\r\n\r\n'
        b'true\r\n'
        b'--XBOUND\r\n'
        b'Content-Disposition: form-data; name="files"; filename="a.vxr"\r\n\r\n'
        b'DATA\r\n'
        b'--XBOUND--\r\n'
    )
    fields, files = _parse_multipart(body, "multipart/form-data; boundary=XBOUND")
    assert fields == {"include_animation": "true"}
    assert files == [("a.vxr", b"DATA")]


# ---- jobs.py ----

def _make_job(tmp_path) -> LocalJob:
    return LocalJob(
        job_id="x" * 32,
        project_dir=tmp_path,
        asset_name="Asset",
        output_root=tmp_path / "out",
        output_dir_hint=tmp_path / "out" / "Asset",
        log_path=tmp_path / "job.log",
        include_animation=False,
        command=["dummy"],
    )


def test_job_warning_status(tmp_path):
    from app.server.jobs import JobManager
    jm = JobManager.__new__(JobManager)
    job = _make_job(tmp_path)
    job.exit_code = 0
    job.log_path.write_text("ok\n", encoding="utf-8")
    assert jm._final_status(job) == "completed"
    job.log_path.write_text(f"{WARNING_PREFIX} skipped part\n", encoding="utf-8")
    assert jm._final_status(job) == "completed_with_warnings"
    job.exit_code = 1
    assert jm._final_status(job) == "failed"


# ---- modular_runner.py ----

def test_sanitize_name():
    assert modular_runner.sanitize_name('a<b>:c') == "a_b__c"
    assert modular_runner.sanitize_name("") == "Unnamed"
    assert modular_runner.sanitize_name("x" * 100) == "x" * 60


def test_luau_long_string():
    assert modular_runner.luau_long_string("Hero") == "[[Hero]]"
    assert modular_runner.luau_long_string("a]]b") == "[=[a]]b]=]"


def test_copy_obj_dir_with_base(tmp_path):
    src = tmp_path / "old_obj"
    src.mkdir()
    (src / "old.obj").write_text("mtllib old.mtl\nv 0 0 0\n", encoding="utf-8")
    (src / "old.mtl").write_text("newmtl palette\n", encoding="utf-8")
    dst = tmp_path / "New_obj"
    modular_runner.copy_obj_dir_with_base(src, dst, "New")
    assert (dst / "New.obj").exists()
    assert (dst / "New.mtl").exists()
    assert "mtllib New.mtl" in (dst / "New.obj").read_text(encoding="utf-8")
