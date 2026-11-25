"""Check GitHub for newer releases and update a git checkout in place."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

GITHUB_USER = "S4rtx"
DEFAULT_REPO = f"{GITHUB_USER}/scrapit"
GITHUB_API = "https://api.github.com"
FALLBACK_INSTALL = f"pip install --upgrade git+https://github.com/{DEFAULT_REPO}.git"


def version_key(value: str) -> tuple[int, int, int]:
    """Turn 'v2.1' / '2.1.0-beta' into a comparable (2, 1, 0) tuple."""
    numbers = [int(part) for part in re.findall(r"\d+", str(value))[:3]]
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


@dataclass
class UpdateInfo:
    current: str
    latest: str | None = None
    url: str = ""
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.latest is not None and version_key(self.latest) > version_key(self.current)


@dataclass
class UpdateResult:
    updated: bool
    detail: str = ""
    error: str | None = None


def _http_error(exc: Exception) -> str:
    return f"Could not reach GitHub: {exc}"


def _status_error(status: int) -> str:
    if status in (403, 429):
        return (
            f"GitHub returned HTTP {status} (rate limited). "
            "Try again later or set a GITHUB_TOKEN."
        )
    return f"GitHub returned HTTP {status}."


def _parse_json(response: httpx.Response) -> object | None:
    try:
        return response.json()
    except ValueError:
        return None


def _latest_tag(client: httpx.Client, repo: str) -> tuple[str | None, str, str]:
    """Return (tag, url, error) preferring the latest release, falling back to tags."""
    try:
        response = client.get(f"{GITHUB_API}/repos/{repo}/releases/latest")
    except httpx.HTTPError as exc:
        return None, "", _http_error(exc)

    if response.status_code == 200:
        data = _parse_json(response)
        if isinstance(data, dict) and isinstance(data.get("tag_name"), str) and data["tag_name"].strip():
            tag = data["tag_name"].strip()
            url = data.get("html_url") if isinstance(data.get("html_url"), str) else ""
            return tag, url or f"https://github.com/{repo}/releases/tag/{tag}", ""
        return None, "", "Latest release has no tag name."

    if response.status_code != 404:
        return None, "", _status_error(response.status_code)

    try:
        response = client.get(f"{GITHUB_API}/repos/{repo}/tags")
    except httpx.HTTPError as exc:
        return None, "", _http_error(exc)

    if response.status_code != 200:
        return None, "", _status_error(response.status_code)

    data = _parse_json(response)
    tags = []
    if isinstance(data, list):
        tags = [
            item["name"].strip()
            for item in data
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()
        ]
    if not tags:
        return None, "", "No releases or tags found on GitHub."

    best = max(tags, key=version_key)
    return best, f"https://github.com/{repo}/releases/tag/{best}", ""


def check_for_updates(
    current: str,
    *,
    repo: str = DEFAULT_REPO,
    client: httpx.Client | None = None,
    timeout: float = 6.0,
) -> UpdateInfo:
    """Query GitHub for the newest release/tag and compare it with ``current``."""
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": f"scrapit/{current}",
                "Accept": "application/vnd.github+json",
            },
        )
    try:
        tag, url, error = _latest_tag(client, repo)
    finally:
        if owns_client:
            client.close()

    if error:
        return UpdateInfo(current=current, error=error)
    return UpdateInfo(current=current, latest=tag, url=url)


def project_root() -> Path | None:
    """Return the git checkout this package lives in, or None for a plain install."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    return None


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def perform_update(root: Path | None = None) -> UpdateResult:
    """Fast-forward the local checkout. Never touches a non-git install."""
    checkout = root or project_root()
    if checkout is None:
        return UpdateResult(
            updated=False,
            error=f"This install is not a git checkout. Update with: {FALLBACK_INSTALL}",
        )

    before = _git(checkout, "rev-parse", "HEAD")
    pull = _git(checkout, "pull", "--ff-only")
    if pull is None:
        return UpdateResult(updated=False, error="Could not run git. Is it installed and on PATH?")

    output = (pull.stdout + pull.stderr).strip()
    if pull.returncode != 0:
        return UpdateResult(
            updated=False,
            detail=output,
            error="git pull failed. Commit or stash local changes, then try again.",
        )

    after = _git(checkout, "rev-parse", "HEAD")
    changed = bool(before and after) and before.stdout != after.stdout
    return UpdateResult(updated=changed, detail=output or "Already up to date.")
