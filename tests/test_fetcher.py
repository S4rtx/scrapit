import io
import json

import httpx
import pytest
from rich.console import Console

from scrapit import fetcher as fetcher_module
from scrapit.config import Config
from scrapit.fetcher import Scrapit, classify_status, run_session
from scrapit.storage import RunStorage

PUBLIC = ["93.184.216.34"]


def make_fetcher(tmp_path, handler, paths, config=None, resolver=None):
    config = config or Config(delay=0, respect_robots=False)
    config.delay = 0
    storage = RunStorage(tmp_path / "output", "example.com")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    console = Console(file=io.StringIO(), width=120)
    fetcher = Scrapit(
        config,
        "https://example.com",
        paths,
        storage,
        client=client,
        resolver=resolver or (lambda host: PUBLIC),
        console=console,
    )
    return fetcher, storage


def html_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


@pytest.mark.parametrize(
    "status, expected",
    [
        (200, "FOUND"),
        (204, "FOUND"),
        (301, "REDIRECT"),
        (302, "REDIRECT"),
        (308, "REDIRECT"),
        (401, "AUTH_REQUIRED"),
        (403, "FORBIDDEN"),
        (404, "NOT_FOUND"),
        (410, "NOT_FOUND"),
        (429, "RATE_LIMITED"),
        (500, "SERVER_ERROR"),
        (503, "SERVER_ERROR"),
        (418, "CLIENT_ERROR"),
        (None, "NETWORK_ERROR"),
    ],
)
def test_classify_status(status, expected):
    assert classify_status(status) == expected


def test_found_saves_file_and_headers(tmp_path):
    def handler(request):
        if request.url.path.startswith("/pf-"):
            return httpx.Response(404)
        if request.url.path == "/admin":
            return html_response("<h1>admin</h1>")
        return httpx.Response(404)

    fetcher, storage = make_fetcher(tmp_path, handler, ["/admin"])
    fetcher.run()
    assert len(fetcher.results) == 1
    result = fetcher.results[0]
    assert result.label == "FOUND"
    assert result.status == 200
    assert result.saved_as == "files/admin/index.html"
    assert (storage.run_dir / "files" / "admin" / "index.html").read_text() == "<h1>admin</h1>"
    assert (storage.run_dir / "headers" / "admin.headers.txt").is_file()


def test_not_found_and_protected_are_not_saved(tmp_path):
    codes = {"/missing": 404, "/gone": 410, "/secret": 401, "/forbidden": 403}

    def handler(request):
        if request.url.path.startswith("/pf-"):
            return httpx.Response(404)
        return httpx.Response(codes.get(request.url.path, 404))

    fetcher, _ = make_fetcher(tmp_path, handler, list(codes))
    fetcher.run()
    labels = {r.path: r.label for r in fetcher.results}
    assert labels == {
        "/missing": "NOT_FOUND",
        "/gone": "NOT_FOUND",
        "/secret": "AUTH_REQUIRED",
        "/forbidden": "FORBIDDEN",
    }
    assert all(r.saved_as is None for r in fetcher.results)


def test_soft_404_fingerprint(tmp_path):
    def handler(request):
        path = request.url.path
        if path.startswith("/pf-") or path == "/ghost":
            return html_response("SPA SHELL")
        if path == "/real":
            return html_response("REAL PAGE")
        return httpx.Response(404)

    fetcher, _ = make_fetcher(tmp_path, handler, ["/ghost", "/real"])
    fetcher.run()
    labels = {r.path: r.label for r in fetcher.results}
    assert labels["/ghost"] == "SOFT_404"
    assert labels["/real"] == "FOUND"
    assert "enabled" in fetcher.soft404_note


def test_soft_404_probe_404_disables_detection(tmp_path):
    def handler(request):
        if request.url.path.startswith("/pf-"):
            return httpx.Response(404)
        return html_response("REAL PAGE")

    fetcher, _ = make_fetcher(tmp_path, handler, ["/real"])
    fetcher.run()
    assert fetcher.soft_fingerprint is None
    assert "disabled" in fetcher.soft404_note
    assert fetcher.results[0].label == "FOUND"


def test_redirect_chain_and_final_saved(tmp_path):
    def handler(request):
        path = request.url.path
        if path.startswith("/pf-"):
            return httpx.Response(404)
        if path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        if path == "/new":
            return html_response("new page")
        return httpx.Response(404)

    fetcher, storage = make_fetcher(tmp_path, handler, ["/old"])
    fetcher.run()
    result = fetcher.results[0]
    assert result.label == "FOUND"
    assert result.final_url == "https://example.com/new"
    assert result.redirect_chain == ["302 -> https://example.com/new"]
    assert result.saved_as == "files/old/index.html"
    assert (storage.run_dir / "files" / "old" / "index.html").read_text() == "new page"
    assert fetcher.requests_sent == 3


