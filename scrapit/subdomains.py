"""Subdomain enumeration: passive certificate-transparency sources plus DNS brute force.

Coverage is honest: no technique finds *every* subdomain of a domain. This module
combines the strongest free passive sources (CT logs, passive DNS) with an optional
brute-force sweep and wildcard detection. Names that never appear in a public log and
are not in the wordlist cannot be discovered by any tool.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .utils import random_hex

Resolver = Callable[[str], Iterable[str]]

# Commonly used second-level labels so the registrable domain is detected without the full PSL.
MULTI_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au", "asn.au",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz", "school.nz",
    "co.za", "org.za", "net.za", "gov.za", "ac.za", "web.za",
    "co.in", "net.in", "org.in", "firm.in", "gen.in", "ind.in", "ac.in", "edu.in", "gov.in", "res.in",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "ad.jp", "ed.jp", "go.jp", "gr.jp", "lg.jp",
    "com.br", "net.br", "org.br", "gov.br", "edu.br", "art.br", "blog.br", "eco.br",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.mx", "org.mx", "net.mx", "edu.mx", "gob.mx",
    "com.ar", "net.ar", "org.ar", "gov.ar", "edu.ar",
    "com.tr", "net.tr", "org.tr", "gov.tr", "edu.tr", "gen.tr", "web.tr",
    "com.sg", "net.sg", "org.sg", "gov.sg", "edu.sg", "per.sg",
    "com.hk", "net.hk", "org.hk", "gov.hk", "edu.hk", "idv.hk",
    "com.tw", "net.tw", "org.tw", "gov.tw", "edu.tw", "idv.tw",
    "com.my", "net.my", "org.my", "gov.my", "edu.my",
    "co.id", "net.id", "or.id", "web.id", "ac.id", "sch.id", "go.id", "mil.id",
    "co.kr", "ne.kr", "or.kr", "re.kr", "pe.kr", "go.kr", "ac.kr",
    "com.pk", "net.pk", "org.pk", "gov.pk", "edu.pk",
    "com.ng", "net.ng", "org.ng", "gov.ng", "edu.ng",
    "com.eg", "net.eg", "org.eg", "gov.eg", "edu.eg",
    "co.il", "net.il", "org.il", "ac.il", "gov.il", "muni.il",
    "com.sa", "net.sa", "org.sa", "gov.sa", "edu.sa", "med.sa",
    "com.ua", "net.ua", "org.ua", "gov.ua", "edu.ua", "in.ua",
    "com.pl", "net.pl", "org.pl", "gov.pl", "edu.pl", "info.pl",
    "co.ke", "or.ke", "ne.ke", "go.ke", "ac.ke", "sc.ke",
    "com.ph", "net.ph", "org.ph", "gov.ph", "edu.ph",
    "com.vn", "net.vn", "org.vn", "gov.vn", "edu.vn",
    "co.th", "in.th", "or.th", "ac.th", "go.th", "net.th",
    "com.bd", "net.bd", "org.bd", "gov.bd", "edu.bd",
    "com.np", "net.np", "org.np", "gov.np", "edu.np",
    "com.lk", "net.lk", "org.lk", "gov.lk", "edu.lk",
    "com.pk", "com.af", "com.kw", "com.qa", "com.bh", "com.om", "com.jo", "com.lb",
    "co.at", "or.at", "ac.at", "gv.at", "co.hu", "co.ro", "co.bg", "co.rs", "co.hr",
    "com.es", "nom.es", "org.es", "gob.es", "edu.es",
    "com.pt", "org.pt", "gov.pt", "edu.pt",
    "com.gr", "net.gr", "org.gr", "gov.gr", "edu.gr",
}

# A compact, high-yield wordlist for brute force. Not exhaustive by design.
DEFAULT_WORDLIST: tuple[str, ...] = (
    "www", "mail", "webmail", "smtp", "pop", "pop3", "imap", "mx", "email", "exchange",
    "autodiscover", "autoconfig", "owa", "lyncdiscover", "sip", "voip",
    "api", "api1", "api2", "api-dev", "api-staging", "api-test", "apis", "gateway",
    "app", "apps", "application", "webapp", "portal", "dashboard", "panel", "admin",
    "administrator", "adminpanel", "manage", "management", "manager", "console",
    "cpanel", "whm", "plesk", "webdisk", "webmin", "directadmin", "server", "server1",
    "dev", "develop", "developer", "development", "devel", "staging", "stage", "stg",
    "test", "testing", "test1", "test2", "qa", "uat", "preprod", "pre-prod", "sandbox",
    "demo", "beta", "alpha", "gamma", "preview", "review", "rc", "canary", "edge",
    "prod", "production", "live", "main", "primary", "secondary", "backup", "bak",
    "old", "new", "legacy", "archive", "archives", "v1", "v2", "v3", "version",
    "static", "assets", "asset", "cdn", "cdn1", "cdn2", "media", "images", "img",
    "video", "videos", "files", "file", "download", "downloads", "upload", "uploads",
    "docs", "doc", "documentation", "wiki", "help", "support", "helpdesk", "faq",
    "kb", "knowledgebase", "forum", "forums", "community", "blog", "blogs", "news",
    "shop", "store", "ecommerce", "cart", "checkout", "pay", "payment", "payments",
    "billing", "invoice", "invoices", "orders", "account", "accounts", "login", "sso",
    "auth", "oauth", "id", "identity", "idp", "passport", "secure", "vpn", "remote",
    "rdp", "citrix", "gatekeeper", "proxy", "firewall", "router", "switch", "ns",
    "ns1", "ns2", "ns3", "dns", "dns1", "dns2", "resolver", "monitor", "monitoring",
    "status", "health", "metrics", "grafana", "kibana", "prometheus", "zabbix",
    "nagios", "logs", "log", "logging", "analytics", "stats", "statistics", "report",
    "reports", "tracking", "click", "ads", "ad", "campaign", "marketing", "crm",
    "erp", "hr", "finance", "intranet", "extranet", "internal", "corp", "corporate",
    "office", "employees", "staff", "team", "meet", "chat", "conference", "calendar",
    "mailer", "newsletter", "lists", "list", "mailing", "crm2", "sales", "clients",
    "client", "partners", "partner", "suppliers", "vendor", "vendors",
    "git", "gitlab", "github", "bitbucket", "svn", "repo", "registry", "docker",
    "k8s", "kubernetes", "jenkins", "ci", "cd", "build", "builds", "deploy", "jira",
    "confluence", "sonar", "nexus", "artifactory", "harbor", "vault", "consul",
    "db", "database", "mysql", "postgres", "postgresql", "mongo", "mongodb", "redis",
    "elastic", "elasticsearch", "cache", "queue", "rabbitmq", "kafka", "search",
    "mobile", "m", "wap", "ios", "android", "app1", "app2", "game", "games", "play",
    "tv", "stream", "streaming", "radio", "music", "podcast",
    "learn", "lms", "edu", "training", "course", "courses", "academy", "school",
    "campus", "students", "portal2", "apply", "admission", "admissions", "exam",
    "exams", "result", "results", "quiz", "quizzes", "library", "research",
    "blog2", "news2", "press", "careers", "jobs", "job", "recruit", "recruitment",
    "events", "event", "tickets", "ticket", "support2", "service", "services",
    "site", "site1", "website", "web", "web1", "web2", "host", "hosting", "cloud",
    "aws", "azure", "gcp", "s3", "storage", "backups", "restore", "dr", "failover",
    "origin", "origin1", "proxy1", "lb", "loadbalancer", "haproxy", "nginx", "apache",
    "uat1", "sit", "sit1", "pilot", "trial", "free", "trial2", "subscribe", "member",
    "members", "user", "users", "customer", "customers", "profile", "profiles",
    "photo", "photos", "gallery", "gallery2", "album", "alumni", "donate", "donation",
    "feedback", "survey", "surveys", "form", "forms", "contact", "about", "info",
    "dir", "directory", "search2", "maps", "map", "geo", "location", "locations",
    "weather", "sms", "mms", "gateway2", "payments2", "wallet", "card", "cards",
    "loan", "loans", "bank", "banking", "insurance", "policy", "claim", "claims",
    "hospital", "medic", "clinic", "patient", "patients", "doctor", "pharmacy",
    "gov", "egov", "public", "citizen", "citizens", "tax", "taxes", "license",
    "portal3", "scheme", "scholarship", "scholarships", "notification", "notify",
    "push", "mqtt", "iot", "device", "devices", "sensor", "sensors", "edge2",
    "ipv6", "ipv4", "www2", "www3", "www-dev", "www-test", "www-staging",
    "web-test", "web-dev", "web-staging", "prod-api", "dev-api", "staging-api",
    "test-api", "internal-api", "external-api", "private-api", "public-api",
    "graphql", "rest", "soap", "ws", "websocket", "socket", "live2", "events2",
    "cron", "scheduler", "worker", "workers", "jobs2", "batch", "etl", "data",
    "dataset", "datasets", "open", "opendata", "api3", "service1", "service2",
)

_SAFE_LABEL = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")

USER_AGENT = "Scrapit/Subdomains (authorized security testing)"


class SubdomainError(Exception):
    """Raised when enumeration cannot start (bad domain, unusable wordlist...)."""


@dataclass
class SubdomainResult:
    name: str
    sources: set[str] = field(default_factory=set)
    ips: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ips": list(self.ips), "sources": sorted(self.sources)}


@dataclass
class SubdomainReport:
    domain: str
    results: list[SubdomainResult]
    wildcard: list[str]
    notes: list[str] = field(default_factory=list)
    source_counts: dict[str, int] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    bruteforced: int = 0
    bruteforce_skipped: bool = False

    @property
    def names(self) -> list[str]:
        return [r.name for r in self.results]


def registrable_domain(raw: str) -> str:
    """Reduce a host or URL to its registrable domain (best effort without the full PSL)."""
    text = (raw or "").strip().lower()
    if not text:
        raise SubdomainError("Domain is empty.")
    if "://" in text:
        from urllib.parse import urlsplit

        text = urlsplit(text).hostname or ""
    text = text.rstrip(".").split("/", 1)[0].split(":", 1)[0]
    if text.startswith("*."):
        text = text[2:]
    if ".." in text:
        raise SubdomainError(f"'{raw}' contains an empty label.")
    labels = [label for label in text.split(".") if label]
    if len(labels) < 2:
        raise SubdomainError(f"'{raw}' is not a domain with a public suffix.")
    for label in labels:
        if not _SAFE_LABEL.match(label.replace("_", "-")):
            raise SubdomainError(f"Invalid domain label '{label}'.")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _clean_name(value: str, domain: str) -> str | None:
    text = (value or "").strip().lower().strip(".")
    text = text.removeprefix("*.")
    text = text.replace("*.", "")
    if not text or "@" in text or " " in text:
        return None
    if not text.endswith("." + domain):
        return None
    labels = text.split(".")
    if not all(_SAFE_LABEL.match(label.replace("_", "-")) for label in labels):
        return None
    return text


def _safe_resolve(name: str, resolver: Resolver) -> list[str]:
    try:
        return sorted({str(ip) for ip in resolver(name)})
    except OSError:
        return []
    except Exception:  # noqa: BLE001 - a resolver hiccup must not abort enumeration
        return []


# -- passive sources ------------------------------------------------------------


def fetch_crtsh(domain: str, client: httpx.Client) -> set[str]:
    """Certificate transparency via crt.sh (JSON). Often slow, so retry once."""
    last_error: Exception | None = None
    data: object = None
    for attempt in range(2):
        try:
            response = client.get(
                "https://crt.sh/",
                params={"q": domain, "output": "json"},
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()
            break
        except Exception as exc:  # noqa: BLE001 - retry once, then surface the failure
            last_error = exc
            if attempt == 0:
                time.sleep(3)
    if data is None:
        raise last_error or RuntimeError("crt.sh returned no data")
    names: set[str] = set()
    if not isinstance(data, list):
        return names
    for entry in data:
        if not isinstance(entry, dict):
            continue
        for key in ("name_value", "common_name"):
            value = entry.get(key)
            if not isinstance(value, str):
                continue
            for line in value.splitlines():
                cleaned = _clean_name(line, domain)
                if cleaned:
                    names.add(cleaned)
    return names


def fetch_certspotter(domain: str, client: httpx.Client) -> set[str]:
    """Certificate transparency via the Cert Spotter API."""
    response = client.get(
        "https://api.certspotter.com/v1/issuances",
        params={"domain": domain, "include_subdomains": "true", "expand": "dns_names"},
    )
    response.raise_for_status()
    data = response.json()
    names: set[str] = set()
    if not isinstance(data, list):
        return names
    for entry in data:
        if not isinstance(entry, dict):
            continue
        for value in entry.get("dns_names") or []:
            if isinstance(value, str):
                cleaned = _clean_name(value, domain)
                if cleaned:
                    names.add(cleaned)
    return names


def fetch_hackertarget(domain: str, client: httpx.Client) -> set[str]:
    """Passive DNS via HackerTarget's hostsearch (small anonymous quota)."""
    response = client.get("https://api.hackertarget.com/hostsearch/", params={"q": domain})
    response.raise_for_status()
    text = response.text
    if "error" in text.lower() and "API count exceeded" in text:
        return set()
    names: set[str] = set()
    for line in text.splitlines():
        host = line.split(",", 1)[0]
        cleaned = _clean_name(host, domain)
        if cleaned:
            names.add(cleaned)
    return names


