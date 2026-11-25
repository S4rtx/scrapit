import io
import json

import httpx
from rich.console import Console

from scrapit import fetcher as fetcher_module
from scrapit.config import Config
from scrapit.discovery import (
    extract_links,
    looks_like_page,
    path_of,
    resolve_link,
    robots_sitemaps,
    sitemap_locations,
)
from scrapit.fetcher import run_session

PUBLIC = ["93.184.216.34"]

SITE = {
    "/": "<html><body><a href='/a'>a</a><a href='/b.html'>b</a><a href='/x.png'>img</a>"
    "<a href='https://other.test/nope'>ext</a><a href='mailto:x@y.z'>m</a></body></html>",
    "/a": "<html><body><a href='/c'>c</a><a href='/'>home</a></body></html>",
    "/b.html": "<html><body><a href='/d'>d</a></body></html>",
    "/c": "<html><body><a href='/e'>e</a></body></html>",
    "/d": "<html><body>leaf</body></html>",
    "/e": "<html><body>leaf</body></html>",
}


def site_handler(request):
    path = request.url.path
    if path.startswith("/pf-"):
        return httpx.Response(404)
    if path == "/robots.txt":
        return httpx.Response(404)
    if path == "/sitemap.xml":
        return httpx.Response(404)
    body = SITE.get(path)
    if body is None:
        return httpx.Response(404, text="missing", headers={"content-type": "text/html"})
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def run(tmp_path, handler, *, depth=3, sitemap=True, max_paths=50, paths=None):
    config = Config(
        delay=0.2,
        respect_robots=False,
        output_dir=tmp_path / "out",
        discover=True,
        max_depth=depth,
        use_sitemap=sitemap,
        max_paths=max_paths,
        warn_paths=min(200, max_paths),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    console = Console(file=io.StringIO(), width=120)
    summary = run_session(
        config,
        "https://example.com",
        paths or ["/"],
        console,
        assume_yes=True,
        i_have_permission=True,
        interactive=False,
        resolver=lambda host: PUBLIC,
        client=client,
        discover_paths=True,
    )
    assert summary is not None
    payload = json.loads((summary.run_dir / "results.json").read_text(encoding="utf-8"))
    return summary, payload


# -- pure helpers ---------------------------------------------------------------


def test_path_of_keeps_query():
    assert path_of("https://example.com/a/b?x=1#frag") == "/a/b?x=1"


def test_looks_like_page_filters_assets():
    assert looks_like_page("/blog/")
    assert looks_like_page("/about")
    assert looks_like_page("/docs/intro.html")
    assert not looks_like_page("/x.png")
    assert not looks_like_page("/app.css")
    assert not looks_like_page("/app.js")


def test_resolve_link_rejects_offsite_and_non_http():
    base = "https://example.com"
    page = "https://example.com/a"
    assert resolve_link("/b", page, base) == "https://example.com/b"
    assert resolve_link("https://example.com/c#x", page, base) == "https://example.com/c"
    assert resolve_link("https://other.test/d", page, base) is None
    assert resolve_link("mailto:a@b.c", page, base) is None
    assert resolve_link("javascript:alert(1)", page, base) is None
    assert resolve_link("#frag", page, base) is None


def test_extract_links_same_host_pages_only():
    html = b"<html><body><a href='/a'>a</a><a href='/x.png'>i</a><a href='https://other.test/b'>b</a></body></html>"
    links = extract_links(html, "https://example.com/", "https://example.com")
    assert links == ["https://example.com/a"]


def test_sitemap_locations_parse_and_filter():
    xml = "<urlset><url><loc>https://example.com/one</loc></url><url><loc>https://other.test/two</loc></url></urlset>"
    assert sitemap_locations(xml, "https://example.com/sitemap.xml", "https://example.com") == [
        "https://example.com/one"
    ]


def test_robots_sitemaps_parse():
    text = "User-agent: *\nSitemap: https://example.com/sitemap.xml\n"
    assert robots_sitemaps(text, "https://example.com") == ["https://example.com/sitemap.xml"]


# -- crawl behaviour ------------------------------------------------------------


def test_discovery_follows_links_to_depth(tmp_path):
    _summary, payload = run(tmp_path, site_handler, depth=2)
    paths = {r["path"] for r in payload["results"]}
    # depth 0: /, depth 1: /a /b.html, depth 2: /c /d
    assert {"/", "/a", "/b.html", "/c", "/d"} <= paths
    # /e is depth 3 from /a -> /c -> /e, beyond depth 2
    assert "/e" not in paths
    assert payload["discovery"] is True


def test_discovery_respects_depth_zero(tmp_path):
    _summary, payload = run(tmp_path, site_handler, depth=0)
    paths = {r["path"] for r in payload["results"]}
    assert paths == {"/"}


def test_discovery_skips_assets_and_external(tmp_path):
    _summary, payload = run(tmp_path, site_handler, depth=3)
    paths = {r["path"] for r in payload["results"]}
    assert "/x.png" not in paths
    assert all(not p.startswith("https://other.test") for p in paths)


def test_discovery_visits_each_path_once(tmp_path):
    _summary, payload = run(tmp_path, site_handler, depth=3)
    paths = [r["path"] for r in payload["results"]]
    assert len(paths) == len(set(paths))


def test_discovery_honours_max_paths(tmp_path):
    _summary, payload = run(tmp_path, site_handler, depth=5, max_paths=3)
    assert len(payload["results"]) <= 3


def test_discovery_seeds_from_sitemap(tmp_path):
    def handler(request):
        path = request.url.path
        if path.startswith("/pf-"):
            return httpx.Response(404)
        if path == "/robots.txt":
            return httpx.Response(404)
        if path == "/sitemap.xml":
            return httpx.Response(
                200,
                text="<urlset><url><loc>https://example.com/from-sitemap</loc></url></urlset>",
                headers={"content-type": "application/xml"},
            )
        if path == "/from-sitemap":
            return httpx.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"})
        return httpx.Response(404)

    _summary, payload = run(tmp_path, handler, depth=0, sitemap=True)
    paths = {r["path"] for r in payload["results"]}
    assert "/from-sitemap" in paths


def test_discovery_sitemap_can_be_disabled(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.startswith("/pf-"):
            return httpx.Response(404)
        return httpx.Response(404)

    run(tmp_path, handler, depth=0, sitemap=False)
    assert "/sitemap.xml" not in seen


def test_discovery_without_paths_starts_at_root(tmp_path):
    _summary, payload = run(tmp_path, site_handler, depth=0, paths=[])
    assert payload["results"][0]["path"] == "/"


def test_non_discovery_run_still_uses_fixed_paths(tmp_path):
    config = Config(delay=0.2, respect_robots=False, output_dir=tmp_path / "out")
    client = httpx.Client(transport=httpx.MockTransport(site_handler), follow_redirects=False)
    console = Console(file=io.StringIO(), width=120)
    summary = run_session(
        config,
        "https://example.com",
        ["/", "/a"],
        console,
        assume_yes=True,
        i_have_permission=True,
        interactive=False,
        resolver=lambda host: PUBLIC,
        client=client,
    )
    payload = json.loads((summary.run_dir / "results.json").read_text(encoding="utf-8"))
    assert [r["path"] for r in payload["results"]] == ["/", "/a"]
    assert payload["discovery"] is False


def test_discovery_marks_html_as_discovery_in_report(tmp_path):
    summary, _payload = run(tmp_path, site_handler, depth=1)
    report = (summary.run_dir / "report.md").read_text(encoding="utf-8")
    assert "Discovery mode" in report
    assert "| Discovery mode | yes |" in report


def test_discovery_reports_robots_blocked_paths(tmp_path):
    def handler(request):
        path = request.url.path
        if path.startswith("/pf-"):
            return httpx.Response(404)
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        if path == "/sitemap.xml":
            return httpx.Response(404)
        return httpx.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"})

    config = Config(
        delay=0.2,
        respect_robots=True,
        output_dir=tmp_path / "out",
        discover=True,
        max_depth=1,
        use_sitemap=False,
        max_paths=20,
        warn_paths=20,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    console = Console(file=io.StringIO(), width=120)
    summary = run_session(
        config,
        "https://example.com",
        ["/"],
        console,
        assume_yes=True,
        i_have_permission=True,
        interactive=False,
        resolver=lambda host: PUBLIC,
        client=client,
        discover_paths=True,
    )
    payload = json.loads((summary.run_dir / "results.json").read_text(encoding="utf-8"))
    assert payload["results"][0]["label"] == "BLOCKED_BY_ROBOTS"
    assert payload["counts"]["blocked"] == 1
