"""HTTP fetching: sequential, polite, retrying, and scope-aware."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from . import report
from .config import Config, ConfigError
from .safety import (
    RobotsRules,
    SafetyError,
    check_url_allowed,
    estimate_requests,
    host_confirmation_matches,
    load_robots,
    scope_message,
)
from .storage import RunStorage, StorageError
from .utils import (
    Resolver,
    UrlError,
    build_url,
    host_of,
    human_bytes,
    normalize_url,
    now_iso,
    parse_retry_after,
    random_hex,
    resolve_host,
    same_host,
    setup_logging,
)

REDIRECT_CODES = {301, 302, 303, 307, 308}
RETRY_CODES = {429, 502, 503, 504}


class FetchError(Exception):
    """A single request failed after retries."""


def classify_status(status: int | None) -> str:
    if status is None:
        return "NETWORK_ERROR"
    if 200 <= status < 300:
        return "FOUND"
    if status in REDIRECT_CODES or 300 <= status < 400:
        return "REDIRECT"
    if status == 401:
        return "AUTH_REQUIRED"
    if status == 403:
        return "FORBIDDEN"
    if status in (404, 410):
        return "NOT_FOUND"
    if status == 429:
        return "RATE_LIMITED"
    if 500 <= status < 600:
        return "SERVER_ERROR"
    return "CLIENT_ERROR"


@dataclass
class FetchResult:
    path: str
    url: str
    final_url: str | None = None
    status: int | None = None
    label: str = "PENDING"
    content_type: str | None = None
    size_bytes: int = 0
    sha256: str | None = None
    response_ms: int = 0
    redirect_chain: list[str] = field(default_factory=list)
    server: str | None = None
    timestamp: str = field(default_factory=now_iso)
    saved_as: str | None = None
    headers_saved_as: str | None = None
    error: str | None = None
    asset_of: str | None = None
    body: bytes | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "label": self.label,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "response_ms": self.response_ms,
            "redirect_chain": list(self.redirect_chain),
            "server": self.server,
            "timestamp": self.timestamp,
            "saved_as": self.saved_as,
            "headers_saved_as": self.headers_saved_as,
            "error": self.error,
            "asset_of": self.asset_of,
        }


@dataclass
class Outcome:
    status: int | None
    content_type: str | None
    server: str | None
    body: bytes
    elapsed_ms: int
    chain: list[str]
    final_url: str
    header_items: list[tuple[str, str]]
    too_large: bool


@dataclass
class RunSummary:
    target: str
    host: str
    run_dir: Path
    payload: dict[str, Any]
    zip_path: Path | None = None


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: (value or "") for key, value in attrs}
        if tag == "script" and values.get("src"):
            self.urls.append(values["src"])
        elif tag == "link" and values.get("href") and "stylesheet" in values.get("rel", "").lower():
            self.urls.append(values["href"])
        elif tag == "img" and values.get("src"):
            self.urls.append(values["src"])


class LiveDisplay:
    """Progress bar plus a rolling results table, disabled automatically on non-TTYs."""

    def __init__(self, console: Console, total: int) -> None:
        self.console = console
        self.enabled = bool(getattr(console, "is_terminal", False))
        self.progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        self.task = self.progress.add_task("Starting...", total=max(total, 1))
        self.table = Table(header_style="bold", expand=False)
        self.table.add_column("Path", style="cyan", max_width=40, no_wrap=True)
        self.table.add_column("Status", no_wrap=True)
        self.table.add_column("Type", max_width=20)
        self.table.add_column("Size", justify="right")
        self.table.add_column("Time", justify="right")
        self.table.add_column("Saved As", max_width=35)
        self.rows: list[FetchResult] = []
        self._index = 0
        self.live = Live(Group(self.progress, self.table), console=console, refresh_per_second=6)

    def start(self) -> None:
        if self.enabled:
            self.live.start()

    def stop(self) -> None:
        if self.enabled:
            self.live.stop()

    def path_started(self, index: int, total: int, path: str) -> None:
        self._index = index
        self.progress.update(
            self.task,
            total=max(total, 1),
            completed=index - 1,
            description=f"[cyan]{escape(path[:60])}[/cyan]",
        )
        if not self.enabled:
            self.console.print(f"[dim]({index}/{total})[/dim] {escape(path)}")

    def add(self, result: FetchResult) -> None:
        self.rows.append(result)
        if len(self.rows) > 12:
            self.rows = self.rows[-12:]
        if result.asset_of is None:
            self.progress.update(self.task, completed=self._index)
        self._rebuild()
        if not self.enabled:
            saved = (result.saved_as or "").removeprefix("files/")
            self.console.print(
                f"   [cyan]{escape(result.path)}[/cyan] "
                f"[{report.label_style(result.label)}]{result.status if result.status is not None else '-'} "
                f"{result.label}[/{report.label_style(result.label)}] "
                f"{human_bytes(result.size_bytes)} {result.response_ms} ms"
                + (f" -> {escape(saved)}" if saved else "")
            )

    def _rebuild(self) -> None:
        self.table.rows.clear()
        for item in self.rows:
            saved = (item.saved_as or "").removeprefix("files/")
            self.table.add_row(
                Text(item.path[:60], style=report.label_style(item.label)),
                Text(
                    f"{item.status if item.status is not None else '-'} {item.label}",
                    style=report.label_style(item.label),
                ),
                Text((item.content_type or "-").split(";")[0][:20]),
                Text(human_bytes(item.size_bytes)),
                Text(f"{item.response_ms} ms"),
                Text(saved[:35], style="dim"),
            )


class Scrapit:
    def __init__(
        self,
        config: Config,
        target_url: str,
        paths: list[str],
        storage: RunStorage,
        *,
        logger: logging.Logger | None = None,
        client: httpx.Client | None = None,
        resolver: Resolver | None = None,
        console: Console | None = None,
    ) -> None:
        self.config = config
        self.base = normalize_url(target_url)
        self.host = host_of(self.base)
        self.paths = list(paths)
        self.storage = storage
        self.logger = logger or logging.getLogger("scrapit")
        self.audit = logging.getLogger("scrapit.audit")
        self.console = console or Console()
        self.owns_client = client is None
        self.client = client or httpx.Client(
            follow_redirects=False,
            verify=config.verify_tls,
            timeout=config.timeout,
            headers=config.request_headers,
        )
        self.results: list[FetchResult] = []
        self.requests_sent = 0
        self.robots: RobotsRules = RobotsRules.allow_all("robots.txt not checked")
        self.soft_fingerprint: tuple[int, int, str] | None = None
        self.soft404_note = "disabled"
        self.started_at = ""
        self.finished_at = ""
        self.duration = 0.0
        self.stopped = False
        self._last_request_time: float | None = None
        self._resolver_impl = resolver or resolve_host
        self._ip_cache: dict[str, list[str]] = {}

    def close(self) -> None:
        if self.owns_client:
            with contextlib.suppress(Exception):
                self.client.close()

    # -- resolution / throttling -------------------------------------------------

    def _resolver(self, host: str) -> list[str]:
        key = host.lower()
        if key not in self._ip_cache:
            self._ip_cache[key] = list(self._resolver_impl(host))
        return self._ip_cache[key]

    def _check_url(self, url: str) -> None:
        check_url_allowed(url, allow_private=self.config.allow_private, resolver=self._resolver)

    def _throttle(self) -> None:
        delay = float(self.config.delay)
        if self._last_request_time is not None and delay > 0:
            remaining = delay - (time.monotonic() - self._last_request_time)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_time = time.monotonic()

    # -- requests ----------------------------------------------------------------

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        cause = exc.__cause__ or exc.__context__
        if isinstance(exc, httpx.TimeoutException):
            return f"Timeout: {exc}"
        if isinstance(exc, httpx.ConnectError):
            if cause is not None:
                cause_name = type(cause).__name__.lower()
                if "ssl" in cause_name or "certificate" in cause_name:
                    return (
                        f"TLS error: {cause} "
                        "(if this is your own test server you may disable 'Verify TLS' in Settings)"
                    )
                if isinstance(cause, socket.gaierror):
                    return f"DNS error: {cause}"
            return f"Connection error: {exc}"
        if isinstance(exc, httpx.InvalidURL):
            return f"Invalid URL: {exc}"
        if isinstance(exc, httpx.UnsupportedProtocol):
            return f"Unsupported protocol: {exc}"
        return f"{type(exc).__name__}: {exc}"

    def _send_with_retries(self, url: str) -> httpx.Response:
        attempts = max(1, int(self.config.max_retries) + 1)
        for attempt in range(attempts):
            self._throttle()
            try:
                request = self.client.build_request(
                    "GET",
                    url,
                    headers=self.config.request_headers,
                    timeout=self.config.timeout,
                )
                response = self.client.send(request, stream=True)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                self.requests_sent += 1
                detail = self._describe_error(exc)
                self.audit.info("GET %s -> ERROR %s", url, detail)
                if attempt < attempts - 1:
                    wait = min(2**attempt, 30)
                    self.logger.info("Retrying %s after %s in %ss", url, detail, wait)
                    time.sleep(wait)
                    continue
                raise FetchError(detail) from exc

            self.requests_sent += 1
            status = response.status_code
            self.audit.info("GET %s -> %s", url, status)

            if status in RETRY_CODES and attempt < attempts - 1:
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                wait = min(retry_after, 30) if retry_after is not None else min(2**attempt, 30)
                response.close()
                self.logger.info("Retrying %s after HTTP %s in %ss", url, status, wait)
                time.sleep(wait)
                continue
            return response
        raise FetchError("Request failed after retries.")

    @staticmethod
    def _read_body(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
        data = bytearray()
        too_large = False
        try:
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    too_large = True
                    break
        except httpx.HTTPError as exc:
            raise FetchError(f"Read error: {exc}") from exc
        return bytes(data), too_large

    def _perform(self, url: str) -> Outcome:
        chain: list[str] = []
        current = url
        for hop in range(self.config.max_redirects + 1):
            self._check_url(current)
            started = time.perf_counter()
            response = self._send_with_retries(current)
            try:
                status = response.status_code
                location = response.headers.get("location")
                if 300 <= status < 400 and location:
                    try:
                        next_url = urljoin(current, location)
                        next_scheme = urlsplit(next_url).scheme
                    except ValueError as exc:
                        raise FetchError(f"Invalid redirect location: {location}") from exc
                    if next_scheme not in ("http", "https"):
                        raise FetchError(f"Redirect to unsupported scheme: {next_url}")
                    if self.config.follow_redirects:
                        chain.append(f"{status} -> {next_url}")
                        if hop >= self.config.max_redirects:
                            raise FetchError(f"Too many redirects (limit {self.config.max_redirects})")
                        self.logger.debug("Following redirect %s -> %s", current, next_url)
                        current = next_url
                        continue
                    chain.append(f"{status} (not followed) -> {next_url}")

                body, too_large = self._read_body(response, self.config.max_download_bytes)
                elapsed = int((time.perf_counter() - started) * 1000)
                return Outcome(
                    status=status,
                    content_type=response.headers.get("content-type"),
                    server=response.headers.get("server"),
                    body=body,
                    elapsed_ms=elapsed,
                    chain=chain,
                    final_url=current,
                    header_items=list(response.headers.multi_items()),
                    too_large=too_large,
                )
            finally:
                response.close()
        raise FetchError(f"Too many redirects (limit {self.config.max_redirects})")

    # -- per-path flow ------------------------------------------------------------

    def _is_soft404(self, status: int, body: bytes) -> bool:
        if self.soft_fingerprint is None or status != 200:
            return False
        digest = hashlib.sha256(body).hexdigest()
        return (status, len(body), digest) == self.soft_fingerprint

    def _prepare_robots(self) -> None:
        if not self.config.respect_robots:
            self.robots = RobotsRules.allow_all("disabled in settings")
            return

        def fetch(url: str) -> tuple[int | None, bytes, str]:
            outcome = self._perform(url)
            return outcome.status, outcome.body, ""

        self.robots = load_robots(fetch, self.base)
        self.logger.info("robots.txt: %s", self.robots.note)

    def _probe_soft404(self) -> None:
        probe_path = "/pf-" + random_hex(8)
        probe_url = build_url(self.base, probe_path)
        if self.config.respect_robots and not self.robots.allowed(probe_url, self.config.user_agent):
            self.soft404_note = "disabled - random probe path is disallowed by robots.txt"
            self.logger.info("Soft-404: %s", self.soft404_note)
            return
        try:
            outcome = self._perform(probe_url)
        except (FetchError, SafetyError) as exc:
            self.soft404_note = f"disabled - probe failed ({exc})"
            self.logger.info("Soft-404 probe failed: %s", exc)
            return
        except Exception as exc:
            self.soft404_note = f"disabled - probe error ({exc})"
            self.logger.exception("Soft-404 probe crashed")
            return
        if outcome.status == 200:
            digest = hashlib.sha256(outcome.body).hexdigest()
            self.soft_fingerprint = (200, len(outcome.body), digest)
            self.soft404_note = (
                f"enabled - {probe_path} returned 200 ({human_bytes(len(outcome.body))}); "
                "identical 200 responses are reported as SOFT_404"
            )
        else:
            self.soft404_note = f"disabled - probe {probe_path} returned {outcome.status}"
        self.logger.info("Soft-404: %s", self.soft404_note)

    def _fetch_one(
        self,
        path: str,
        url: str,
        *,
        is_asset: bool = False,
        asset_of: str | None = None,
    ) -> FetchResult:
        result = FetchResult(path=path, url=url, timestamp=now_iso(), asset_of=asset_of)

        if self.config.respect_robots and not self.robots.allowed(url, self.config.user_agent):
            result.label = "BLOCKED_BY_ROBOTS"
            result.error = "Disallowed by robots.txt"
            return result

        try:
            outcome = self._perform(url)
        except SafetyError as exc:
            result.label = "SAFETY_BLOCK"
            result.error = str(exc)
            return result
        except FetchError as exc:
            result.label = "NETWORK_ERROR"
            result.error = str(exc)
            return result
        except Exception as exc:
            self.logger.exception("Unexpected error while fetching %s", url)
            result.label = "NETWORK_ERROR"
            result.error = f"Unexpected error: {exc}"
            return result

        result.final_url = outcome.final_url
        result.status = outcome.status
        result.content_type = outcome.content_type
        result.server = outcome.server
        result.redirect_chain = outcome.chain
        result.response_ms = outcome.elapsed_ms
        result.size_bytes = len(outcome.body)
        result.sha256 = hashlib.sha256(outcome.body).hexdigest()
        result.label = classify_status(outcome.status)

        if outcome.too_large:
            result.label = "TOO_LARGE"
            result.error = (
                f"Response exceeded the {human_bytes(self.config.max_download_bytes)} limit; not saved."
            )
            return result

        if not is_asset and outcome.status == 200 and self._is_soft404(outcome.status, outcome.body):
            result.label = "SOFT_404"
            result.error = "Response matches the target's soft-404 fingerprint; not saved."
            return result

        if result.label == "FOUND":
            try:
                saved = self.storage.save(
                    path,
                    outcome.content_type,
                    outcome.body,
                    url,
                    outcome.final_url,
                    outcome.status,
                    outcome.header_items,
                )
                result.saved_as = saved.relative_path
                result.headers_saved_as = saved.headers_path
            except StorageError as exc:
                result.label = "SAVE_ERROR"
                result.error = str(exc)
                return result

            if (
                not is_asset
                and self.config.download_assets
                and "html" in (outcome.content_type or "").lower()
            ):
                result.body = outcome.body
        return result

    def _extract_assets(self, html_body: bytes, base_url: str) -> list[str]:
        parser = _AssetParser()
        try:
            parser.feed(html_body.decode("utf-8", "replace"))
            parser.close()
        except (ValueError, AssertionError) as exc:
            self.logger.warning("Could not parse HTML for assets: %s", exc)
            return []

        found: list[str] = []
        seen: set[str] = set()
        for raw in parser.urls:
            try:
                candidate = urljoin(base_url, raw.strip()).split("#", 1)[0]
                parsed = urlsplit(candidate)
            except ValueError:
                continue
            if parsed.scheme not in ("http", "https"):
                continue
            if not same_host(candidate, self.base):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            found.append(candidate)
            if len(found) >= self.config.max_assets_per_page:
                break
        return found

    def _asset_target(self, url: str) -> str:
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return path

    def run(
        self,
        on_result: Callable[[FetchResult], None] | None = None,
        on_path_start: Callable[[int, int, str], None] | None = None,
    ) -> None:
        self.started_at = now_iso()
        started = time.perf_counter()
        try:
            self._prepare_robots()
            self._probe_soft404()
            total = len(self.paths)
            for index, path in enumerate(self.paths, 1):
                if on_path_start is not None:
                    on_path_start(index, total, path)
                try:
                    url = build_url(self.base, path)
                except ValueError as exc:
                    result = FetchResult(
                        path=path,
                        url=path,
                        label="NETWORK_ERROR",
                        error=f"Could not build URL: {exc}",
                    )
                else:
                    result = self._fetch_one(path, url)
                self.results.append(result)
                self._log_result(result)
                if on_result is not None:
                    on_result(result)

                if result.label == "FOUND" and result.body is not None:
                    assets = self._extract_assets(result.body, result.final_url or result.url)
                    result.body = None
                    for asset_url in assets:
                        asset_path = self._asset_target(asset_url)
                        asset_result = self._fetch_one(
                            asset_path,
                            asset_url,
                            is_asset=True,
                            asset_of=result.path,
                        )
                        self.results.append(asset_result)
                        self._log_result(asset_result)
                        if on_result is not None:
                            on_result(asset_result)
        except KeyboardInterrupt:
            self.stopped = True
            self.logger.warning("Interrupted by user; partial results retained")
        finally:
            self.duration = time.perf_counter() - started
            self.finished_at = now_iso()

    def _log_result(self, result: FetchResult) -> None:
        self.logger.info(
            "%-18s %-4s %s %s %s",
            result.label,
            result.status if result.status is not None else "-",
            result.url,
            result.size_bytes,
            result.error or "",
        )


# -- interactive run orchestration ------------------------------------------------


def _confirm(console: Console, message: str, default: bool = False) -> bool:
    from rich.prompt import Confirm

    try:
        return Confirm.ask(message, console=console, default=default)
    except (EOFError, KeyboardInterrupt):
        return False


def run_session(
    config: Config,
    target_url: str,
    paths: list[str],
    console: Console | None = None,
    *,
    assume_yes: bool = False,
    i_have_permission: bool = False,
    interactive: bool = True,
    create_zip: bool = False,
    resolver: Resolver | None = None,
    client: httpx.Client | None = None,
) -> RunSummary | None:
    """Confirm scope, fetch, save reports and return a summary (or None if cancelled)."""
    console = console or Console()

    try:
        base = normalize_url(target_url)
    except UrlError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Invalid target", border_style="red"))
        return None

    if not paths:
        console.print(Panel("[red]No paths loaded.[/red]", title="Nothing to do", border_style="red"))
        return None

    try:
        config.validate()
    except ConfigError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Invalid settings", border_style="red"))
        return None

    if len(paths) > config.max_paths:
        console.print(
            Panel(
                f"[red]Refusing to send {len(paths)} requests (cap is {config.max_paths}).[/red]\n"
                "Reduce the paths file or raise the cap in Settings.",
                title="Request cap",
                border_style="red",
            )
        )
        return None

    host = host_of(base)
    try:
        addresses = check_url_allowed(base, allow_private=True, resolver=resolver)
    except SafetyError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Target unavailable", border_style="red"))
        return None
    ip = addresses[0]

    try:
        check_url_allowed(base, allow_private=config.allow_private, resolver=resolver)
    except SafetyError as exc:
        console.print(
            Panel(f"[yellow]{escape(str(exc))}[/yellow]", title="Blocked by safety policy", border_style="yellow")
        )
        return None

    request_count = estimate_requests(len(paths), config.respect_robots)

    if not i_have_permission:
        if not interactive:
            console.print(
                Panel(
                    "[red]Non-interactive runs require --i-have-permission.[/red]\n"
                    "Only use that flag on a site you own or are explicitly authorized to test.",
                    title="Permission required",
                    border_style="red",
                )
            )
            return None
        console.print(
            Panel(
                scope_message(base, request_count, ip),
                title="Scope confirmation",
                border_style="yellow",
            )
        )
        try:
            typed = console.input(f"Type [bold]{host}[/bold] to confirm (anything else cancels): ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Cancelled.[/yellow]")
            return None
        if not host_confirmation_matches(typed, host):
            console.print(
                "[red]Hostname did not match - run cancelled.[/red]\n"
                f"[dim]Expected the hostname [bold]{escape(host)}[/bold] "
                "(without https://, a port or a path).[/dim]"
            )
            return None
    else:
        console.print(
            f"[dim]Permission confirmed for {host} ({ip}) via --i-have-permission; "
            f"{request_count} request(s) planned.[/dim]"
        )

    if len(paths) > config.warn_paths and not assume_yes:
        proceed = _confirm(
            console,
            f"{len(paths)} paths is above the {config.warn_paths}-path warning threshold. Continue?",
            default=False,
        )
        if not proceed:
            console.print("[yellow]Cancelled.[/yellow]")
            return None

    try:
        storage = RunStorage(config.output_dir, host, save_headers=config.save_headers)
    except StorageError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Output folder error", border_style="red"))
        return None

    logger = setup_logging(storage.run_dir / "scrapit.log", storage.host_dir / "scrapit.log")
    logger.info(
        "Run start: target=%s paths=%s delay=%s robots=%s allow_private=%s",
        base,
        len(paths),
        config.delay,
        config.respect_robots,
        config.allow_private,
    )

    fetcher = Scrapit(
        config,
        base,
        paths,
        storage,
        logger=logger,
        client=client,
        resolver=resolver,
        console=console,
    )
    display = LiveDisplay(console, len(paths))
    display.start()
    try:
        fetcher.run(on_result=display.add, on_path_start=display.path_started)
    finally:
        display.stop()
        fetcher.close()

    counts = report.compute_counts(fetcher.results)
    payload = report.build_payload(
        target=base,
        host=host,
        run_dir=str(storage.run_dir),
        started_at=fetcher.started_at,
        finished_at=fetcher.finished_at,
        duration_s=fetcher.duration,
        requests_sent=fetcher.requests_sent,
        stopped_early=fetcher.stopped,
        settings=config.to_dict(),
        counts=counts,
        robots_note=fetcher.robots.note,
        soft404_note=fetcher.soft404_note,
        paths_loaded=len(paths),
        redirect_chains=sum(1 for item in fetcher.results if item.redirect_chain),
        results=[item.to_dict() for item in fetcher.results],
    )

    try:
        report.write_all(storage.run_dir, payload)
    except report.ReportError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")

    zip_path: Path | None = None
    if create_zip or (interactive and not assume_yes and _confirm(console, "Create a ZIP of this run?", False)):
        try:
            zip_path = storage.zip_run()
        except StorageError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")

    report.render_summary(console, payload, zip_path)
    logger.info("Run finished in %.2fs", fetcher.duration)

    return RunSummary(target=base, host=host, run_dir=storage.run_dir, payload=payload, zip_path=zip_path)
