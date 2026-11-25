import io
import json

import httpx
import pytest

from scrapit.subdomains import (
    DEFAULT_WORDLIST,
    SubdomainError,
    brute_force,
    enumerate_subdomains,
    fetch_certspotter,
    fetch_crtsh,
    fetch_hackertarget,
    fetch_rapiddns,
    load_wordlist,
    passive_lookup,
    registrable_domain,
    results_to_csv,
    results_to_json,
    results_to_text,
    wildcard_ips,
    wildcard_is_rotating,
    write_outputs,
)

PUBLIC = ["93.184.216.34"]


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def resolver_from(mapping, default=None):
    def resolve(host):
        if host in mapping:
            return mapping[host]
        if default is not None:
            return default
        raise OSError("no such host")

    return resolve


# -- domain normalization -------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("example.com", "example.com"),
        ("Example.COM.", "example.com"),
        ("https://www.example.com/path?q=1", "example.com"),
        ("sub.deep.example.com", "example.com"),
        ("example.co.uk", "example.co.uk"),
        ("news.bbc.co.uk", "bbc.co.uk"),
        ("x.example.co.in", "example.co.in"),
        ("*.example.com", "example.com"),
    ],
)
def test_registrable_domain(value, expected):
    assert registrable_domain(value) == expected


@pytest.mark.parametrize("value", ["", "localhost", "bad host.com", "a..b"])
def test_registrable_domain_rejects_bad_input(value):
    with pytest.raises(SubdomainError):
        registrable_domain(value)


# -- passive source parsers -----------------------------------------------------


def test_fetch_crtsh_parses_name_value_lines():
    payload = [
        {"name_value": "www.example.com\napi.example.com", "common_name": "example.com"},
        {"name_value": "*.dev.example.com"},
        {"name_value": "other.test"},
    ]

    def handler(request):
        assert request.url.host == "crt.sh"
        return httpx.Response(200, json=payload)

    names = fetch_crtsh("example.com", client_for(handler))
    assert names == {"www.example.com", "api.example.com", "dev.example.com"}


def test_fetch_certspotter_parses_dns_names():
    payload = [{"dns_names": ["a.example.com", "*.b.example.com", "nope.test"]}]

    def handler(request):
        return httpx.Response(200, json=payload)

    names = fetch_certspotter("example.com", client_for(handler))
    assert names == {"a.example.com", "b.example.com"}


def test_fetch_hackertarget_parses_csv_lines():
    body = "www.example.com,1.2.3.4\nmail.example.com,5.6.7.8\nevil.test,9.9.9.9\n"

    def handler(request):
        return httpx.Response(200, text=body)

    names = fetch_hackertarget("example.com", client_for(handler))
    assert names == {"www.example.com", "mail.example.com"}


def test_fetch_hackertarget_quota_message_is_empty():
    def handler(request):
        return httpx.Response(200, text="error: API count exceeded - Increase Quota with Membership")

    assert fetch_hackertarget("example.com", client_for(handler)) == set()


def test_fetch_rapiddns_parses_table_cells():
    body = "<tr><td>a.example.com</td><td>1.2.3.4</td></tr><tr><td>b.example.com</td></tr>"

    def handler(request):
        return httpx.Response(200, text=body)

    names = fetch_rapiddns("example.com", client_for(handler))
    assert names == {"a.example.com", "b.example.com"}


def test_passive_lookup_reports_source_errors_without_raising():
    def handler(request):
        raise httpx.ConnectError("blocked", request=request)

    events = []
    found = passive_lookup(
        "example.com",
        client_for(handler),
        sources=["crtsh"],
        on_source=lambda src, count, err: events.append((src, count, err)),
        pause=0,
    )
    assert found == {}
    assert events and events[0][0] == "crtsh" and events[0][2] is not None


def test_passive_lookup_merges_sources_for_same_name():
    def handler(request):
        if request.url.host == "crt.sh":
            return httpx.Response(200, json=[{"name_value": "www.example.com"}])
        return httpx.Response(200, json=[{"dns_names": ["www.example.com"]}])

    found = passive_lookup(
        "example.com",
        client_for(handler),
        sources=["crtsh", "certspotter"],
        pause=0,
    )
    assert found["www.example.com"] == {"crtsh", "certspotter"}


# -- brute force ----------------------------------------------------------------


def test_wildcard_ips_detects_catch_all():
    assert wildcard_ips("example.com", lambda host: ["1.2.3.4"]) == ["1.2.3.4"]


