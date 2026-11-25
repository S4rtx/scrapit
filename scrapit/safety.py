"""Safety controls: scope confirmation, IP checks and robots.txt rules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .utils import Resolver, host_of, ip_is_blocked, resolve_host


class SafetyError(Exception):
    """Raised when a target or redirect is outside the allowed scope."""


def check_url_allowed(
    url: str,
    allow_private: bool = False,
    resolver: Resolver | None = None,
) -> list[str]:
    """Resolve a URL's host and refuse non-public addresses unless explicitly allowed."""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SafetyError(f"Invalid URL '{url}': {exc}") from exc

    if parsed.scheme not in ("http", "https"):
        raise SafetyError(f"Unsupported scheme in '{url}'. Only http and https are allowed.")

    host = parsed.hostname
    if not host:
        raise SafetyError(f"URL has no hostname: {url}")

    resolve = resolver or resolve_host
    try:
        addresses = list(resolve(host))
    except OSError as exc:
        raise SafetyError(str(exc)) from exc
    except Exception as exc:  # resolver failure should never crash the tool
        raise SafetyError(f"Could not resolve '{host}': {exc}") from exc

    if not addresses:
        raise SafetyError(f"Could not resolve '{host}'.")

    if not allow_private:
        blocked = [ip for ip in addresses if ip_is_blocked(ip)]
        if blocked:
            raise SafetyError(
                f"Refusing to contact '{host}': it resolves to the non-public address "
                f"{blocked[0]}. Enable 'Allow localhost/private targets' only when you are "
                "testing your own local server."
            )
    return addresses


def primary_ip(url: str, resolver: Resolver | None = None) -> str:
    addresses = check_url_allowed(url, allow_private=True, resolver=resolver)
    return addresses[0]


def host_confirmation_matches(typed: str, target: str) -> bool:
    """The user must type the target hostname exactly (case-insensitive)."""
    expected = host_of(target) if "//" in target else target.strip().lower()
    return (typed or "").strip().lower() == expected


def scope_message(target: str, request_count: int, ip: str) -> str:
    host = host_of(target) if "//" in target else target
    return (
        f"You are about to send {request_count} request(s) to {host} ({ip}).\n"
        "Only continue if you own this site or have explicit written permission to test it.\n"
        f"Type the target hostname to confirm: {host}"
    )


def estimate_requests(path_count: int, respect_robots: bool) -> int:
    """Paths plus robots.txt plus one soft-404 probe request."""
    return path_count + (1 if respect_robots else 0) + 1


@dataclass
class RobotsRules:
    parser: RobotFileParser | None = None
    deny_all: bool = False
    fetched: bool = False
    note: str = ""

    @classmethod
    def allow_all(cls, note: str = "robots.txt not checked") -> RobotsRules:
        return cls(parser=None, deny_all=False, fetched=False, note=note)

    def allowed(self, url: str, user_agent: str) -> bool:
        if self.deny_all:
            return False
        if self.parser is None:
            return True
        try:
            return self.parser.can_fetch(user_agent, url)
        except Exception:  # noqa: BLE001 - a malformed robots.txt must not crash a run
            return True


def parse_robots(status: int | None, text: str, reason: str = "") -> RobotsRules:
    """Build rules from a robots.txt response. 401/403 is treated as 'deny all'."""
    if status == 200:
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        note = "robots.txt loaded"
        if reason:
            note += f" ({reason})"
        return RobotsRules(parser=parser, deny_all=False, fetched=True, note=note)
    if status in (401, 403):
        return RobotsRules(
            parser=None,
            deny_all=True,
            fetched=True,
            note=f"robots.txt returned {status}; treating all paths as disallowed",
        )
    if status is None:
        return RobotsRules.allow_all(f"robots.txt unavailable{': ' + reason if reason else ''}; allowing all")
    return RobotsRules.allow_all(f"robots.txt returned {status}; allowing all")


def load_robots(
    fetch: Callable[[str], tuple[int | None, bytes, str]],
    base_url: str,
) -> RobotsRules:
    """Fetch and parse robots.txt using a supplied fetch callable."""
    url = base_url.rstrip("/") + "/robots.txt"
    try:
        status, body, reason = fetch(url)
    except SafetyError as exc:
        return RobotsRules.allow_all(f"robots.txt blocked: {exc}")
    except Exception as exc:  # noqa: BLE001 - any fetch failure must fall back to allow-all
        return RobotsRules.allow_all(f"robots.txt unavailable: {exc}")
    text = body.decode("utf-8", "replace") if body else ""
    return parse_robots(status, text, reason)