def test_redirect_not_followed_when_disabled(tmp_path):
    def handler(request):
        path = request.url.path
        if path.startswith("/pf-"):
            return httpx.Response(404)
        if path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return html_response("new")

    fetcher, _ = make_fetcher(
        tmp_path,
        handler,
        ["/old"],
        config=Config(delay=0, respect_robots=False, follow_redirects=False),
    )
    fetcher.run()
    result = fetcher.results[0]
    assert result.label == "REDIRECT"
    assert result.status == 301
    assert result.saved_as is None
    assert "not followed" in result.redirect_chain[0]


def test_retry_on_503_then_success(tmp_path, monkeypatch):
    calls = {"count": 0}

    def handler(request):
        path = request.url.path
        if path.startswith("/pf-"):
            return httpx.Response(404)
        if path == "/flaky":
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx.Response(503)
            return html_response("ok")
        return httpx.Response(404)

    monkeypatch.setattr(fetcher_module.time, "sleep", lambda seconds: None)
    fetcher, _ = make_fetcher(
        tmp_path,
        handler,
        ["/flaky"],
        config=Config(delay=0, respect_robots=False, max_retries=1),
    )
    fetcher.run()
    assert calls["count"] == 2
    assert fetcher.results[0].label == "FOUND"


def test_network_timeout_is_recorded(tmp_path):
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    fetcher, _ = make_fetcher(
        tmp_path,
        handler,
        ["/slow"],
        config=Config(delay=0, respect_robots=False, max_retries=0),
    )
    fetcher.run()
    result = fetcher.results[0]
    assert result.label == "NETWORK_ERROR"
    assert "Timeout" in result.error


def test_private_redirect_is_blocked_before_request(tmp_path):
    hits: list[str] = []

    def handler(request):
        hits.append(str(request.url))
        path = request.url.path
        if path.startswith("/pf-"):
            return httpx.Response(404)
        if path == "/go":
            return httpx.Response(302, headers={"location": "http://internal.test/secret"})
        return httpx.Response(200)

    def resolver(host):
        if host == "internal.test":
            return ["127.0.0.1"]
        return PUBLIC

    fetcher, _ = make_fetcher(tmp_path, handler, ["/go"], resolver=resolver)
    fetcher.run()
    result = fetcher.results[0]
    assert result.label == "SAFETY_BLOCK"
    assert "non-public" in result.error
    assert not any("internal.test" in url for url in hits)


def test_private_target_is_blocked(tmp_path):
    hits: list[str] = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(200)

    fetcher, _ = make_fetcher(tmp_path, handler, ["/x"], resolver=lambda host: ["10.0.0.5"])
    fetcher.run()
    assert fetcher.results[0].label == "SAFETY_BLOCK"
    assert hits == []


def test_robots_disallowed_paths_are_skipped(tmp_path):
    def handler(request):
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private\n",
                headers={"content-type": "text/plain"},
            )
        if path.startswith("/pf-"):
            return httpx.Response(404)
        if path == "/public":
            return html_response("public")
        if path == "/private":
            return html_response("private")
        return httpx.Response(404)

    fetcher, _ = make_fetcher(
        tmp_path,
        handler,
        ["/private", "/public"],
        config=Config(delay=0, respect_robots=True),
    )
    fetcher.run()
    labels = {r.path: r.label for r in fetcher.results}
    assert labels["/private"] == "BLOCKED_BY_ROBOTS"
    assert labels["/public"] == "FOUND"
    assert "loaded" in fetcher.robots.note


def test_too_large_body_is_not_saved(tmp_path):
    def handler(request):
        if request.url.path.startswith("/pf-"):
            return httpx.Response(404)
        return html_response("x" * 50)

    fetcher, _ = make_fetcher(
        tmp_path,
        handler,
        ["/big"],
        config=Config(delay=0, respect_robots=False, max_download_bytes=10),
    )
    fetcher.run()
    result = fetcher.results[0]
    assert result.label == "TOO_LARGE"
    assert result.saved_as is None
    assert "limit" in result.error


def test_linked_assets_are_downloaded_same_host_only(tmp_path):
    page = (
        "<html><head><link rel='stylesheet' href='/style.css'></head>"
        "<body><img src='https://example.com/logo.png'>"
        "<img src='https://other.example/x.png'>"
        "<script src='/app.js'></script></body></html>"
    )

    def handler(request):
        path = request.url.path
        if path.startswith("/pf-"):
            return httpx.Response(404)
        if path == "/page":
            return html_response(page)
        if path == "/app.js":
            return httpx.Response(200, text="console.log(1)", headers={"content-type": "application/javascript"})
        if path == "/style.css":
            return httpx.Response(200, text="body{}", headers={"content-type": "text/css"})
        if path == "/logo.png":
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
        return httpx.Response(404)

    fetcher, _ = make_fetcher(
        tmp_path,
        handler,
        ["/page"],
        config=Config(delay=0, respect_robots=False, download_assets=True),
    )
    fetcher.run()
    assets = [r for r in fetcher.results if r.asset_of == "/page"]
    assert {r.path for r in assets} == {"/app.js", "/style.css", "/logo.png"}
    assert all(r.label == "FOUND" for r in assets)
    assert not any("other.example" in r.url for r in fetcher.results)
    assert any((r.saved_as or "").endswith("app.js") for r in assets)


