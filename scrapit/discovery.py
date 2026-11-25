"""Discover request targets automatically: same-host links and sitemaps, no paths file."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .utils import same_host

ASSET_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff",
    ".css", ".js", ".mjs", ".map", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".webm", ".mp3", ".wav", ".ogg", ".avi", ".mov", ".m4a",
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z",
}

PAGE_EXTENSIONS = {
    ".html", ".htm", ".xhtml", ".php", ".phtml", ".asp", ".aspx", ".jsp", ".jspx",
    ".txt", ".xml", ".json", ".md", ".csv",
}

_SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


class LinkParser(HTMLParser):
    """Collect navigable references from an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: (value or "") for key, value in attrs}
        if tag in ("a", "area") and values.get("href"):
            self.links.append(values["href"])
        elif tag == "form" and values.get("action"):
            self.links.append(values["action"])
        elif tag == "iframe" and values.get("src"):
            self.links.append(values["src"])


def looks_like_page(path: str) -> bool:
    """True when a path is worth crawling (not a stylesheet, image or other binary asset)."""
    name = urlsplit(path).path.rsplit("/", 1)[-1].lower()
    if not name:
        return True
    dot = name.rfind(".")
    if dot <= 0:
        return True
    return name[dot:] in PAGE_EXTENSIONS


def path_of(url: str) -> str:
    """Return the requestable path (with query) for a URL."""
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return path


def resolve_link(raw: str, page_url: str, base_url: str) -> str | None:
    """Resolve a link from a page to an absolute same-host http(s) URL, or None."""
    text = (raw or "").strip()
    if not text or text.lower().startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    try:
        absolute = urljoin(page_url, text).split("#", 1)[0]
        parsed = urlsplit(absolute)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not same_host(absolute, base_url):
        return None
    return absolute


def extract_links(html: bytes, page_url: str, base_url: str, limit: int = 100) -> list[str]:
    """Return same-host page URLs linked from an HTML document."""
    parser = LinkParser()
    try:
        parser.feed(html.decode("utf-8", "replace"))
        parser.close()
    except (ValueError, AssertionError):
        return []

    found: list[str] = []
    seen: set[str] = set()
    for raw in parser.links:
        absolute = resolve_link(raw, page_url, base_url)
        if absolute is None or absolute in seen:
            continue
        if not looks_like_page(path_of(absolute)):
            continue
        seen.add(absolute)
        found.append(absolute)
        if len(found) >= limit:
            break
    return found


def sitemap_locations(text: str, page_url: str, base_url: str, limit: int = 500) -> list[str]:
    """Return same-host URLs listed in a sitemap document."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in _SITEMAP_LOC_RE.findall(text):
        absolute = resolve_link(raw, page_url, base_url)
        if absolute is None or absolute in seen:
            continue
        seen.add(absolute)
        found.append(absolute)
        if len(found) >= limit:
            break
    return found


def robots_sitemaps(text: str, base_url: str, limit: int = 10) -> list[str]:
    """Return sitemap URLs announced in a robots.txt file."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in _ROBOTS_SITEMAP_RE.findall(text or ""):
        absolute = resolve_link(raw, base_url.rstrip("/") + "/robots.txt", base_url)
        if absolute is None or absolute in seen:
            continue
        seen.add(absolute)
        found.append(absolute)
        if len(found) >= limit:
            break
    return found
