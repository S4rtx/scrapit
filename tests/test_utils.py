
import pytest

from scrapit.utils import (
    UnsafePathError,
    UrlError,
    build_url,
    encode_path,
    ext_from_content_type,
    has_extension,
    human_bytes,
    ip_is_blocked,
    normalize_url,
    parse_retry_after,
    safe_join,
    sanitize_component,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("example.com", "https://example.com"),
        ("HTTP://Example.COM/", "http://example.com"),
        ("https://example.com/", "https://example.com"),
        ("  https://example.com/a/b?x=1#frag  ", "https://example.com"),
        ("https://example.com:8443/path", "https://example.com:8443"),
        ("https://example.com:443/", "https://example.com"),
        ("http://example.com:80/", "http://example.com"),
        ("example.com:8080", "https://example.com:8080"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ftp://example.com", "https://", "https://exa mple.com"])
def test_normalize_url_rejects_bad_input(raw):
    with pytest.raises(UrlError):
        normalize_url(raw)


def test_build_url_keeps_host():
    assert build_url("https://example.com", "/admin") == "https://example.com/admin"
    assert build_url("https://example.com/", "robots.txt") == "https://example.com/robots.txt"
    assert build_url("https://example.com", "//evil.example/x") == "https://example.com/evil.example/x"
    assert build_url("https://example.com", "/javascript:alert(1)").startswith("https://example.com/")


def test_encode_path_only_unsafe():
    assert encode_path("/a b") == "/a%20b"
    assert encode_path("/a?b=c&d=e") == "/a?b=c&d=e"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("NUL", "_NUL"),
        ("a<b>.html", "a_b_.html"),
        ("a:b|c?d*e", "a_b_c_d_e"),
        ("..", "_"),
        ("", "_"),
        ("trailing. ", "trailing"),
        ("café.html", "café.html"),
    ],
)
def test_sanitize_component(name, expected):
    assert sanitize_component(name) == expected


def test_sanitize_traversal_never_contains_slash():
    cleaned = sanitize_component("../../etc/passwd")
    assert "/" not in cleaned
    assert cleaned != ".."


def test_sanitize_long_name_preserves_extension():
    cleaned = sanitize_component("a" * 150 + ".html")
    assert len(cleaned) <= 100
    assert cleaned.endswith(".html")


def test_has_extension():
    assert has_extension("app.js")
    assert has_extension(".env")
    assert not has_extension("admin")
    assert not has_extension("trailing.")


@pytest.mark.parametrize(
    "content_type, expected",
    [
        ("text/html; charset=utf-8", ".html"),
        ("application/javascript", ".js"),
        ("text/css", ".css"),
        ("application/json", ".json"),
        ("text/plain", ".txt"),
        ("application/ld+json", ".json"),
        ("image/svg+xml", ".svg"),
        ("application/octet-stream", ".bin"),
        (None, ".bin"),
    ],
)
def test_ext_from_content_type(content_type, expected):
    assert ext_from_content_type(content_type) == expected


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.10.10",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "224.0.0.1",
    ],
)
def test_private_or_reserved_ips_are_blocked(ip):
    assert ip_is_blocked(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700::1111"])
def test_public_ips_allowed(ip):
    assert ip_is_blocked(ip) is False


def test_safe_join_blocks_escape(tmp_path):
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, "..", "escape.txt")
    target = safe_join(tmp_path, "a", "b.txt")
    assert target == (tmp_path / "a" / "b.txt").resolve()


def test_parse_retry_after_numeric():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("not-a-date") is None
    assert parse_retry_after(None) is None


def test_human_bytes():
    assert human_bytes(0) == "0 B"
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(10 * 1024 * 1024) == "10.0 MB"
