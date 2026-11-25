"""Command-line entry point: flags for automation, interactive menu by default."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from . import __version__
from .config import Config
from .fetcher import run_session
from .menu import MenuApp
from .paths import PathFileError, load_paths
from .update import DEFAULT_REPO, check_for_updates, perform_update, project_root

MB = 1024 * 1024
logger = logging.getLogger("scrapit")


def run_update_command(console: Console, *, apply_update: bool) -> int:
    info = check_for_updates(__version__)
    if info.error:
        console.print(f"[yellow]{escape(info.error)}[/yellow]")
        return 1
    if not info.available:
        console.print(
            f"[green]scrapit {__version__} is up to date.[/green] "
            f"(latest: {escape(info.latest or __version__)})"
        )
        return 0
    console.print(
        f"[bold]Update available:[/bold] {__version__} -> [green]{escape(info.latest or '')}[/green]\n"
        f"[dim]{escape(info.url)}[/dim]"
    )
    if not apply_update:
        console.print("[dim]Run 'scrapit --update' to update this checkout.[/dim]")
        return 0
    if project_root() is None:
        console.print(
            "[yellow]This install is not a git checkout, so it cannot update itself.[/yellow]\n"
            f"[dim]Update with: pip install --upgrade git+https://github.com/{DEFAULT_REPO}.git[/dim]"
        )
        return 1
    result = perform_update()
    if result.error:
        console.print(f"[red]{escape(result.error)}[/red]")
        if result.detail:
            console.print(f"[dim]{escape(result.detail)}[/dim]")
        return 1
    console.print(
        "[green]Updated.[/green] Restart scrapit to use the new version."
        if result.updated
        else "[green]Already up to date.[/green]"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scrapit",
        description=(
            "Sequentially request paths on a target site, download what exists and write a report. "
            "Use only on sites you own or are explicitly authorized to test."
        ),
    )
    parser.add_argument("--target", help="Base URL, for example https://example.com")
    parser.add_argument("--paths", dest="paths_file", help="Text file with one path per line")
    parser.add_argument("--delay", type=float, help="Seconds between requests (default 1.0, min 0.2)")
    parser.add_argument("--timeout", type=float, help="Per-request timeout in seconds (default 10)")
    parser.add_argument("--retries", type=int, help="Retries for timeouts/429/5xx (default 2)")
    parser.add_argument("--max-size-mb", type=float, help="Max download size per file in MB (default 10)")
    parser.add_argument("--output", help="Output folder (default ./output)")
    parser.add_argument("--user-agent", help="Custom User-Agent")
    parser.add_argument("--no-robots", action="store_true", help="Do not fetch or enforce robots.txt")
    parser.add_argument("--no-redirects", action="store_true", help="Do not follow redirects")
    parser.add_argument("--no-verify", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--allow-private", action="store_true", help="Allow localhost/private targets")
    parser.add_argument("--assets", action="store_true", help="Download same-host JS/CSS/images linked from HTML")
    parser.add_argument("--no-save-headers", action="store_true", help="Do not save response headers")
    parser.add_argument("--max-paths", type=int, help="Request cap per run (default 500)")
    parser.add_argument("--zip", action="store_true", help="Create a ZIP of the run folder")
    parser.add_argument("--name", dest="run_name", help="Name for this run (used in the output folder name)")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Auto mode: crawl from the target and follow same-host links without a paths file",
    )
    parser.add_argument(
        "--depth",
        type=int,
        help="Maximum link-following depth in discovery mode (default 3)",
    )
    parser.add_argument("--no-sitemap", action="store_true", help="Do not seed the crawl from sitemaps")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmations (does NOT replace --i-have-permission)",
    )
    parser.add_argument(
        "--i-have-permission",
        action="store_true",
        help="Confirm you own the target or have explicit written permission (required non-interactively)",
    )
    parser.add_argument("--debug", action="store_true", help="Show a traceback when an unexpected error occurs")
    parser.add_argument("--check-updates", action="store_true", help="Check GitHub for a newer release and exit")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Check for a newer release and update this git checkout (git pull --ff-only)",
    )
    parser.add_argument("--version", action="version", version=f"scrapit {__version__}")
    parser.add_argument(
        "--subdomains",
        metavar="DOMAIN",
        help="Find subdomains of DOMAIN (passive CT/passive-DNS sources + optional DNS brute force) and exit",
    )
    parser.add_argument("--wordlist", help="Wordlist file for --subdomains brute force (default: built-in)")
    parser.add_argument("--no-bruteforce", action="store_true", help="Passive sources only for --subdomains")
    parser.add_argument("--workers", type=int, help="Parallel DNS lookups for brute force (default 20)")
    parser.add_argument("--output-file", help="Where to write the subdomain list (default: alongside the run folder)")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    config = Config()
    if args.delay is not None:
        config.delay = args.delay
    if args.timeout is not None:
        config.timeout = args.timeout
    if args.retries is not None:
        config.max_retries = args.retries
    if args.max_size_mb is not None:
        config.max_download_bytes = int(args.max_size_mb * MB)
    if args.output:
        config.output_dir = Path(args.output).expanduser()
    if args.user_agent:
        config.user_agent = args.user_agent
    if args.no_robots:
        config.respect_robots = False
    if args.no_redirects:
        config.follow_redirects = False
    if args.no_verify:
        config.verify_tls = False
    if args.allow_private:
        config.allow_private = True
    if args.assets:
        config.download_assets = True
    if args.no_save_headers:
        config.save_headers = False
    if args.max_paths is not None:
        config.max_paths = args.max_paths
        config.warn_paths = min(config.warn_paths, config.max_paths)
    if args.discover:
        config.discover = True
    if args.depth is not None:
        config.max_depth = args.depth
    if args.no_sitemap:
        config.use_sitemap = False
    return config


def run_subdomain_command(console: Console, args: argparse.Namespace) -> int:
    from . import report as report_module
    from .subdomains import (
        SubdomainError,
        enumerate_subdomains,
        load_wordlist,
        registrable_domain,
        write_outputs,
    )
    from .storage import RunStorage, StorageError

    try:
        domain = registrable_domain(args.subdomains)
    except SubdomainError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Invalid domain", border_style="red"))
        return 2

    try:
        words = load_wordlist(args.wordlist) if not args.no_bruteforce else []
    except SubdomainError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Wordlist error", border_style="red"))
        return 2

    try:
        storage = RunStorage(args.output or "output", domain, run_name=args.run_name)
    except StorageError as exc:
        console.print(Panel(f"[red]{escape(str(exc))}[/red]", title="Output folder error", border_style="red"))
        return 1

    console.print(
        f"[bold]Finding subdomains for[/bold] {escape(domain)}\n"
        f"[dim]{'Passive sources + brute force (' + str(len(words)) + ' words)' if words else 'Passive sources only'}"
        f" - writing to {escape(str(storage.run_dir))}[/dim]"
    )

    def on_source(source: str, count: int, error: str | None) -> None:
        if error:
            console.print(f"  [yellow]{escape(source)}[/yellow] [dim]failed: {escape(error)}[/dim]")
        else:
            console.print(f"  [green]{escape(source)}[/green] {count} name(s)")

    def on_progress(name: str, is_real: bool, done: int, total: int) -> None:
        if is_real:
            console.print(f"   [green]+ {escape(name)}[/green] [dim]({done}/{total})[/dim]")

    try:
        result = enumerate_subdomains(
            domain,
            bruteforce=bool(words),
            words=words,
            workers=args.workers or 20,
            on_source=on_source,
            on_progress=on_progress,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted - no output written.[/yellow]")
        return 130

    try:
        paths = write_outputs(result, storage.run_dir)
    except OSError as exc:
        console.print(Panel(f"[red]Cannot write output: {escape(str(exc))}[/red]", title="Write error", border_style="red"))
        return 1

    report_module.render_subdomain_summary(console, result, paths)
    if args.output_file:
        target = Path(args.output_file).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(result.names) + ("\n" if result.names else ""), encoding="utf-8")
            console.print(f"[green]Copied list to[/green] {escape(str(target))}")
        except OSError as exc:
            console.print(f"[yellow]Could not write {escape(str(target))}: {escape(str(exc))}[/yellow]")
    return 0 if result.results else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console(highlight=False)

    if args.subdomains:
        try:
            return run_subdomain_command(console, args)
        except Exception as exc:  # noqa: BLE001 - never dump a traceback by default
            logger.exception("Subdomain run failed")
            console.print(
                Panel(
                    f"[red]{type(exc).__name__}: {escape(str(exc))}[/red]\n"
                    "[dim]Re-run with --debug for a traceback.[/dim]",
                    title="Unexpected error",
                    border_style="red",
                )
            )
            if args.debug:
                console.print_exception()
            return 1

    if args.check_updates or args.update:
        return run_update_command(console, apply_update=args.update)

    try:
        config = config_from_args(args)
        direct = bool(args.target and (args.paths_file or config.discover) and (args.i_have_permission or args.yes))

        if direct:
            if not args.i_have_permission:
                console.print(
                    Panel(
                        "[red]Non-interactive runs require --i-have-permission.[/red]\n"
                        "Only pass that flag for a site you own or are explicitly authorized to test.\n"
                        "Omit --yes to use the interactive menu (which still asks you to confirm the host).",
                        title="Permission required",
                        border_style="red",
                    )
                )
                return 2
            paths: list[str] = ["/"]
            if args.paths_file:
                try:
                    loaded = load_paths(args.paths_file, args.target)
                except PathFileError as exc:
                    console.print(Panel(f"[red]{exc}[/red]", title="Paths error", border_style="red"))
                    return 2
                paths = loaded.paths
            summary = run_session(
                config,
                args.target,
                paths,
                console,
                assume_yes=args.yes,
                i_have_permission=True,
                interactive=sys.stdin.isatty(),
                create_zip=args.zip,
                run_name=args.run_name,
                discover_paths=config.discover,
            )
            return 0 if summary is not None else 2

        paths: list[str] | None = None
        paths_file = args.paths_file
        if paths_file:
            try:
                loaded = load_paths(paths_file, args.target)
                paths = loaded.paths
                console.print(f"[green]{loaded.summary}[/green]")
            except PathFileError as exc:
                console.print(f"[yellow]Warning:[/yellow] {escape(str(exc))}")

        app = MenuApp(
            console=console,
            config=config,
            target=args.target,
            paths_file=paths_file,
            paths=paths,
            assume_yes=args.yes,
            run_name=args.run_name,
        )
        app.run()
        return 0
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted - exiting.[/yellow]")
        return 130
    except Exception as exc:  # last-resort guard: never show a traceback by default
        logger.exception("Fatal error")
        console.print(
            Panel(
                f"[red]{type(exc).__name__}: {exc}[/red]\n"
                "[dim]Nothing was left running. Re-run with --debug for a traceback.[/dim]",
                title="Unexpected error",
                border_style="red",
            )
        )
        if args.debug:
            console.print_exception()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
