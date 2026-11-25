"""JSON / CSV / Markdown reports and rich rendering helpers."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __author__, __version__
from .utils import human_bytes


class ReportError(Exception):
    """Raised when a report cannot be written or read."""


LABEL_STYLES = {
    "FOUND": "green",
    "SOFT_404": "yellow",
    "REDIRECT": "yellow",
    "AUTH_REQUIRED": "orange3",
    "FORBIDDEN": "orange3",
    "NOT_FOUND": "red",
    "RATE_LIMITED": "yellow",
    "SERVER_ERROR": "red",
    "NETWORK_ERROR": "red",
    "TOO_LARGE": "yellow",
    "BLOCKED_BY_ROBOTS": "dim",
    "SAVE_ERROR": "red",
    "CLIENT_ERROR": "red",
    "SAFETY_BLOCK": "red",
    "SKIPPED": "dim",
    "PENDING": "dim",
}

CATEGORY_MAP = {
    "FOUND": "found",
    "SOFT_404": "soft_404",
    "REDIRECT": "redirects",
    "AUTH_REQUIRED": "protected",
    "FORBIDDEN": "protected",
    "NOT_FOUND": "not_found",
    "RATE_LIMITED": "errors",
    "SERVER_ERROR": "errors",
    "NETWORK_ERROR": "errors",
    "TOO_LARGE": "errors",
    "SAVE_ERROR": "errors",
    "CLIENT_ERROR": "errors",
    "SAFETY_BLOCK": "errors",
    "SKIPPED": "skipped",
}

CATEGORY_ORDER = ["found", "redirects", "protected", "not_found", "errors", "blocked", "soft_404", "skipped"]

CSV_COLUMNS = [
    "path",
    "url",
    "final_url",
    "status",
    "label",
    "content_type",
    "size_bytes",
    "sha256",
    "response_ms",
    "saved_as",
    "error",
]


def label_style(label: str | None) -> str:
    return LABEL_STYLES.get(label or "", "white")


def compute_counts(results: Iterable[dict[str, Any] | Any]) -> dict[str, int]:
    counts = {key: 0 for key in CATEGORY_ORDER}
    for item in results:
        label = item.get("label") if isinstance(item, dict) else getattr(item, "label", None)
        if label == "BLOCKED_BY_ROBOTS":
            counts["blocked"] += 1
            continue
        counts[CATEGORY_MAP.get(label or "", "errors")] += 1
    return counts


def build_payload(
    *,
    target: str,
    host: str,
    run_dir: str,
    started_at: str,
    finished_at: str,
    duration_s: float,
    requests_sent: int,
    stopped_early: bool,
    settings: dict[str, Any],
    counts: dict[str, int],
    robots_note: str,
    soft404_note: str,
    paths_loaded: int,
    results: list[dict[str, Any]],
    redirect_chains: int = 0,
) -> dict[str, Any]:
    return {
        "tool": "scrapit",
        "version": __version__,
        "author": __author__,
        "target": target,
        "host": host,
        "run_dir": run_dir,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": round(duration_s, 3),
        "requests_sent": requests_sent,
        "stopped_early": stopped_early,
        "paths_loaded": paths_loaded,
        "redirect_chains": redirect_chains,
        "settings": settings,
        "counts": counts,
        "robots": robots_note,
        "soft404": soft404_note,
        "results": results,
    }


def _as_dict(item: dict[str, Any] | Any) -> dict[str, Any]:
    return item if isinstance(item, dict) else item.to_dict()


def results_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def results_to_csv(results: Iterable[dict[str, Any] | Any]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for item in results:
        row = _as_dict(item)
        writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in CSV_COLUMNS})
    return buffer.getvalue()


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _settings_rows(settings: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        ("Target delay", f"{settings.get('delay')} s"),
        ("Timeout", f"{settings.get('timeout')} s"),
        ("Max retries", str(settings.get("max_retries"))),
        ("Follow redirects", "yes" if settings.get("follow_redirects") else "no"),
        ("Max download size", f"{settings.get('max_download_mb')} MB"),
        ("Respect robots.txt", "yes" if settings.get("respect_robots") else "no"),
        ("User-Agent", str(settings.get("user_agent"))),
        ("Extra headers", json.dumps(settings.get("extra_headers") or {})),
        ("Verify TLS", "yes" if settings.get("verify_tls") else "no"),
        ("Save response headers", "yes" if settings.get("save_headers") else "no"),
        ("Download linked assets", "yes" if settings.get("download_assets") else "no"),
        ("Allow localhost/private targets", "yes" if settings.get("allow_private") else "no"),
        ("Max paths per run", str(settings.get("max_paths"))),
        ("Output folder", str(settings.get("output_dir"))),
    ]


def results_to_markdown(payload: dict[str, Any]) -> str:
    results = [_as_dict(item) for item in payload.get("results", [])]
    counts = payload.get("counts", {})
    lines: list[str] = [
        "# scrapit report",
        "",
        f"**Author:** @{payload.get('author') or __author__}",
        f"**Tool:** scrapit {payload.get('version')}",
        f"**Target:** {payload.get('target')}",
        f"**Host:** {payload.get('host')}",
        f"**Started:** {payload.get('started_at')}",
        f"**Finished:** {payload.get('finished_at')}",
        f"**Duration:** {payload.get('duration_s')} s",
        f"**Requests sent:** {payload.get('requests_sent')}",
        f"**Run folder:** `{payload.get('run_dir')}`",
        "",
    ]
    if payload.get("stopped_early"):
        lines.extend(["> Run stopped early (Ctrl+C). Results below are partial.", ""])

    lines.extend(["## Settings", "", "| Setting | Value |", "| --- | --- |"])
    for key, value in _settings_rows(payload.get("settings", {})):
        lines.append(f"| {key} | {_md(value)} |")
    lines.append("")

    lines.extend(["## Summary", "", "| Category | Count |", "| --- | --- |"])
    for key in CATEGORY_ORDER:
        lines.append(f"| {key.replace('_', ' ').title()} | {counts.get(key, 0)} |")
    lines.append("")

    found = [r for r in results if r.get("label") == "FOUND"]
    lines.extend([f"## Found ({len(found)})", ""])
    if found:
        lines.extend(["| Path | Status | Type | Size | Time | Saved as |", "| --- | --- | --- | --- | --- | --- |"])
        for row in found:
            lines.append(
                f"| {_md(row.get('path'))} | {row.get('status')} | {_md(row.get('content_type') or '')} "
                f"| {human_bytes(row.get('size_bytes'))} | {row.get('response_ms')} ms "
                f"| {_md(row.get('saved_as') or '')} |"
            )
    else:
        lines.append("_No downloadable pages found._")
    lines.append("")

    protected = [r for r in results if r.get("label") in ("AUTH_REQUIRED", "FORBIDDEN")]
    lines.extend([f"## Protected ({len(protected)})", ""])
    if protected:
        for row in protected:
            lines.append(f"- `{row.get('path')}` — {row.get('status')} {row.get('label')} (not bypassed)")
    else:
        lines.append("_No protected paths encountered._")
    lines.append("")

    missing = [r for r in results if r.get("label") == "NOT_FOUND"]
    lines.extend([f"## Not found ({len(missing)})", ""])
    if missing:
        lines.extend(f"- `{row.get('path')}` — {row.get('status')}" for row in missing)
    else:
        lines.append("_None._")
    lines.append("")

    errors = [
        r
        for r in results
        if r.get("label")
        in ("NETWORK_ERROR", "SERVER_ERROR", "RATE_LIMITED", "TOO_LARGE", "SAVE_ERROR", "CLIENT_ERROR", "SAFETY_BLOCK")
    ]
    lines.extend([f"## Errors ({len(errors)})", ""])
    if errors:
        lines.extend(["| Path | Label | Detail |", "| --- | --- | --- |"])
        for row in errors:
            lines.append(f"| {_md(row.get('path'))} | {row.get('label')} | {_md(row.get('error') or '')} |")
    else:
        lines.append("_None._")
    lines.append("")

    blocked = [r for r in results if r.get("label") == "BLOCKED_BY_ROBOTS"]
    if blocked:
        lines.extend([f"## Blocked by robots.txt ({len(blocked)})", ""])
        lines.extend(f"- `{row.get('path')}`" for row in blocked)
        lines.append("")

    lines.extend(
        [
            "## Notes",
            "",
            f"- robots.txt: {payload.get('robots')}",
            f"- Soft 404 detection: {payload.get('soft404')}",
            f"- Redirect chains followed: {payload.get('redirect_chains', 0)}",
            "- Only plain, sequential GET requests were sent. No authentication bypass, header spoofing, proxying or other evasion was attempted.",
            "- Every redirect hop was resolved and re-checked against the private-address policy before it was requested.",
            "",
        ]
    )
    return "\n".join(lines)


def write_all(run_dir: str | Path, payload: dict[str, Any]) -> dict[str, Path]:
    """Write results.json, results.csv and report.md into a run folder."""
    directory = Path(run_dir)
    payload = dict(payload)
    payload["results"] = [_as_dict(item) for item in payload.get("results", [])]
    paths = {
        "json": directory / "results.json",
        "csv": directory / "results.csv",
        "markdown": directory / "report.md",
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        paths["json"].write_text(results_to_json(payload), encoding="utf-8")
        paths["csv"].write_text(results_to_csv(payload["results"]), encoding="utf-8", newline="")
        paths["markdown"].write_text(results_to_markdown(payload), encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"Cannot write reports in '{directory}': {exc}") from exc
    return paths


def load_results(run_dir: str | Path) -> dict[str, Any]:
    path = Path(run_dir) / "results.json"
    if not path.is_file():
        raise ReportError(f"No results.json in '{run_dir}'.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"Cannot read '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise ReportError(f"Unexpected results format in '{path}'.")
    return data


def build_results_table(results: Iterable[dict[str, Any] | Any], title: str | None = None) -> Table:
    table = Table(title=title, header_style="bold", show_lines=False, expand=False)
    table.add_column("Path", style="cyan", no_wrap=True, max_width=38)
    table.add_column("Status", no_wrap=True)
    table.add_column("Type", max_width=22)
    table.add_column("Size", justify="right")
    table.add_column("Time", justify="right")
    table.add_column("Saved As", max_width=40)
    for item in results:
        row = _as_dict(item)
        label = row.get("label") or "PENDING"
        status = str(row.get("status")) if row.get("status") is not None else "-"
        type_text = (row.get("content_type") or "-").split(";")[0]
        saved = row.get("saved_as") or ""
        saved = saved.removeprefix("files/")
        table.add_row(
            Text(str(row.get("path") or ""), style=label_style(label)),
            Text(f"{status} {label}", style=label_style(label)),
            Text(type_text),
            Text(human_bytes(row.get("size_bytes"))),
            Text(f"{row.get('response_ms', 0)} ms"),
            Text(saved, style="dim"),
        )
    return table


def render_results(console: Console, results: Iterable[dict[str, Any] | Any], title: str | None = None) -> None:
    table = build_results_table(results, title=title)
    if not table.rows:
        console.print("[dim]No results to display.[/dim]")
        return
    console.print(table)


def render_summary(console: Console, payload: dict[str, Any], zip_path: Path | None = None) -> None:
    counts = payload.get("counts", {})
    lines = [
        f"[bold green]Done in {payload.get('duration_s')}s[/bold green]",
        f"[dim]scrapit by @{__author__} - https://github.com/{__author__}/scrapit[/dim]",
        (
            f"Found: [green]{counts.get('found', 0)}[/green]   "
            f"Redirects: [yellow]{counts.get('redirects', 0)}[/yellow]   "
            f"Protected: [orange3]{counts.get('protected', 0)}[/orange3]   "
            f"Not found: [red]{counts.get('not_found', 0)}[/red]   "
            f"Errors: [red]{counts.get('errors', 0)}[/red]   "
            f"Blocked by robots: [dim]{counts.get('blocked', 0)}[/dim]"
        ),
    ]
    if counts.get("soft_404"):
        lines.append(f"Soft 404 (no page really exists): [yellow]{counts['soft_404']}[/yellow]")
    if payload.get("redirect_chains"):
        lines.append(
            f"Redirect chains followed: [yellow]{payload['redirect_chains']}[/yellow] "
            "[dim](final responses counted by their final status)[/dim]"
        )
    if payload.get("stopped_early"):
        lines.append("[yellow]Stopped early - partial results were saved.[/yellow]")
    lines.append(f"Requests sent: {payload.get('requests_sent')}")
    lines.append(f"Saved to: [bold]{escape(str(payload.get('run_dir')))}[/bold]")
    if zip_path is not None:
        lines.append(f"ZIP: [bold]{escape(str(zip_path))}[/bold]")
    console.print(Panel(Text.from_markup("\n".join(lines)), title="Run complete", border_style="green"))


def render_report_meta(console: Console, payload: dict[str, Any]) -> None:
    counts = payload.get("counts", {})
    console.print(
        Panel(
            Text.from_markup(
                f"[bold]Target:[/bold] {escape(str(payload.get('target')))}\n"
                f"[bold]Run:[/bold] {escape(str(payload.get('run_dir')))}\n"
                f"[bold]Started:[/bold] {payload.get('started_at')}  "
                f"[bold]Duration:[/bold] {payload.get('duration_s')}s\n"
                f"[bold]Counts:[/bold] found {counts.get('found', 0)}, "
                f"soft-404 {counts.get('soft_404', 0)}, redirects {counts.get('redirects', 0)}, "
                f"protected {counts.get('protected', 0)}, not found {counts.get('not_found', 0)}, "
                f"errors {counts.get('errors', 0)}, blocked {counts.get('blocked', 0)}"
            ),
            title="Last results",
            border_style="cyan",
        )
    )