def fetch_rapiddns(domain: str, client: httpx.Client) -> set[str]:
    """Passive DNS via rapiddns.io (HTML table)."""
    response = client.get("https://rapiddns.io/subdomain/" + domain, params={"full": "1"})
    response.raise_for_status()
    pattern = re.compile(
        r"<td>\s*([a-z0-9._\-]+\.%s)\s*</td>" % re.escape(domain),
        re.IGNORECASE,
    )
    names: set[str] = set()
    for match in pattern.findall(response.text):
        cleaned = _clean_name(match, domain)
        if cleaned:
            names.add(cleaned)
    return names


PASSIVE_SOURCES: dict[str, Callable[[str, httpx.Client], set[str]]] = {
    "crtsh": fetch_crtsh,
    "certspotter": fetch_certspotter,
    "hackertarget": fetch_hackertarget,
    "rapiddns": fetch_rapiddns,
}


def passive_lookup(
    domain: str,
    client: httpx.Client,
    sources: Iterable[str] | None = None,
    on_source: Callable[[str, int, str | None], None] | None = None,
    pause: float = 0.3,
) -> dict[str, set[str]]:
    """Query each passive source and return {name: {sources}}. Errors are reported, not raised."""
    chosen = list(sources) if sources else list(PASSIVE_SOURCES)
    found: dict[str, set[str]] = {}
    for index, source in enumerate(chosen):
        func = PASSIVE_SOURCES.get(source)
        if func is None:
            continue
        error: str | None = None
        names: set[str] = set()
        try:
            names = func(domain, client)
        except Exception as exc:  # noqa: BLE001 - one failing source must not stop the others
            error = f"{type(exc).__name__}: {exc}"
        for name in names:
            found.setdefault(name, set()).add(source)
        if on_source is not None:
            try:
                on_source(source, len(names), error)
            except Exception:  # noqa: BLE001 - UI callback is cosmetic
                pass
        if index < len(chosen) - 1 and pause:
            time.sleep(pause)
    return found


