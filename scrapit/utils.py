"""Shared helpers: URL normalization, IP checks, filename sanitizing."""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import logging
import re
import secrets
import socket
import time
from collections.abc import Callable, Iterable
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
BARE_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
HOST_RE = re.compile(r"^[a-z0-9._\-]+$")

INVALID_FILENAME_CHARS = '<>:"|?*\\'
MAX_COMPONENT = 100

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

CONTENT_TYPE_EXTENSIONS = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/x-javascript": ".js",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/xml": ".xml",
    "application/xml": ".xml",
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

Resolver = Callable[[str], Iterable[str]]


class UrlError(ValueError):
    """Raised when a URL cannot be normalized."""


class UnsafePathError(ValueError):
    """Raised when a filename would escape its output folder."""


def strip_control_chars(text: str) -> str:
    return CONTROL_RE.sub("", text)


def has_control_chars(text: str) -> bool:
    return bool(CONTROL_RE.search(text))


def normalize_url(raw: str) -> str:
    """Normalize a base URL to scheme://host[:port] (path/query/fragment dropped)."""
    text = (raw or "").strip()
    if not text:
        raise UrlError("URL is empty.")

    if not SCHEME_RE.match(text):
        text = "https://" + text

    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise UrlError(f"Invalid URL: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlError(f"Unsupported scheme '{parsed.scheme}'. Use http:// or https://.")

    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise UrlError("URL has no hostname.")
    if host.startswith("["):
        host = host.strip("[]")
    if not HOST_RE.match(host) and ":" not in host:
        raise UrlError(f"Invalid hostname '{host}'.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise UrlError(f"Invalid port: {exc}") from exc

    if ":" in host:
        netloc = f"[{host}]"
    else:
        netloc = host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"

    return urlunsplit((scheme, netloc, "", "", ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def effective_port(url: str) -> int | None:
    try:
        parsed = urlsplit(url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def same_host(url_a: str, url_b: str) -> bool:
    return host_of(url_a) == host_of(url_b) and effective_port(url_a) == effective_port(url_b)


def build_url(base_url: str, path: str) -> str:
    """Join a base URL and a path without allowing the path to change the host."""
    if SCHEME_RE.match(path):
        return path
    base = base_url.rstrip("/") + "/"
    relative = path.lstrip("/")
    # urljoin treats "javascript:x" / "data:x" as absolute URLs; keep them on the base host.
    if BARE_SCHEME_RE.match(relative):
        relative = "./" + relative
    from urllib.parse import urljoin

    return urljoin(base, relative)


def encode_path(path: str) -> str:
    """Percent-encode spaces and other unsafe characters while keeping URL syntax."""
    return quote(path, safe="/?=&%:+,;@!$'()*[]~#-._")


def resolve_host(host: str, port: int | None = None) -> list[str]:
    """Resolve a hostname to a de-duplicated list of IP strings."""
    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise OSError(f"DNS lookup failed for '{host}': {exc}") from exc
    addresses: list[str] = []
    for info in infos:
        ip = str(info[4][0]).split("%")[0]
        if ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise OSError(f"No addresses found for '{host}'.")
    return addresses


def ip_is_blocked(value: str) -> bool:
    """True when an IP is loopback/private/link-local/reserved/non-global."""
    try:
        ip = ipaddress.ip_address(str(value).split("%")[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast or ip.is_unspecified


def safe_join(root: Path, *parts: str) -> Path:
    """Join parts to root and refuse anything that resolves outside root."""
    root_path = Path(root).resolve()
    target = root_path.joinpath(*parts).resolve()
    try:
        target.relative_to(root_path)
    except ValueError as exc:
        raise UnsafePathError(f"Refusing path outside '{root_path}': {target}") from exc
    return target


def sanitize_component(name: str) -> str:
    """Make one path component safe on Windows, macOS and Linux."""
    text = strip_control_chars(str(name))
    for ch in INVALID_FILENAME_CHARS:
        text = text.replace(ch, "_")
    text = text.replace("/", "_")
    text = text.rstrip(" .")
    if text in ("", ".", ".."):
        return "_"

    stem = text.split(".")[0].upper()
    if stem in WINDOWS_RESERVED:
        text = "_" + text

    if len(text) > MAX_COMPONENT:
        dot = text.rfind(".")
        if 0 < dot and len(text) - dot <= 16:
            text = text[: MAX_COMPONENT - (len(text) - dot)] + text[dot:]
        else:
            text = text[:MAX_COMPONENT]
    return text or "_"


def has_extension(name: str) -> bool:
    """Heuristic: does this filename look like it already has an extension?"""
    dot = name.rfind(".")
    return dot >= 0 and 0 < len(name) - dot - 1 <= 12


def ext_from_content_type(content_type: str | None) -> str:
    if not content_type:
        return ".bin"
    base = content_type.split(";", 1)[0].strip().lower()
    if not base:
        return ".bin"
    if base in CONTENT_TYPE_EXTENSIONS:
        return CONTENT_TYPE_EXTENSIONS[base]
    if base.endswith("+json"):
        return ".json"
    if base.endswith("+xml"):
        return ".xml"
    if base.startswith("text/"):
        return ".txt"
    if base.startswith("image/"):
        return ".bin"
    return ".bin"


def hash_suffix(value: str, length: int = 6) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:length]


def random_hex(length: int = 6) -> str:
    return secrets.token_hex((length + 1) // 2)[:length]


def human_bytes(size: float | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After as seconds, supporting both delay and HTTP-date forms."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def setup_logging(run_log: Path, audit_log: Path | None = None) -> logging.Logger:
    """Configure DEBUG logging to the run log and INFO audit entries to the host log."""
    logger = logging.getLogger("scrapit")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()

    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    try:
        Path(run_log).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(run_log, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass

    audit = logging.getLogger("scrapit.audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    for handler in list(audit.handlers):
        audit.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()
    if audit_log is not None:
        try:
            Path(audit_log).parent.mkdir(parents=True, exist_ok=True)
            audit_handler = logging.FileHandler(audit_log, encoding="utf-8")
            audit_handler.setLevel(logging.INFO)
            audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            audit.addHandler(audit_handler)
        except OSError:
            pass
    return logger
