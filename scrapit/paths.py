"""Load, clean and validate the paths file."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .utils import SCHEME_RE, encode_path, has_control_chars, same_host


class PathFileError(Exception):
    """Friendly error raised for missing, empty or unreadable path files."""


@dataclass
class PathLoadResult:
    paths: list[str] = field(default_factory=list)
    total_lines: int = 0
    duplicates_removed: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def summary(self) -> str:
        extras: list[str] = []
        if self.duplicates_removed:
            extras.append(f"{self.duplicates_removed} duplicate(s) removed")
        if self.skipped:
            extras.append(f"{self.skipped} skipped")
        base = f"Loaded {len(self.paths)} path(s)"
        if extras:
            base += " (" + ", ".join(extras) + ")"
        return base


def normalize_path_entry(raw: str, target_url: str | None) -> tuple[str | None, str | None]:
    """Return (normalized_path, warning). A None path means the line was skipped."""
    text = raw.strip()
    if not text:
        return None, "blank line"
    if text.startswith("#"):
        return None, "comment"

    if SCHEME_RE.match(text):
        try:
            parsed = urlsplit(text)
        except ValueError as exc:
            return None, f"invalid URL '{text}': {exc}"
        if parsed.scheme.lower() not in ("http", "https"):
            return None, f"unsupported scheme in '{text}'"
        if not target_url:
            return None, f"absolute URL '{text}' skipped because no target is set"
        if not same_host(text, target_url):
            return None, f"absolute URL '{text}' is outside the target host"
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        text = path

    text = text.split("#", 1)[0]
    if has_control_chars(text):
        return None, f"control characters in '{raw.strip()[:60]}'"
    if not text:
        return None, "empty path after cleanup"
    if not text.startswith("/"):
        text = "/" + text
    text = encode_path(text)
    return text, None


def load_paths(file_path: str | Path, target_url: str | None = None) -> PathLoadResult:
    """Read paths from a UTF-8 text file following the documented rules."""
    path = Path(file_path).expanduser()
    if not path.exists():
        raise PathFileError(f"Paths file not found: {path}")
    if not path.is_file():
        raise PathFileError(f"Not a file: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise PathFileError(f"Cannot read '{path}': {exc}") from exc

    if not raw_text.strip():
        raise PathFileError(f"Paths file is empty: {path}")

    result = PathLoadResult(source=str(path))
    seen: set[str] = set()
    for line in raw_text.splitlines():
        result.total_lines += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        normalized, warning = normalize_path_entry(line, target_url)
        if normalized is None:
            if warning in ("blank line", "comment"):
                continue
            result.skipped += 1
            if warning:
                result.warnings.append(warning)
            continue
        if normalized in seen:
            result.duplicates_removed += 1
            continue
        seen.add(normalized)
        result.paths.append(normalized)

    if not result.paths:
        detail = f" ({result.skipped} line(s) skipped)" if result.skipped else ""
        raise PathFileError(f"No valid paths found in '{path}'{detail}.")

    return result