# -- brute force ----------------------------------------------------------------


def wildcard_ips(domain: str, resolver: Resolver, samples: int = 4) -> list[str]:
    """Detect wildcard DNS by resolving random labels under the domain."""
    ips: set[str] = set()
    for _ in range(samples):
        ips.update(_safe_resolve(f"{random_hex(10)}.{domain}", resolver))
    return sorted(ips)


def wildcard_is_rotating(domain: str, resolver: Resolver, samples: int = 3) -> bool:
    """True when random labels resolve to *different* addresses on each query.

    Rotating wildcard DNS (common on shared hosts) makes IP comparison useless as a
    filter: a real name and a nonexistent one both answer with addresses drawn from
    the same shifting pool.
    """
    answer_sets: list[frozenset[str]] = []
    for _ in range(samples):
        ips = _safe_resolve(f"{random_hex(12)}.{domain}", resolver)
        if not ips:
            return False
        answer_sets.append(frozenset(ips))
    return len(set(answer_sets)) > 1


def brute_force(
    domain: str,
    words: Iterable[str],
    resolver: Resolver,
    workers: int = 20,
    wildcard: Iterable[str] | None = None,
    on_progress: Callable[[str, bool, int, int], None] | None = None,
) -> dict[str, list[str]]:
    """Resolve word.domain for every word. Wildcard-only answers are discarded."""
    wildcard_set = {ip for ip in (wildcard or [])}
    cleaned_words: list[str] = []
    seen: set[str] = set()
    for word in words:
        label = (word or "").strip().lower()
        if not label or label.startswith("#") or label in seen:
            continue
        if not _SAFE_LABEL.match(label.replace("_", "-")):
            continue
        seen.add(label)
        cleaned_words.append(label)

    total = len(cleaned_words)
    found: dict[str, list[str]] = {}
    done = 0
    if not cleaned_words:
        return found

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 64))) as pool:
        futures = {
            pool.submit(_safe_resolve, f"{label}.{domain}", resolver): label
            for label in cleaned_words
        }
        for future in as_completed(futures):
            label = futures[future]
            ips = future.result()
            done += 1
            is_real = bool(ips) and (not wildcard_set or any(ip not in wildcard_set for ip in ips))
            if is_real:
                found[f"{label}.{domain}"] = ips
            if on_progress is not None:
                try:
                    on_progress(f"{label}.{domain}", is_real, done, total)
                except Exception:  # noqa: BLE001 - UI callback is cosmetic
                    pass
    return found


