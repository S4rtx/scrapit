"""Interactive terminal menu with a plain numbered fallback."""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __author__, __version__, report
from .config import Config, ConfigError
from .fetcher import RunSummary, run_session
from .paths import PathFileError, PathLoadResult, load_paths
from .safety import SafetyError, check_url_allowed, estimate_requests
from .storage import StorageError, find_latest_run, zip_directory
from .update import DEFAULT_REPO, GITHUB_USER, UpdateInfo, check_for_updates, perform_update, project_root
from .utils import UrlError, build_url, host_of, ip_is_blocked, normalize_url

MB = 1024 * 1024
logger = logging.getLogger("scrapit")

_questionary_module: Any = None
_questionary_checked = False


def _questionary() -> Any:
    global _questionary_module, _questionary_checked
    if not _questionary_checked:
        _questionary_checked = True
        try:
            if sys.stdin.isatty() and sys.stdout.isatty():
                import questionary

                _questionary_module = questionary
        except Exception:  # noqa: BLE001 - broken terminals/imports fall back to numbered menu
            _questionary_module = None
    return _questionary_module


def select(console: Console, message: str, choices: list[tuple[str, str]], default: str | None = None) -> str | None:
    if not choices:
        return None
    questionary = _questionary()
    if questionary is not None:
        try:
            answer = questionary.select(
                message,
                choices=[questionary.Choice(title=label, value=value) for label, value in choices],
                default=default or choices[0][1],
            ).ask()
            return answer
        except (KeyboardInterrupt, EOFError):
            return None
        except Exception:
            logger.debug("questionary select failed; using fallback", exc_info=True)

    console.print(f"[bold]{message}[/bold]")
    for index, (label, _) in enumerate(choices, 1):
        numbered = bool(re.match(r"^\d+\.\s", label))
        prefix = "" if numbered else f"[cyan]{index}[/cyan]. "
        console.print(f"  {prefix}{escape(label)}")
    default_index = 1
    if default is not None:
        for index, (_, value) in enumerate(choices, 1):
            if value == default:
                default_index = index
                break
    while True:
        try:
            raw = console.input(f"Select [1-{len(choices)}] (default {default_index}): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return choices[default_index - 1][1]
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1][1]
        console.print("[red]Invalid selection, try again.[/red]")


def text(console: Console, message: str, default: str = "") -> str | None:
    questionary = _questionary()
    if questionary is not None:
        try:
            return questionary.text(message, default=default).ask()
        except (KeyboardInterrupt, EOFError):
            return None
        except Exception:
            logger.debug("questionary text failed; using fallback", exc_info=True)

    try:
        raw = console.input(f"{escape(message)} [{escape(default)}]: ")
    except (EOFError, KeyboardInterrupt):
        return None
    return raw.strip() or default


