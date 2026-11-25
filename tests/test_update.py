import httpx
import pytest

from scrapit import update
from scrapit.update import UpdateInfo, check_for_updates, perform_update, project_root, version_key


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1.0.0", (1, 0, 0)),
        ("v2.1", (2, 1, 0)),
        ("2.1.0-beta", (2, 1, 0)),
        ("v10.0.3", (10, 0, 3)),
    ],
)
def test_version_key(value, expected):
    assert version_key(value) == expected


def test_update_available_only_when_newer():
    assert UpdateInfo(current="2.0.0", latest="v2.1.0").available
    assert not UpdateInfo(current="2.1.0", latest="v2.1.0").available
    assert not UpdateInfo(current="2.1.0", latest="v2.0.9").available
    assert not UpdateInfo(current="2.1.0").available


def test_check_for_updates_prefers_latest_release():
    def handler(request):
        assert request.url.path == "/repos/S4rtx/scrapit/releases/latest"
        return httpx.Response(200, json={"tag_name": "v2.1.0", "html_url": "https://example.test/v2.1.0"})

    info = check_for_updates("2.0.0", client=client_for(handler))
    assert info.error is None
    assert info.latest == "v2.1.0"
    assert info.url == "https://example.test/v2.1.0"
    assert info.available


def test_check_for_updates_falls_back_to_tags():
    def handler(request):
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(404)
        assert request.url.path == "/repos/S4rtx/scrapit/tags"
        return httpx.Response(200, json=[{"name": "v2.0.0"}, {"name": "v2.3.1"}, {"name": "v1.0.0"}])

    info = check_for_updates("2.0.0", client=client_for(handler))
    assert info.error is None
    assert info.latest == "v2.3.1"
    assert info.available


def test_check_for_updates_reports_network_error():
    def handler(request):
        raise httpx.ConnectError("no network", request=request)

    info = check_for_updates("2.0.0", client=client_for(handler))
    assert info.error is not None
    assert info.latest is None


def test_check_for_updates_reports_rate_limit():
    def handler(request):
        return httpx.Response(403)

    info = check_for_updates("2.0.0", client=client_for(handler))
    assert info.error is not None
    assert "403" in info.error


def test_check_for_updates_no_tags():
    def handler(request):
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(404)
        return httpx.Response(200, json=[])

    info = check_for_updates("2.0.0", client=client_for(handler))
    assert info.error is not None


def test_perform_update_without_git_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "project_root", lambda: None)
    result = perform_update()
    assert not result.updated
    assert "not a git checkout" in (result.error or "")


def test_perform_update_handles_failed_pull(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "project_root", lambda: tmp_path)

    def fake_git(root, *args):
        import subprocess

        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(["git"], 0, stdout="abc\n", stderr="")
        return subprocess.CompletedProcess(["git"], 1, stdout="", stderr="fatal: not possible to fast-forward")

    monkeypatch.setattr(update, "_git", fake_git)
    result = perform_update()
    assert not result.updated
    assert result.error is not None


def test_perform_update_detects_change(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "project_root", lambda: tmp_path)
    heads = iter(["aaa\n", "bbb\n"])

    def fake_git(root, *args):
        import subprocess

        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(["git"], 0, stdout=next(heads), stderr="")
        return subprocess.CompletedProcess(["git"], 0, stdout="Updating aaa..bbb\n", stderr="")

    monkeypatch.setattr(update, "_git", fake_git)
    result = perform_update()
    assert result.updated


def test_project_root_finds_checkout():
    root = project_root()
    if root is not None:
        assert (root / "scrapit").is_dir()