def load_wordlist(path: str | Path | None) -> list[str]:
    """Read a wordlist file, or return the built-in list when no path is given."""
    if path is None:
        return list(DEFAULT_WORDLIST)
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise SubdomainError(f"Wordlist not found: {file_path}")
    try:
        text = file_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise SubdomainError(f"Cannot read '{file_path}': {exc}") from exc
    words = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not words:
        raise SubdomainError(f"Wordlist is empty: {file_path}")
    return words


# -- orchestration --------------------------------------------------------------


def enumerate_subdomains(
    domain: str,
    *,
    client: httpx.Client | None = None,
    resolver: Resolver | None = None,
    bruteforce: bool = True,
    words: Iterable[str] | None = None,
    workers: int = 20,
    sources: Iterable[str] | None = None,
    on_source: Callable[[str, int, str | None], None] | None = None,
    on_progress: Callable[[str, bool, int, int], None] | None = None,
    max_results: int = 5000,
) -> SubdomainReport:
    """Combine passive sources and brute force into one de-duplicated report."""
    from .utils import resolve_host

    base = registrable_domain(domain)
    resolve = resolver or resolve_host
    owns_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9"},
        )

    started_at = _now()
    started = time.perf_counter()
    notes: list[str] = []
    source_counts: dict[str, int] = {}
    results: dict[str, SubdomainResult] = {}
    bruteforced = 0
    brute_forced_skipped = False
    wildcard: list[str] = []

    def add(name: str, source: str) -> None:
        entry = results.get(name)
        if entry is None:
            if len(results) >= max_results:
                return
            entry = SubdomainResult(name=name)
            results[name] = entry
        entry.sources.add(source)

    try:
        passive = passive_lookup(base, client, sources=sources, on_source=on_source)
        for name, from_sources in passive.items():
            for source in from_sources:
                add(name, source)
                source_counts[source] = source_counts.get(source, 0) + 1

        if bruteforce:
            wildcard = wildcard_ips(base, resolve)
            if wildcard and wildcard_is_rotating(base, resolve):
                # A rotating wildcard makes DNS comparison meaningless: every guess
                # answers with a fresh address. Brute force would be pure noise.
                notes.append(
                    f"Wildcard DNS detected (*.{base}) and its answers change between queries "
                    f"(seen: {', '.join(wildcard[:3])}...). Brute force was skipped because every "
                    "name would resolve and the results would be false positives."
                )
                brute_forced_skipped = True
            else:
                if wildcard:
                    notes.append(
                        f"Wildcard DNS detected (*.{base} -> {', '.join(wildcard)}); "
                        "brute-force hits pointing only at those addresses were discarded."
                    )
                hits = brute_force(
                    base,
                    words if words is not None else DEFAULT_WORDLIST,
                    resolve,
                    workers=workers,
                    wildcard=wildcard,
                    on_progress=on_progress,
                )
                bruteforced = len(hits)
                for name, ips in hits.items():
                    add(name, "bruteforce")
                    results[name].ips = ips
                source_counts["bruteforce"] = bruteforced
    finally:
        if owns_client:
            client.close()

    # Resolve anything still missing an address (passive hits have no IP yet).
    for entry in results.values():
        if not entry.ips:
            entry.ips = _safe_resolve(entry.name, resolve)

    ordered = sorted(results.values(), key=lambda r: (r.name.count("."), r.name))
    report = SubdomainReport(
        domain=base,
        results=ordered,
        wildcard=wildcard,
        notes=notes,
        source_counts=source_counts,
        started_at=started_at,
        finished_at=_now(),
        duration_s=round(time.perf_counter() - started, 3),
        bruteforced=bruteforced,
        bruteforce_skipped=brute_forced_skipped,
    )
    if not ordered:
        notes.append("No subdomains found. Try a different wordlist or check the domain spelling.")
    return report


