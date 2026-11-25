"""Saving downloads under a safe, predictable folder layout."""

from __future__ import annotations

import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .utils import (
    ext_from_content_type,
    has_extension,
    hash_suffix,
    human_bytes,
    safe_join,
    sanitize_component,
)


class StorageError(Exception):
    """Raised when a file or folder cannot be created."""


@dataclass
class SavedFile:
    relative_path: str
    headers_path: str | None
    bytes_written: int


class RunStorage:
    """Owns one timestamped run folder: files/, headers/ and reports."""

    def __init__(
        self,
        output_dir: str | Path,
        host: str,
        save_headers: bool = True,
        timestamp: str | None = None,
        run_name: str | None = None,
    ) -> None:
        self.output_root = Path(output_dir).expanduser()
        self.host = sanitize_component(host or "unknown-host")
        self.host_dir = self.output_root / self.host
        self.save_headers = save_headers

        stamp = timestamp or time.strftime("%Y-%m-%d_%H-%M-%S")
        if run_name and run_name.strip():
            stamp = f"{sanitize_component(run_name.strip())}_{stamp}"
        run_dir = self.host_dir / stamp
        counter = 2
        while run_dir.exists():
            run_dir = self.host_dir / f"{stamp}_{counter}"
            counter += 1

        self.run_dir = run_dir
        self.files_dir = run_dir / "files"
        self.headers_dir = run_dir / "headers"
        try:
            self.files_dir.mkdir(parents=True, exist_ok=False)
            if save_headers:
                self.headers_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise StorageError(f"Cannot create output folder '{run_dir}': {exc}") from exc

    def relative_target(self, path: str, content_type: str | None) -> Path:
        """Map a URL path to a safe relative file path."""
        parsed = urlsplit(path or "/")
        url_path = parsed.path or "/"
        query = parsed.query

        is_dir = url_path.endswith("/") or url_path in ("", "/")
        segments = [seg for seg in url_path.split("/") if seg not in ("", ".")]
        segments = [sanitize_component(seg) for seg in segments]

        ext = ext_from_content_type(content_type)
        suffix = "_" + hash_suffix(query) if query else ""

        if segments and not is_dir and has_extension(segments[-1]):
            last = segments[-1]
            if suffix:
                dot = last.rfind(".")
                if dot > 0:
                    last = last[:dot] + suffix + last[dot:]
                else:
                    last += suffix
            parts = segments[:-1] + [last]
            return Path(*parts)

        name = f"index{suffix}.{ext.lstrip('.')}"
        return Path(*segments, name) if segments else Path(name)

    def headers_relative(self, rel: Path) -> Path:
        parts = list(rel.parts)
        if len(parts) > 1 and parts[-1].startswith("index."):
            parts = parts[:-1]
        parts[-1] = parts[-1] + ".headers.txt"
        return Path(*parts)

    def _dedupe(self, target: Path) -> Path:
        if not target.exists():
            return target
        counter = 2
        while True:
            candidate = target.with_name(f"{target.stem}_{counter}{target.suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def save(
        self,
        path: str,
        content_type: str | None,
        body: bytes,
        request_url: str,
        final_url: str | None,
        status: int | None,
        response_headers: list[tuple[str, str]],
    ) -> SavedFile:
        rel = self.relative_target(path, content_type)
        target = safe_join(self.files_dir, *rel.parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target = self._dedupe(target)
            with open(target, "wb") as handle:
                handle.write(body)
        except OSError as exc:
            free = self._free_space_hint()
            raise StorageError(f"Cannot save '{path}' to '{target}': {exc}{free}") from exc

        actual_rel = target.relative_to(self.files_dir.resolve())
        saved = SavedFile(
            relative_path="files/" + actual_rel.as_posix(),
            headers_path=None,
            bytes_written=len(body),
        )

        if self.save_headers:
            headers_rel = self.headers_relative(actual_rel)
            headers_target = safe_join(self.headers_dir, *headers_rel.parts)
            try:
                headers_target.parent.mkdir(parents=True, exist_ok=True)
                headers_target.write_text(
                    self._headers_text(request_url, final_url, status, content_type, response_headers),
                    encoding="utf-8",
                )
                saved.headers_path = "headers/" + headers_rel.as_posix()
            except OSError as exc:
                raise StorageError(f"Cannot save headers for '{path}': {exc}") from exc

        return saved

    @staticmethod
    def _headers_text(
        request_url: str,
        final_url: str | None,
        status: int | None,
        content_type: str | None,
        response_headers: list[tuple[str, str]],
    ) -> str:
        lines = [
            f"Request URL: {request_url}",
            f"Final URL: {final_url or request_url}",
            f"Status: {status if status is not None else 'n/a'}",
            f"Content-Type: {content_type or 'unknown'}",
            "",
        ]
        lines.extend(f"{key}: {value}" for key, value in response_headers)
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _free_space_hint() -> str:
        try:
            usage = os.statvfs(Path.cwd())
            free = usage.f_bavail * usage.f_frsize
            return f" (free space: {human_bytes(free)})"
        except (OSError, AttributeError):
            return ""

    def zip_run(self) -> Path:
        return zip_directory(self.run_dir)


def zip_directory(run_dir: str | Path) -> Path:
    """Zip a run folder next to itself. Returns the archive path."""
    run_path = Path(run_dir).expanduser()
    if not run_path.is_dir():
        raise StorageError(f"Run folder does not exist: {run_path}")
    archive = run_path.parent / f"{run_path.name}.zip"
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file in sorted(run_path.rglob("*")):
                if file.is_file():
                    zf.write(file, file.relative_to(run_path).as_posix())
    except OSError as exc:
        raise StorageError(f"Cannot create ZIP '{archive}': {exc}") from exc
    return archive


def find_latest_run(output_dir: str | Path) -> Path | None:
    root = Path(output_dir).expanduser()
    if not root.is_dir():
        return None
    candidates = list(root.glob("*/*/results.json"))
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return latest.parent
