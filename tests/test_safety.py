import pytest

from scrapit.safety import (
    RobotsRules,
    SafetyError,
    check_url_allowed,
    estimate_requests,
    host_confirmation_matches,
    load_robots,
    parse_robots,
    primary_ip,
)

PUBLIC = "93.184.216.34"


def public_resolver(host):
    return [PUBLIC]


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.5", "172.20.1.1", "192.168.0.10", "169.254.1.1", "::1", "fd00::1"],
)
def test_private_target_refused(ip):
    with pytest.raises(SafetyError, match="non-public"):
        check_url_allowed("https://example.com", resolver=lambda host: [ip])


def test_public_target_allowed():
    assert check_url_allowed("https://example.com", resolver=public_resolver) == [PUBLIC]


def test_allow_private_flag_permits_localhost():
    assert check_url_allowed(
        "http://127.0.0.1:8000", allow_private=True, resolver=lambda host: ["127.0.0.1"]
    ) == ["127.0.0.1"]


def test_mixed_addresses_with_one_private_are_refused():
    with pytest.raises(SafetyError):
        check_url_allowed("https://example.com", resolver=lambda host: [PUBLIC, "10.0.0.1"])


def test_dns_failure_is_a_safety_error():
    def broken(host):
        raise OSError("DNS lookup failed")

    with pytest.raises(SafetyError, match="DNS lookup failed"):
        check_url_allowed("https://example.com", resolver=broken)


def test_unsupported_scheme_refused():
    with pytest.raises(SafetyError, match="Unsupported scheme"):
        check_url_allowed("ftp://example.com", resolver=public_resolver)


def test_primary_ip():
    assert primary_ip("https://example.com", resolver=public_resolver) == PUBLIC


def test_host_confirmation_matches_exactly():
    assert host_confirmation_matches("example.com", "https://example.com")
    assert host_confirmation_matches("  EXAMPLE.COM  ", "https://example.com/admin")
    assert not host_confirmation_matches("example.com.evil", "https://example.com")
    assert not host_confirmation_matches("", "https://example.com")


def test_host_confirmation_accepts_pasted_variants():
    assert host_confirmation_matches("https://example.com", "https://example.com")
    assert host_confirmation_matches("https://example.com/admin?x=1", "https://example.com/admin")
    assert host_confirmation_matches("example.com:8443", "https://example.com:8443/x")
    assert host_confirmation_matches("example.com.", "https://example.com")
    assert host_confirmation_matches("example.com/path", "https://example.com")
    assert host_confirmation_matches("пример.рф", "https://xn--e1afmkfd.xn--p1ai")
    assert not host_confirmation_matches("https://evil.com/example.com", "https://example.com")


def test_estimate_requests_includes_robots_and_probe():
    assert estimate_requests(10, True) == 12
    assert estimate_requests(10, False) == 11


def test_robots_disallow_rule():
    rules = parse_robots(200, "User-agent: *\nDisallow: /admin\n")
    assert rules.fetched is True
    assert rules.allowed("https://example.com/admin", "Scrapit/1.0") is False
    assert rules.allowed("https://example.com/public", "Scrapit/1.0") is True


def test_robots_deny_all_on_auth_required():
    rules = parse_robots(401, "")
    assert rules.deny_all is True
    assert rules.allowed("https://example.com/", "Scrapit/1.0") is False


def test_robots_missing_allows_all():
    rules = parse_robots(404, "")
    assert rules.deny_all is False
    assert rules.allowed("https://example.com/anything", "Scrapit/1.0") is True


def test_robots_parser_errors_allow_all():
    class Broken:
        def can_fetch(self, agent, url):
            raise RuntimeError("boom")

    rules = RobotsRules(parser=Broken())
    assert rules.allowed("https://example.com/x", "ua") is True


def test_load_robots_uses_fetch_callable():
    def fake_fetch(url):
        assert url == "https://example.com/robots.txt"
        return 200, b"User-agent: *\nDisallow: /private\n", ""

    rules = load_robots(fake_fetch, "https://example.com")
    assert rules.allowed("https://example.com/private", "Scrapit/1.0") is False


def test_load_robots_failure_allows_all():
    def broken(url):
        raise OSError("network down")

    rules = load_robots(broken, "https://example.com")
    assert rules.allowed("https://example.com/x", "Scrapit/1.0") is True
    assert "unavailable" in rules.note
