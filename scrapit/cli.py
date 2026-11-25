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

MB = 1024 * 1024
logger = logging.getLogger("scrapit")


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
    parser.add_argument("--version", action="version", version=f"scrapit {__version__}")
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
    return config


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console(highlight=False)

    try:
        config = config_from_args(args)
        direct = bool(args.target and args.paths_file and (args.i_have_permission or args.yes))

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
            try:
                loaded = load_paths(args.paths_file, args.target)
            except PathFileError as exc:
                console.print(Panel(f"[red]{exc}[/red]", title="Paths error", border_style="red"))
                return 2
            summary = run_session(
                config,
                args.target,
                loaded.paths,
                console,
                assume_yes=args.yes,
                i_have_permission=True,
                interactive=sys.stdin.isatty(),
                create_zip=args.zip,
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