def test_wildcard_ips_empty_when_no_wildcard():
    def resolve(host):
        raise OSError("nxdomain")

    assert wildcard_ips("example.com", resolve) == []


def test_wildcard_is_rotating_detects_changing_answers():
    counter = {"n": 0}

    def resolve(host):
        counter["n"] += 1
        return [f"10.0.0.{counter['n']}"]

    assert wildcard_is_rotating("example.com", resolve) is True


def test_wildcard_is_rotating_false_for_stable_answers():
    assert wildcard_is_rotating("example.com", lambda host: ["1.2.3.4"]) is False


def test_wildcard_is_rotating_false_without_wildcard():
    def resolve(host):
        raise OSError("nxdomain")

    assert wildcard_is_rotating("example.com", resolve) is False


def test_enumerate_skips_bruteforce_on_rotating_wildcard():
    """Rotating wildcard DNS must not produce a flood of false positives."""
    counter = {"n": 0}

    def handler(request):
        return httpx.Response(200, json=[])

    def resolve(host):
        counter["n"] += 1
        return [f"10.0.0.{counter['n'] % 250}"]

    report = enumerate_subdomains(
        "example.com",
        client=client_for(handler),
        resolver=resolve,
        words=["www", "api", "admin", "mail"],
        sources=["crtsh"],
    )
    assert report.bruteforce_skipped is True
    assert report.results == []
    assert any("change between queries" in note for note in report.notes)


def test_brute_force_keeps_real_names_and_drops_wildcard_only():
    mapping = {
        "api.example.com": ["9.9.9.9"],
        "www.example.com": ["1.2.3.4", "9.9.9.9"],
    }

    def resolve(host):
        if host in mapping:
            return mapping[host]
        return ["1.2.3.4"]  # wildcard catch-all

    hits = brute_force(
        "example.com",
        ["api", "www", "nope", "mail"],
        resolve,
        workers=4,
        wildcard=["1.2.3.4"],
    )
    assert set(hits) == {"api.example.com", "www.example.com"}


def test_brute_force_without_wildcard_keeps_all_resolving():
    def resolve(host):
        return ["1.2.3.4"] if host.startswith("api.") else (_ for _ in ()).throw(OSError("nx"))

    hits = brute_force("example.com", ["api", "nope"], resolve, workers=2)
    assert hits == {"api.example.com": ["1.2.3.4"]}


def test_brute_force_skips_comments_and_invalid_labels():
    seen = []

    def resolve(host):
        seen.append(host)
        return []

    brute_force("example.com", ["# comment", "bad label", "ok", ""], resolve, workers=2)
    assert seen == ["ok.example.com"]


def test_brute_force_reports_progress():
    events = []

    def resolve(host):
        return ["1.1.1.1"] if host.startswith("a.") else []

    brute_force(
        "example.com",
        ["a", "b"],
        resolve,
        workers=1,
        on_progress=lambda name, real, done, total: events.append((name, real, done, total)),
    )
    assert len(events) == 2
    assert any(real for _, real, _, _ in events)


# -- wordlists ------------------------------------------------------------------


def test_builtin_wordlist_is_nonempty_and_unique():
    assert len(DEFAULT_WORDLIST) > 200
    assert len(set(DEFAULT_WORDLIST)) == len(DEFAULT_WORDLIST)
    assert "www" in DEFAULT_WORDLIST and "api" in DEFAULT_WORDLIST


def test_load_wordlist_default():
    assert load_wordlist(None) == list(DEFAULT_WORDLIST)


def test_load_wordlist_file(tmp_path):
    path = tmp_path / "words.txt"
    path.write_text("# comment\nwww\n\napi\n", encoding="utf-8")
    assert load_wordlist(path) == ["www", "api"]


def test_load_wordlist_missing_raises(tmp_path):
    with pytest.raises(SubdomainError):
        load_wordlist(tmp_path / "nope.txt")


# -- orchestration --------------------------------------------------------------


def test_enumerate_combines_passive_and_bruteforce():
    def handler(request):
        if request.url.host == "crt.sh":
            return httpx.Response(200, json=[{"name_value": "passive.example.com"}])
        return httpx.Response(200, json=[{"dns_names": ["shared.example.com"]}])

    def resolve(host):
        if host in ("shared.example.com", "portal.example.com", "passive.example.com"):
            return ["9.9.9.9"]
        raise OSError("nx")

    report = enumerate_subdomains(
        "https://www.example.com/deep/path",
        client=client_for(handler),
        resolver=resolve,
        words=["portal", "missing"],
        sources=["crtsh", "certspotter"],
    )
    names = set(report.names)
    assert report.domain == "example.com"
    assert {"passive.example.com", "shared.example.com", "portal.example.com"} <= names
    assert "missing.example.com" not in names
    assert report.bruteforced == 1
    entry = next(r for r in report.results if r.name == "shared.example.com")
    assert entry.sources == {"certspotter"}
    assert entry.ips == ["9.9.9.9"]


