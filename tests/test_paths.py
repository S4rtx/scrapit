from pathlib import Path

import pytest

from scrapit.paths import PathFileError, load_paths, normalize_path_entry


def write_file(tmp_path: Path, content: str, name: str = "paths.txt") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_comments_blanks_and_leading_slash(tmp_path):
    path = write_file(
        tmp_path,
        "/admin\n\n# comment\n   # indented comment\nrobots.txt\n",
    )
    result = load_paths(path, "https://example.com")
    assert result.paths == ["/admin", "/robots.txt"]
    assert result.total_lines == 5
    assert result.skipped == 0


def test_duplicates_removed_and_order_kept(tmp_path):
    path = write_file(tmp_path, "/admin\nadmin\n/login\n/admin/\n/admin\n")
    result = load_paths(path, "https://example.com")
    assert result.paths == ["/admin", "/login", "/admin/"]
    assert result.duplicates_removed == 2


def test_bom_and_crlf(tmp_path):
    path = tmp_path / "paths.txt"
    path.write_bytes(b"\xef\xbb\xbf/admin\r\n/js\r\n")
    result = load_paths(path, "https://example.com")
    assert result.paths == ["/admin", "/js"]


def test_full_url_matching_host_is_accepted(tmp_path):
    path = write_file(tmp_path, "https://example.com/login?next=/home\n")
    result = load_paths(path, "https://example.com")
    assert result.paths == ["/login?next=/home"]


def test_full_url_other_host_is_skipped(tmp_path):
    path = write_file(tmp_path, "https://other.example/login\n/admin\n")
    result = load_paths(path, "https://example.com")
    assert result.paths == ["/admin"]
    assert result.skipped == 1
    assert "outside the target host" in result.warnings[0]


def test_full_url_port_must_match(tmp_path):
    path = write_file(tmp_path, "https://example.com:8443/x\nhttps://example.com/y\n")
    result = load_paths(path, "https://example.com:8443")
    assert result.paths == ["/x"]
    assert result.skipped == 1


def test_full_url_without_target_is_skipped(tmp_path):
    path = write_file(tmp_path, "https://example.com/x\n/admin\n")
    result = load_paths(path)
    assert result.paths == ["/admin"]
    assert result.skipped == 1
    assert "no target" in result.warnings[0]


def test_spaces_are_percent_encoded(tmp_path):
    path = write_file(tmp_path, "/search/my page\n")
    result = load_paths(path, "https://example.com")
    assert result.paths == ["/search/my%20page"]


def test_control_characters_rejected(tmp_path):
    path = tmp_path / "paths.txt"
    path.write_text("/good\n/bad\x01path\n", encoding="utf-8")
    result = load_paths(path, "https://example.com")
    assert result.paths == ["/good"]
    assert result.skipped == 1


def test_fragment_is_removed(tmp_path):
    path = write_file(tmp_path, "/admin#section\n")
    result = load_paths(path, "https://example.com")
    assert result.paths == ["/admin"]


def test_missing_file_raises_friendly_error(tmp_path):
    with pytest.raises(PathFileError, match="not found"):
        load_paths(tmp_path / "nope.txt", "https://example.com")


def test_empty_file_raises(tmp_path):
    path = write_file(tmp_path, "\n\n   \n")
    with pytest.raises(PathFileError, match="empty"):
        load_paths(path, "https://example.com")


def test_no_valid_paths_raises(tmp_path):
    path = write_file(tmp_path, "# only comments\n")
    with pytest.raises(PathFileError, match="No valid paths"):
        load_paths(path, "https://example.com")


def test_summary_counts():
    from scrapit.paths import PathLoadResult

    result = PathLoadResult(paths=["/a", "/b"], duplicates_removed=2, skipped=1)
    assert result.summary == "Loaded 2 path(s) (2 duplicate(s) removed, 1 skipped)"


def test_normalize_entry_unsupported_scheme():
    normalized, warning = normalize_path_entry("ftp://example.com/x", "https://example.com")
    assert normalized is None
    assert "unsupported scheme" in warning