def test_last_redirect_loop_is_reported(tmp_path):
    def handler(request):
        path = request.url.path
        if path.startswith("/pf-"):
            return httpx.Response(404)
        if path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        return httpx.Response(404)

    fetcher, _ = make_fetcher(
        tmp_path,
        handler,
        ["/loop"],
        config=Config(delay=0, respect_robots=False, max_redirects=2),
    )
    fetcher.run()
    result = fetcher.results[0]
    assert result.label == "NETWORK_ERROR"
    assert "Too many redirects" in result.error


def test_run_session_refuses_without_permission(tmp_path):
    def handler(request):
        return httpx.Response(404)

    config = Config(delay=0.2, respect_robots=False, output_dir=tmp_path / "out")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    console = Console(file=io.StringIO(), width=120)
    summary = run_session(
        config,
        "https://example.com",
        ["/x"],
        console,
        interactive=False,
        i_have_permission=False,
        resolver=lambda host: PUBLIC,
        client=client,
    )
    assert summary is None
    assert not (tmp_path / "out").exists()


def test_run_session_writes_reports(tmp_path, monkeypatch):
    def handler(request):
        if request.url.path.startswith("/pf-"):
            return httpx.Response(404)
        return html_response("<h1>admin</h1>")

    monkeypatch.setattr(fetcher_module.time, "sleep", lambda seconds: None)
    config = Config(delay=0.2, respect_robots=False, output_dir=tmp_path / "out")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    console = Console(file=io.StringIO(), width=120)
    summary = run_session(
        config,
        "https://example.com",
        ["/admin"],
        console,
        assume_yes=True,
        i_have_permission=True,
        interactive=False,
        resolver=lambda host: PUBLIC,
        client=client,
    )
    assert summary is not None
    run_dir = summary.run_dir
    for name in ("results.json", "results.csv", "report.md", "scrapit.log"):
        assert (run_dir / name).is_file(), name
    data = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert data["counts"]["found"] == 1
    assert data["target"] == "https://example.com"
    assert (run_dir / "files" / "admin" / "index.html").read_text() == "<h1>admin</h1>"


def test_live_display_rebuild_survives_concurrent_refresh():
    import threading
    import time

    from scrapit.fetcher import FetchResult, LiveDisplay

    console = Console(force_terminal=True, width=120, file=io.StringIO())
    display = LiveDisplay(console, 60)
    display.start()
    errors: list[BaseException] = []

    def pump():
        try:
            for index in range(60):
                display.path_started(index + 1, 60, f"/p{index}")
                display.add(
                    FetchResult(
                        path=f"/p{index}",
                        url=f"https://example.com/p{index}",
                        status=200,
                        label="FOUND",
                        content_type="text/html",
                        size_bytes=index * 10,
                        response_ms=index,
                        saved_as=f"files/p{index}.html",
                    )
                )
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    worker = threading.Thread(target=pump)
    worker.start()
    worker.join()
    display.stop()
    assert errors == []


def test_run_session_saves_reports_when_display_teardown_fails(tmp_path, monkeypatch):
    def handler(request):
        if request.url.path.startswith("/pf-"):
            return httpx.Response(404)
        return html_response("<h1>ok</h1>")

    def boom(self):
        raise IndexError("list index out of range")

    monkeypatch.setattr(fetcher_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(fetcher_module.Live, "stop", boom)
    config = Config(delay=0.2, respect_robots=False, output_dir=tmp_path / "out")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    console = Console(file=io.StringIO(), width=120)
    summary = run_session(
        config,
        "https://example.com",
        ["/admin"],
        console,
        assume_yes=True,
        i_have_permission=True,
        interactive=False,
        resolver=lambda host: PUBLIC,
        client=client,
    )
    assert summary is not None
    assert (summary.run_dir / "results.json").is_file()


def test_run_session_uses_run_name(tmp_path):
    def handler(request):
        if request.url.path.startswith("/pf-"):
            return httpx.Response(404)
        return html_response("<h1>ok</h1>")

    config = Config(delay=0.2, respect_robots=False, output_dir=tmp_path / "out")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    console = Console(file=io.StringIO(), width=120)
    summary = run_session(
        config,
        "https://example.com",
        ["/admin"],
        console,
        assume_yes=True,
        i_have_permission=True,
        interactive=False,
        run_name="evening scan",
        resolver=lambda host: PUBLIC,
        client=client,
    )
    assert summary is not None
    assert summary.run_dir.name.startswith("evening scan_")
    assert (summary.run_dir / "results.json").is_file()


def test_ui_callback_failure_does_not_abort_run(tmp_path):
    def handler(request):
        if request.url.path.startswith("/pf-"):
            return httpx.Response(404)
        return html_response("<h1>ok</h1>")

    fetcher, _ = make_fetcher(tmp_path, handler, ["/admin", "/js"])

    def boom(*args, **kwargs):
        raise IndexError("list index out of range")

    fetcher.run(on_result=boom, on_path_start=boom)
    assert len(fetcher.results) == 2
    assert all(result.label in ("FOUND", "NOT_FOUND") for result in fetcher.results)