def _now() -> str:
    from .utils import now_iso

    return now_iso()


# -- output ---------------------------------------------------------------------


def results_to_text(report: SubdomainReport) -> str:
    return "\n".join(report.names) + ("\n" if report.names else "")


def results_to_csv(report: SubdomainReport) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["subdomain", "ips", "sources"])
    for entry in report.results:
        writer.writerow([entry.name, ";".join(entry.ips), ";".join(sorted(entry.sources))])
    return buffer.getvalue()


def results_to_json(report: SubdomainReport) -> str:
    payload = {
        "domain": report.domain,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "duration_s": report.duration_s,
        "count": len(report.results),
        "bruteforced": report.bruteforced,
        "bruteforce_skipped": report.bruteforce_skipped,
        "wildcard": report.wildcard,
        "source_counts": report.source_counts,
        "notes": report.notes,
        "subdomains": [entry.to_dict() for entry in report.results],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def write_outputs(report: SubdomainReport, directory: str | Path) -> dict[str, Path]:
    """Write subdomains.txt/csv/json, plus a stable subdomains.txt beside the run folder."""
    target = Path(directory).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    paths = {
        "txt": target / "subdomains.txt",
        "csv": target / "subdomains.csv",
        "json": target / "subdomains.json",
    }
    paths["txt"].write_text(results_to_text(report), encoding="utf-8")
    paths["csv"].write_text(results_to_csv(report), encoding="utf-8", newline="")
    paths["json"].write_text(results_to_json(report), encoding="utf-8")

    stable = target.parent / "subdomains.txt"
    try:
        if stable.resolve() != paths["txt"].resolve():
            stable.write_text(results_to_text(report), encoding="utf-8")
            paths["stable"] = stable
    except OSError:
        pass
    return paths