def confirm(console: Console, message: str, default: bool = True) -> bool | None:
    questionary = _questionary()
    if questionary is not None:
        try:
            return questionary.confirm(message, default=default).ask()
        except (KeyboardInterrupt, EOFError):
            return None
        except Exception:
            logger.debug("questionary confirm failed; using fallback", exc_info=True)

    suffix = "Y/n" if default else "y/N"
    try:
        raw = console.input(f"{escape(message)} [{suffix}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return default
    return raw in ("y", "yes")


class MenuApp:
    def __init__(
        self,
        console: Console | None = None,
        config: Config | None = None,
        target: str | None = None,
        paths_file: str | None = None,
        paths: list[str] | None = None,
        assume_yes: bool = False,
        run_name: str | None = None,
    ) -> None:
        self.console = console or Console()
        self.config = config or Config()
        self.target: str | None = None
        self.target_ip: str | None = None
        self.paths_file: str | None = paths_file
        self.paths: list[str] = paths or []
        self.paths_info: PathLoadResult | None = None
        self.last_run: RunSummary | None = None
        self.last_subdomains: Any | None = None
        self.assume_yes = assume_yes
        self.run_name = run_name.strip() if run_name and run_name.strip() else None
        if target:
            self._assign_target(target)

    # -- header / loop ------------------------------------------------------------

    def _assign_target(self, raw: str) -> bool:
        try:
            normalized = normalize_url(raw)
        except UrlError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return False
        try:
            ips = check_url_allowed(normalized, allow_private=True)
            self.target_ip = ips[0]
        except SafetyError as exc:
            self.target_ip = None
            self.console.print(f"[yellow]Warning:[/yellow] {escape(str(exc))}")
        self.target = normalized
        if (
            self.target_ip
            and not self.config.allow_private
            and ip_is_blocked(self.target_ip)
        ):
            self.console.print(
                Panel(
                    f"[yellow]'{host_of(normalized)}' resolves to a private address "
                    f"({self.target_ip}).[/yellow]\n"
                    "Runs will be blocked unless you enable 'Allow localhost/private targets' "
                    "in Settings. Only do that for a server you control.",
                    title="Private address",
                    border_style="yellow",
                )
            )
        return True

    def run(self) -> None:
        self._notify_updates_on_start()
        while True:
            self._render_header()
            choice = select(
                self.console,
                "What would you like to do?",
                [
                    ("1. Set target URL", "target"),
                    ("2. Load paths file (.txt)", "paths"),
                    ("3. Auto-scrape this site (no paths file)", "auto"),
                    ("4. Find subdomains (passive + brute force)", "subdomains"),
                    ("5. Settings", "settings"),
                    ("6. Preview URLs that will be requested", "preview"),
                    ("7. Start fetching", "start"),
                    ("8. View last results", "results"),
                    ("9. Export report / ZIP the downloads", "export"),
                    ("10. Help / About", "help"),
                    ("11. Check for updates", "update"),
                    ("12. Exit", "exit"),
                ],
                default="target",
            )
            if choice is None:
                self.console.print("\n[dim]Goodbye.[/dim]")
                return
            try:
                if choice == "exit":
                    answer = confirm(self.console, "Exit scrapit?", True)
                    if answer is None:
                        return
                    if answer:
                        self.console.print("[green]Goodbye.[/green]")
                        return
                elif choice == "target":
                    self._set_target()
                elif choice == "paths":
                    self._load_paths()
                elif choice == "auto":
                    self._auto_scrape()
                elif choice == "subdomains":
                    self._find_subdomains()
                elif choice == "settings":
                    self._settings()
                elif choice == "preview":
                    self._preview()
                elif choice == "start":
                    self._start()
                elif choice == "results":
                    self._view_results()
                elif choice == "export":
                    self._export()
                elif choice == "help":
                    self._help()
                elif choice == "update":
                    self._check_updates()
            except KeyboardInterrupt:
                self.console.print("\n[yellow]Interrupted - back to the menu.[/yellow]")
            except Exception as exc:
                logger.exception("Menu action failed")
                self.console.print(f"[red]Unexpected error:[/red] {escape(str(exc))}")
                self.console.print("[dim]Details were written to the log file if one exists.[/dim]")

    def _render_header(self) -> None:
        if self.target:
            target_text = escape(self.target)
            if self.target_ip:
                target_text += f" [dim]({self.target_ip})[/dim]"
        else:
            target_text = "[dim]not set[/dim]"

        if self.paths:
            source = Path(self.paths_file).name if self.paths_file else "memory"
            paths_text = f"{escape(source)} ({len(self.paths)} loaded)"
        else:
            paths_text = "[dim]not set[/dim]"

        body = (
            f"[bold]Target :[/bold] {target_text}\n"
            f"[bold]Paths  :[/bold] {paths_text}\n"
            f"[bold]Output :[/bold] {escape(str(self.config.output_dir))}\n"
            f"[bold]Mode   :[/bold] "
            + (
                f"[green]auto-scrape[/green] [dim](depth {self.config.max_depth})[/dim]"
                if self.config.discover
                else "[dim]paths file[/dim]"
            )
        )
        self.console.print(
            Panel(
                Text.from_markup(body),
                title="SCRAPIT",
                subtitle=f"by @{__author__} - polite path fetcher",
                border_style="cyan",
            )
        )

    # -- actions ------------------------------------------------------------------

    def _set_target(self) -> None:
        raw = text(self.console, "Target base URL", default=self.target or "")
        if raw is None:
            return
        raw = raw.strip()
        if not raw:
            self.console.print("[yellow]No change.[/yellow]")
            return
        if self._assign_target(raw):
            self.console.print(f"[green]Target set:[/green] {escape(self.target or '')}")

    def _load_paths(self) -> None:
        default = self.paths_file or "paths.txt"
        raw = text(self.console, "Paths file", default=default)
        if raw is None:
            return
        raw = raw.strip()
        if not raw:
            self.console.print("[yellow]No change.[/yellow]")
            return
        try:
            result = load_paths(raw, self.target)
        except PathFileError as exc:
            self.console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Paths error", border_style="red"))
            return

        self.paths = result.paths
        self.paths_file = str(Path(raw).expanduser())
        self.paths_info = result
        self.console.print(f"[green]{escape(result.summary)}[/green]")
        for warning in result.warnings[:8]:
            self.console.print(f"[yellow]Warning:[/yellow] {escape(warning)}")
        if len(result.warnings) > 8:
            self.console.print(f"[dim]... and {len(result.warnings) - 8} more warning(s).[/dim]")

        table = Table(title="First paths", header_style="bold")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Path", style="cyan")
        for index, path in enumerate(result.paths[:10], 1):
            table.add_row(str(index), path)
        self.console.print(table)
        if len(result.paths) > 10:
            self.console.print(f"[dim]... and {len(result.paths) - 10} more path(s).[/dim]")

    def _settings(self) -> None:
        while True:
            cfg = self.config
            choice = select(
                self.console,
                "Settings",
                [
                    (f"1. Delay between requests: {cfg.delay:g} s", "delay"),
                    (f"2. Timeout: {cfg.timeout:g} s", "timeout"),
                    (f"3. Max retries: {cfg.max_retries}", "retries"),
                    (f"4. Follow redirects: {'on' if cfg.follow_redirects else 'off'}", "redirects"),
                    (f"5. Max download size: {cfg.max_download_mb:g} MB", "size"),
                    (f"6. Respect robots.txt: {'on' if cfg.respect_robots else 'off'}", "robots"),
                    (f"7. User-Agent: {cfg.user_agent}", "ua"),
                    (f"8. Extra headers: {len(cfg.extra_headers)} set", "headers"),
                    (f"9. Verify TLS certificates: {'on' if cfg.verify_tls else 'off'}", "tls"),
                    (f"10. Output folder: {cfg.output_dir}", "output"),
                    (f"11. Save response headers: {'on' if cfg.save_headers else 'off'}", "save_headers"),
                    (f"12. Download linked assets: {'on' if cfg.download_assets else 'off'}", "assets"),
                    (f"13. Allow localhost/private targets: {'on' if cfg.allow_private else 'off'}", "private"),
                    (f"14. Max paths per run: {cfg.max_paths}", "max_paths"),
                    (f"15. Discovery mode: {'on' if cfg.discover else 'off'}", "discover"),
                    (f"16. Crawl depth: {cfg.max_depth}", "depth"),
                    (f"17. Use sitemaps when discovering: {'on' if cfg.use_sitemap else 'off'}", "sitemap"),
                    ("18. Back", "back"),
                ],
                default="back",
            )
            if choice is None or choice == "back":
                return
            try:
                self._edit_setting(choice)
            except KeyboardInterrupt:
                self.console.print("\n[yellow]Cancelled.[/yellow]")

    def _edit_setting(self, choice: str) -> None:
        cfg = replace(self.config)

        if choice == "delay":
            raw = text(self.console, "Delay in seconds (min 0.2)", default=f"{cfg.delay:g}")
            if raw is None:
                return
            try:
                cfg.delay = float(raw)
            except ValueError:
                self.console.print("[red]Enter a number.[/red]")
                return
        elif choice == "timeout":
            raw = text(self.console, "Timeout in seconds", default=f"{cfg.timeout:g}")
            if raw is None:
                return
            try:
                cfg.timeout = float(raw)
            except ValueError:
                self.console.print("[red]Enter a number.[/red]")
                return
        elif choice == "retries":
            raw = text(self.console, "Max retries (0-5)", default=str(cfg.max_retries))
            if raw is None:
                return
            try:
                cfg.max_retries = int(raw)
            except ValueError:
                self.console.print("[red]Enter a whole number.[/red]")
                return
        elif choice == "redirects":
            answer = confirm(self.console, "Follow redirects?", cfg.follow_redirects)
            if answer is None:
                return
            cfg.follow_redirects = answer
        elif choice == "size":
            raw = text(self.console, "Max download size in MB", default=f"{cfg.max_download_mb:g}")
            if raw is None:
                return
            try:
                cfg.max_download_bytes = int(float(raw) * MB)
            except ValueError:
                self.console.print("[red]Enter a number.[/red]")
                return
        elif choice == "robots":
            answer = confirm(self.console, "Respect robots.txt?", cfg.respect_robots)
            if answer is None:
                return
            cfg.respect_robots = answer
        elif choice == "ua":
            raw = text(self.console, "User-Agent", default=cfg.user_agent)
            if raw is None:
                return
            cfg.user_agent = raw
        elif choice == "headers":
            current = "; ".join(f"{key}: {value}" for key, value in cfg.extra_headers.items())
            raw = text(self.console, "Extra headers ('Name: Value; Name2: Value2', empty clears)", default=current)
            if raw is None:
                return
            headers: dict[str, str] = {}
            for part in [item.strip() for item in raw.split(";") if item.strip()]:
                if ":" not in part:
                    self.console.print(f"[red]Invalid header '{escape(part)}' - use Name: Value.[/red]")
                    return
                key, value = part.split(":", 1)
                headers[key.strip()] = value.strip()
            cfg.extra_headers = headers
        elif choice == "tls":
            answer = confirm(self.console, "Verify TLS certificates? (off is unsafe)", cfg.verify_tls)
            if answer is None:
                return
            cfg.verify_tls = answer
            if not answer:
                self.console.print(
                    Panel(
                        "[yellow]TLS verification is now OFF. Only use this against a test server "
                        "with a self-signed certificate you control.[/yellow]",
                        title="Warning",
                        border_style="yellow",
                    )
                )
        elif choice == "output":
            raw = text(self.console, "Output folder", default=str(cfg.output_dir))
            if raw is None:
                return
            cfg.output_dir = Path(raw).expanduser()
        elif choice == "save_headers":
            answer = confirm(self.console, "Save response headers alongside each file?", cfg.save_headers)
            if answer is None:
                return
            cfg.save_headers = answer
        elif choice == "assets":
            answer = confirm(self.console, "Download linked JS/CSS/images found in HTML?", cfg.download_assets)
            if answer is None:
                return
            cfg.download_assets = answer
        elif choice == "private":
            answer = confirm(
                self.console,
                "Allow localhost/private IP targets? Only for servers you own.",
                cfg.allow_private,
            )
            if answer is None:
                return
            cfg.allow_private = answer
            if answer:
                self.console.print(
                    Panel(
                        "[yellow]Private targets are allowed. Keep this off unless you are testing "
                        "your own local server.[/yellow]",
                        title="Warning",
                        border_style="yellow",
                    )
                )
        elif choice == "max_paths":
            raw = text(self.console, "Max paths per run (1-5000)", default=str(cfg.max_paths))
            if raw is None:
                return
            try:
                cfg.max_paths = int(raw)
            except ValueError:
                self.console.print("[red]Enter a whole number.[/red]")
                return
            cfg.warn_paths = min(cfg.warn_paths, cfg.max_paths)
        elif choice == "discover":
            answer = confirm(
                self.console,
                "Enable discovery mode (crawl links instead of using a paths file)?",
                cfg.discover,
            )
            if answer is None:
                return
            cfg.discover = answer
        elif choice == "depth":
            raw = text(self.console, "Crawl depth (0-10)", default=str(cfg.max_depth))
            if raw is None:
                return
            try:
                cfg.max_depth = int(raw)
            except ValueError:
                self.console.print("[red]Enter a whole number.[/red]")
                return
        elif choice == "sitemap":
            answer = confirm(self.console, "Seed the crawl from sitemaps?", cfg.use_sitemap)
            if answer is None:
                return
            cfg.use_sitemap = answer
        else:
            return

        try:
            cfg.validate()
        except ConfigError as exc:
            self.console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Invalid value", border_style="red"))
            return
        self.config = cfg
        self.console.print("[green]Setting saved.[/green]")

    def _preview(self) -> None:
        if not self._require_ready():
            return
        assert self.target is not None
        if self.config.discover:
            seeds = self.paths or ["/"]
            lines = "\n".join(f"  {escape(url)}" for url in seeds[:10])
            self.console.print(
                Panel(
                    Text.from_markup(
                        f"[bold]Discovery mode is on[/bold] - starting points:\n{lines}\n"
                        f"[dim]These are the start only; live links are followed up to depth "
                        f"{self.config.max_depth}. Preview cannot list them in advance.[/dim]"
                    ),
                    title="Preview (auto-scrape)",
                    border_style="cyan",
                )
            )
            return
        urls = [build_url(self.target, path) for path in self.paths]
        table = Table(title=f"Preview ({len(urls)} URL(s))", header_style="bold")
        table.add_column("#", justify="right", style="dim")
        table.add_column("URL", style="cyan")
        for index, url in enumerate(urls[:100], 1):
            table.add_row(str(index), url)
        self.console.print(table)
        if len(urls) > 100:
            self.console.print(f"[dim]... and {len(urls) - 100} more URL(s).[/dim]")
        planned = estimate_requests(len(urls), self.config.respect_robots)
        self.console.print(
            f"[dim]About {planned} request(s) will be sent sequentially. "
            f"robots.txt is fetched and checked at run time.[/dim]"
        )
        if len(urls) > self.config.max_paths:
            self.console.print(
                f"[yellow]Warning:[/yellow] {len(urls)} paths exceed the cap of {self.config.max_paths}; "
                "the run will be refused."
            )

    def _start(self) -> None:
        if not self._require_ready():
            return
        try:
            self.config.validate()
        except ConfigError as exc:
            self.console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Invalid settings", border_style="red"))
            return
        assert self.target is not None
        run_name: str | None = self.run_name
        if run_name is None and not self.assume_yes:
            raw = text(
                self.console,
                "Name for this run (optional; appears in the output folder name)",
                default="",
            )
            if raw is None:
                self.console.print("[yellow]Cancelled.[/yellow]")
                return
            run_name = raw.strip() or None
        summary = run_session(
            self.config,
            self.target,
            self.paths or ["/"],
            self.console,
            assume_yes=self.assume_yes,
            run_name=run_name,
            discover_paths=self.config.discover,
        )
        if summary is not None:
            self.last_run = summary

    def _auto_scrape(self) -> None:
        """One-step automation: ask for a URL, then crawl and report without a paths file."""
        self.console.print(
            Panel(
                Text.from_markup(
                    "[bold]Auto-scrape[/bold]\n"
                    "Give a target and scrapit will request the home page plus robots.txt and any sitemap, "
                    "then follow same-host links up to the crawl depth. No paths file needed.\n"
                    "[dim]Only use this on a site you own or are explicitly authorized to test.[/dim]"
                ),
                title="Auto-scrape",
                border_style="cyan",
            )
        )
        raw = text(self.console, "Target base URL", default=self.target or "")
        if raw is None:
            self.console.print("[yellow]Cancelled.[/yellow]")
            return
        raw = raw.strip()
        if not raw:
            if not self.target:
                self.console.print("[yellow]No target set.[/yellow]")
                return
        elif not self._assign_target(raw):
            return

        try:
            self.config.validate()
        except ConfigError as exc:
            self.console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Invalid settings", border_style="red"))
            return

        assert self.target is not None
        run_name = self.run_name
        if run_name is None and not self.assume_yes:
            answer = text(
                self.console,
                "Name for this run (optional; appears in the output folder name)",
                default="",
            )
            if answer is None:
                self.console.print("[yellow]Cancelled.[/yellow]")
                return
            run_name = answer.strip() or None

        summary = run_session(
            self.config,
            self.target,
            ["/"],
            self.console,
            assume_yes=self.assume_yes,
            run_name=run_name,
            discover_paths=True,
        )
        if summary is not None:
            self.last_run = summary

    def _find_subdomains(self) -> None:
        """Enumerate subdomains and write a list that can be fed back in as targets."""
        from .subdomains import (
            SubdomainError,
            enumerate_subdomains,
            load_wordlist,
            registrable_domain,
            write_outputs,
        )

        self.console.print(
            Panel(
                Text.from_markup(
                    "[bold]Find subdomains[/bold]\n"
                    "Queries public certificate-transparency and passive-DNS sources, then (optionally) "
                    "brute-forces common names over DNS. Writes a plain list you can reuse.\n"
                    "[dim]No technique finds every subdomain: names absent from public logs and the "
                    "wordlist cannot be discovered. Use your own wordlist to widen coverage.[/dim]"
                ),
                title="Subdomain finder",
                border_style="cyan",
            )
        )

        default_domain = host_of(self.target) if self.target else ""
        raw = text(self.console, "Domain (for example example.com)", default=default_domain)
        if raw is None:
            return
        try:
            domain = registrable_domain(raw.strip() or default_domain)
        except SubdomainError as exc:
            self.console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Invalid domain", border_style="red"))
            return

        answer = confirm(self.console, "Also brute-force common names over DNS?", True)
        if answer is None:
            return
        words: list[str] = []
        if answer:
            wl = text(self.console, "Wordlist file (blank = built-in list)", default="")
            if wl is None:
                return
            try:
                words = load_wordlist(wl.strip() or None)
            except SubdomainError as exc:
                self.console.print(f"[yellow]{escape(str(exc))}[/yellow]")
                return
            self.console.print(f"[dim]Brute forcing {len(words)} name(s) with parallel DNS lookups.[/dim]")

        self.console.print(f"[dim]Querying public sources for {escape(domain)}...[/dim]")

        def on_source(source: str, count: int, error: str | None) -> None:
            if error:
                self.console.print(f"  [yellow]{escape(source)}[/yellow] [dim]failed: {escape(error)}[/dim]")
            else:
                self.console.print(f"  [green]{escape(source)}[/green] {count} name(s)")

        def on_progress(name: str, is_real: bool, done: int, total: int) -> None:
            if is_real:
                self.console.print(f"   [green]+ {escape(name)}[/green]")

        try:
            result = enumerate_subdomains(
                domain,
                bruteforce=bool(words),
                words=words,
                on_source=on_source,
                on_progress=on_progress,
            )
        except KeyboardInterrupt:
            self.console.print("\n[yellow]Interrupted.[/yellow]")
            return

        try:
            paths = write_outputs(result, self.config.output_dir / "subdomains" / domain)
        except OSError as exc:
            self.console.print(Panel(f"[red]Cannot write output: {escape(str(exc))}[/red]", title="Write error", border_style="red"))
            return

        report.render_subdomain_summary(self.console, result, paths)
        self.last_subdomains = result

        if result.names and confirm(
            self.console,
            "Use the first subdomain as the new target for scraping?",
            False,
        ):
            if self._assign_target(result.names[0]):
                self.console.print(f"[green]Target set:[/green] {escape(self.target or '')}")

    def _view_results(self) -> None:
        run_dir = self.last_run.run_dir if self.last_run else find_latest_run(self.config.output_dir)
        if run_dir is None:
            self.console.print("[dim]No saved results yet. Run a fetch first.[/dim]")
            return
        try:
            payload = report.load_results(run_dir)
        except report.ReportError as exc:
            self.console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Results error", border_style="red"))
            return
        report.render_report_meta(self.console, payload)
        results = payload.get("results", [])
        report.render_results(self.console, results, title=f"Results ({len(results)})")

    def _export(self) -> None:
        run_dir = self.last_run.run_dir if self.last_run else find_latest_run(self.config.output_dir)
        if run_dir is None:
            raw = text(self.console, "Run folder to export", default="")
            if raw is None or not raw.strip():
                return
            run_dir = Path(raw).expanduser()
        if not (Path(run_dir) / "results.json").is_file():
            self.console.print(Panel(f"[red]No results.json in '{escape(str(run_dir))}'.[/red]", title="Export", border_style="red"))
            return
        try:
            payload = report.load_results(run_dir)
            report.write_all(run_dir, payload)
            archive = zip_directory(run_dir)
        except (report.ReportError, StorageError) as exc:
            self.console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Export failed", border_style="red"))
            return
        self.console.print(
            Panel(
                Text.from_markup(
                    f"[green]Reports regenerated:[/green]\n"
                    f"  {escape(str(Path(run_dir) / 'results.json'))}\n"
                    f"  {escape(str(Path(run_dir) / 'results.csv'))}\n"
                    f"  {escape(str(Path(run_dir) / 'report.md'))}\n"
                    f"[green]ZIP:[/green] {escape(str(archive))}"
                ),
                title="Export complete",
                border_style="green",
            )
        )

    def _notify_updates_on_start(self) -> None:
        """Check GitHub once when the tool opens and offer an update if one exists."""
        try:
            info = check_for_updates(__version__, timeout=3.0)
        except Exception:  # noqa: BLE001 - a network check must never block startup
            logger.debug("Startup update check failed", exc_info=True)
            return
        if info.error or not info.available:
            return
        self._offer_update(info, prompt="Update available. Update now?")

    def _offer_update(self, info: UpdateInfo, prompt: str = "Update now with git pull?") -> None:
        self.console.print(
            Panel(
                Text.from_markup(
                    f"[bold]Update available[/bold]\n"
                    f"Installed: {escape(info.current)}\n"
                    f"Latest:    [green]{escape(info.latest or '')}[/green]\n"
                    f"[dim]{escape(info.url)}[/dim]\n"
                    f"[dim]GitHub: https://github.com/{DEFAULT_REPO} (@{GITHUB_USER})[/dim]"
                ),
                title="Update check",
                border_style="cyan",
            )
        )
        if project_root() is None:
            self.console.print(
                "[yellow]This install is not a git checkout, so it cannot update itself.[/yellow]\n"
                f"[dim]Update with: pip install --upgrade "
                f"git+https://github.com/{DEFAULT_REPO}.git[/dim]"
            )
            return
        if not sys.stdin.isatty():
            self.console.print("[dim]Run 'scrapit --update' to apply it.[/dim]")
            return
        answer = confirm(self.console, prompt, False)
        if not answer:
            self.console.print("[dim]Update skipped. Use option 11 from the menu to update later.[/dim]")
            return
        result = perform_update()
        if result.error:
            self.console.print(
                Panel(
                    f"[red]{escape(result.error)}[/red]\n[dim]{escape(result.detail)}[/dim]",
                    title="Update failed",
                    border_style="red",
                )
            )
            return
        message = "Updated." if result.updated else "Already up to date."
        self.console.print(
            Panel(
                Text.from_markup(
                    f"[green]{message}[/green]\n[dim]{escape(result.detail)}[/dim]\n"
                    "Restart scrapit to use the new version."
                ),
                title="Update complete",
                border_style="green",
            )
        )

    def _check_updates(self) -> None:
        self.console.print("[dim]Checking GitHub for the latest release...[/dim]")
        info = check_for_updates(__version__)
        if info.error:
            self.console.print(
                Panel(f"[yellow]{escape(info.error)}[/yellow]", title="Update check", border_style="yellow")
            )
            return
        if not info.available:
            self.console.print(
                Panel(
                    f"[green]scrapit {escape(__version__)} is up to date.[/green]\n"
                    f"[dim]Latest release: {escape(info.latest or __version__)}[/dim]",
                    title="No updates",
                    border_style="green",
                )
            )
            return
        self._offer_update(info)

    def _help(self) -> None:
        self.console.print(
            Panel(
                Text.from_markup(
                    f"[bold]scrapit {__version__}[/bold]\n"
                    f"[dim]by @{__author__} - https://github.com/{DEFAULT_REPO}[/dim]\n\n"
                    "Sends one polite GET request per path, sequentially, and saves whatever it finds. "
                    "No authentication bypass, header spoofing, proxying or form submission is attempted.\n\n"
                    "[bold]Auto-scrape[/bold] - option 3 (or --discover) crawls the target instead of using a "
                    "paths file: it reads robots.txt and sitemaps, then follows same-host links up to the "
                    "crawl depth. Off-host links, assets and non-http(s) schemes are ignored.\n\n"
                    "[bold]Subdomains[/bold] - option 4 (or --subdomains DOMAIN) queries public certificate-"
                    "transparency and passive-DNS sources, then brute-forces common names over DNS with "
                    "wildcard-noise filtering. It writes subdomains.txt/csv/json; the .txt is a plain list "
                    "you can reuse, one host per line. No method finds every subdomain - names missing from "
                    "public logs and your wordlist cannot be discovered.\n\n"
                    "[bold]Paths file[/bold] - one path per line. Blank lines and lines starting with # are ignored. "
                    "A leading slash is added when missing. Full URLs are accepted only when their host matches "
                    "the target. Exact duplicates are removed and order is preserved.\n\n"
                    "[bold]Example[/bold]\n"
                    "  /admin\n"
                    "  /js\n"
                    "  robots.txt\n"
                    "  # comments are ignored\n\n"
                    "[bold]Status colors[/bold]"
                ),
                title="Help / About",
                border_style="cyan",
            )
        )
        table = Table(header_style="bold", show_header=False)
        table.add_column("Color")
        table.add_column("Meaning")
        for color, meaning in [
            ("green", "2xx - found and downloaded"),
            ("yellow", "3xx redirect, 429 rate limit, soft 404, too large"),
            ("orange3", "401 / 403 - protected, logged only"),
            ("red", "404 / 410, server errors, network errors"),
            ("dim", "blocked by robots.txt or skipped"),
        ]:
            table.add_row(Text(color, style=color), meaning)
        self.console.print(table)
        self.console.print(
            "[dim]Ctrl+C stops a run and saves partial results. Use only on sites you own or are "
            "explicitly authorized to test.[/dim]"
        )

    def _require_ready(self) -> bool:
        if not self.target:
            self.console.print("[yellow]Set a target URL first (option 1).[/yellow]")
            return False
        if not self.paths and not self.config.discover:
            self.console.print(
                "[yellow]Load a paths file first (option 2), or turn on Auto-scrape (option 3).[/yellow]"
            )
            return False
        return True
