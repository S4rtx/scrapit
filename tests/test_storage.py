import zipfile
from pathlib import Path

import pytest

from scrapit.storage import RunStorage, StorageError, find_latest_run, zip_directory
from scrapit.utils import UnsafePathError


@pytest.fixture
def storage(tmp_path):
    return RunStorage(tmp_path / "output", "example.com", timestamp="2026-11-25_14-30-05")


def test_layout_created(storage):
    assert storage.run_dir.parts[-3:] == ("output", "example.com", "2026-11-25_14-30-05")
    assert storage.files_dir.is_dir()
    assert storage.headers_dir.is_dir()


@pytest.mark.parametrize(
    "path, content_type, expected",
    [
        ("/admin", "text/html", "admin/index.html"),
        ("/js", "text/html", "js/index.html"),
        ("/js/", "text/html", "js/index.html"),
        ("/", "text/html", "index.html"),
        ("/robots.txt", "text/plain", "robots.txt"),
        ("/assets/app.js", "application/javascript", "assets/app.js"),
        ("/style", "text/css", "style/index.css"),
        ("/data", "application/json", "data/index.json"),
    ],
)
def test_relative_target(storage, path, content_type, expected):
    assert storage.relative_target(path, content_type) == Path(expected)


def test_query_gets_short_hash(storage):
    rel = storage.relative_target("/page.html?x=1", "text/html")
    assert rel.name.startswith("page_")
    assert rel.name.endswith(".html")
    assert rel.name != "page.html"


def test_query_on_extensionless_path_hashes_index(storage):
    rel = storage.relative_target("/page?x=1", "text/html")
    assert rel.parent == Path("page")
    assert rel.name.startswith("index_")
    assert rel.name.endswith(".html")


def test_directory_with_query_hashes_index(storage):
    rel = storage.relative_target("/docs?x=1", "text/html")
    assert rel.parent == Path("docs")
    assert rel.name.startswith("index_")
    assert rel.name.endswith(".html")


def test_traversal_is_neutralized(storage):
    rel = storage.relative_target("/../../etc/passwd", "text/html")
    assert ".." not in rel.parts
    target = storage.files_dir.joinpath(*rel.parts)
    assert target.resolve().is_relative_to(storage.files_dir.resolve())


def test_reserved_windows_name(storage):
    rel = storage.relative_target("/CON", "text/html")
    assert rel.parts[0] == "_CON"


def test_save_writes_body_and_headers(storage):
    saved = storage.save(
        "/admin",
        "text/html",
        b"<html>ok</html>",
        "https://example.com/admin",
        "https://example.com/admin",
        200,
        [("Server", "test-server"), ("Content-Type", "text/html")],
    )
    assert saved.relative_path == "files/admin/index.html"
    assert saved.headers_path == "headers/admin.headers.txt"
    assert (storage.run_dir / "files" / "admin" / "index.html").read_bytes() == b"<html>ok</html>"
    header_text = (storage.run_dir / "headers" / "admin.headers.txt").read_text(encoding="utf-8")
    assert "Request URL: https://example.com/admin" in header_text
    assert "Status: 200" in header_text
    assert "Server: test-server" in header_text


def test_save_avoids_overwrite(storage):
    first = storage.save("/admin", "text/html", b"one", "u", "u", 200, [])
    second = storage.save("/admin", "text/html", b"two", "u", "u", 200, [])
    assert first.relative_path != second.relative_path
    assert (storage.run_dir / first.relative_path).read_bytes() == b"one"
    assert (storage.run_dir / second.relative_path).read_bytes() == b"two"


def test_relative_output_folder_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    storage = RunStorage("output", "example.com")
    saved = storage.save("/admin", "text/html", b"hi", "u", "u", 200, [])
    assert saved.relative_path == "files/admin/index.html"
    assert (tmp_path / storage.run_dir / "files/admin/index.html").read_bytes() == b"hi"
    assert (tmp_path / storage.run_dir / "headers/admin.headers.txt").is_file()


def test_zip_run_contains_files(storage):
    storage.save("/admin", "text/html", b"hi", "u", "u", 200, [])
    archive = storage.zip_run()
    assert archive.is_file()
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "files/admin/index.html" in names
    assert "headers/admin.headers.txt" in names


def test_zip_directory_missing_raises(tmp_path):
    with pytest.raises(StorageError):
        zip_directory(tmp_path / "missing")


def test_find_latest_run(tmp_path):
    storage = RunStorage(tmp_path / "output", "example.com")
    storage.save("/a", "text/html", b"a", "u", "u", 200, [])
    (storage.run_dir / "results.json").write_text("{}", encoding="utf-8")
    found = find_latest_run(tmp_path / "output")
    assert found == storage.run_dir


def test_find_latest_run_empty(tmp_path):
    assert find_latest_run(tmp_path / "nothing") is None


def test_unsafe_component_is_rejected_at_safe_join(tmp_path):
    with pytest.raises(UnsafePathError):
        from scrapit.utils import safe_join

        safe_join(tmp_path, "..", "x")


def test_run_name_prefixes_run_folder(tmp_path):
    storage = RunStorage(
        tmp_path / "output",
        "example.com",
        timestamp="2026-11-25_14-30-05",
        run_name="evening-scan",
    )
    assert storage.run_dir.name == "evening-scan_2026-11-25_14-30-05"


def test_blank_run_name_falls_back_to_timestamp(tmp_path):
    storage = RunStorage(
        tmp_path / "output",
        "example.com",
        timestamp="2026-11-25_14-30-05",
        run_name="   ",
    )
    assert storage.run_dir.name == "2026-11-25_14-30-05"