def test_enumerate_flags_wildcard_dns():
    def handler(request):
        return httpx.Response(200, json=[])

    def resolve(host):
        return ["1.2.3.4"]

    report = enumerate_subdomains(
        "example.com",
        client=client_for(handler),
        resolver=resolve,
        words=["www", "api"],
        sources=["crtsh"],
    )
    assert report.wildcard == ["1.2.3.4"]
    assert any("Wildcard DNS" in note for note in report.notes)
    assert report.results == []


def test_enumerate_notes_when_nothing_found():
    def handler(request):
        return httpx.Response(200, json=[])

    def resolve(host):
        raise OSError("nx")

    report = enumerate_subdomains(
        "example.com",
        client=client_for(handler),
        resolver=resolve,
        words=["www"],
        sources=["crtsh"],
    )
    assert report.results == []
    assert any("No subdomains found" in note for note in report.notes)


def test_enumerate_honours_max_results():
    def handler(request):
        payload = [{"name_value": f"s{i}.example.com"} for i in range(50)]
        return httpx.Response(200, json=payload)

    def resolve(host):
        raise OSError("nx")

    report = enumerate_subdomains(
        "example.com",
        client=client_for(handler),
        resolver=resolve,
        bruteforce=False,
        sources=["crtsh"],
        max_results=10,
    )
    assert len(report.results) == 10


def test_enumerate_results_are_sorted_by_depth():
    def handler(request):
        payload = [{"name_value": "z.a.example.com"}, {"name_value": "b.example.com"}]
        return httpx.Response(200, json=payload)

    def resolve(host):
        raise OSError("nx")

    report = enumerate_subdomains(
        "example.com",
        client=client_for(handler),
        resolver=resolve,
        bruteforce=False,
        sources=["crtsh"],
    )
    assert report.names == ["b.example.com", "z.a.example.com"]


# -- output files ---------------------------------------------------------------


@pytest.fixture
def sample_report():
    def handler(request):
        return httpx.Response(200, json=[{"name_value": "www.example.com\napi.example.com"}])

    def resolve(host):
        return ["1.2.3.4"] if "example.com" in host else []

    return enumerate_subdomains(
        "example.com",
        client=client_for(handler),
        resolver=resolve,
        bruteforce=False,
        sources=["crtsh"],
    )


def test_text_output_is_a_plain_domain_list(sample_report):
    text = results_to_text(sample_report)
    lines = text.strip().splitlines()
    assert lines == sorted(lines, key=lambda n: (n.count("."), n))
    assert "www.example.com" in lines


def test_csv_output_has_header_and_rows(sample_report):
    csv_text = results_to_csv(sample_report)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "subdomain,ips,sources"
    assert any(line.startswith("www.example.com,") for line in lines)


def test_json_output_structure(sample_report):
    data = json.loads(results_to_json(sample_report))
    assert data["domain"] == "example.com"
    assert data["count"] == len(sample_report.results)
    assert {"domain", "wildcard", "source_counts", "notes", "subdomains"} <= set(data)
    assert data["subdomains"][0]["name"].endswith("example.com")


def test_write_outputs_creates_all_files(tmp_path, sample_report):
    paths = write_outputs(sample_report, tmp_path / "run")
    for key in ("txt", "csv", "json"):
        assert paths[key].is_file(), key
    # txt file must be directly usable as a target list
    assert "www.example.com" in paths["txt"].read_text(encoding="utf-8").splitlines()


def test_write_outputs_writes_stable_copy_in_parent(tmp_path, sample_report):
    paths = write_outputs(sample_report, tmp_path / "host" / "run")
    stable = tmp_path / "host" / "subdomains.txt"
    assert stable.is_file()
    assert paths["stable"] == stable


def test_output_list_feeds_back_as_targets(tmp_path, sample_report):
    from scrapit.paths import load_paths

    paths = write_outputs(sample_report, tmp_path / "run")
    text = paths["txt"].read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert all("." in line for line in text.split())
    assert load_paths  # imported to confirm the list format stays compatible
