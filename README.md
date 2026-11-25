# scrapit

[![tests](https://github.com/S4rtx/scrapit/actions/workflows/tests.yml/badge.svg)](https://github.com/S4rtx/scrapit/actions/workflows/tests.yml)

**A polite, terminal-first website path fetcher for authorized security testing.**

Built and maintained by [@S4rtx](https://github.com/S4rtx).

Scrapit requests a list of paths against one target, records what exists, downloads the content it
finds, and writes JSON / CSV / Markdown reports. It runs as an interactive TUI by default and as a
flagged CLI for repeatable runs.

> Use scrapit only on sites you own or have explicit written permission to test.

**Contents:** [Features](#features) · [Install](#install) · [Quick start](#quick-start) ·
[TUI](#interactive-tui) · [CLI](#command-line-reference) · [Paths file](#paths-file-format) ·
[Output](#output-layout) · [Reports](#reports) · [Safety](#safety-model) ·
[Architecture](#architecture) · [Updates](#updates) · [Testing](#testing) ·
[Demo](#local-demo-own-server) · [Responsible use](#responsible-use)

---

## Features

- **Sequential and polite** — one GET at a time, configurable delay, no concurrency
- **Two ways to scan** — a paths file, or **auto-scrape** mode that crawls a site from its home page
  and sitemaps by following same-host links (no paths file needed)
- **Scope-aware** — typed-hostname confirmation, private-IP refusal, per-redirect IP re-checks
- **robots.txt aware** — fetched once, disallowed paths marked `BLOCKED_BY_ROBOTS`
- **Useful discovery** — soft-404 fingerprinting, redirect chains, retries with backoff
- **Safe downloads** — path-traversal protection, reserved-name handling, size limits, optional headers
- **Reports** — `results.json`, `results.csv`, `report.md`, and an optional ZIP
- **Audit trail** — every request logged to `output/<host>/scrapit.log`
- **Two interfaces** — arrow-key TUI plus scriptable flags
- **Self-updating** — on launch it notifies you when a newer GitHub release exists and offers to update; `--check-updates` / `--update` do the same from the CLI
- **Named runs** — optionally name each run so its output folder is easy to find; every run folder
  and ZIP is prefixed with the author handle (`S4rtx_…`) and reports record `@S4rtx` as the author
- **No bypass features** — no auth bypass, WAF evasion, UA spoofing, proxying or form submission

## Requirements

- Python **3.10+** (tested on 3.14)
- Dependencies pinned in [`pyproject.toml`](pyproject.toml) and [`requirements.txt`](requirements.txt):
  `httpx`, `rich`, `questionary`, `pytest` (tests)

## Install

### Linux / WSL / Kali

```bash
git clone https://github.com/S4rtx/scrapit.git
cd scrapit
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
scrapit
```

### macOS

```bash
git clone https://github.com/S4rtx/scrapit.git
cd scrapit
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
scrapit
```

### Windows PowerShell

```powershell
git clone https://github.com/S4rtx/scrapit.git
cd scrapit
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
scrapit
```

If PowerShell blocks activation, run once in that shell:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Prefer requirements only? `pip install -r requirements.txt` works too, then run
`python -m scrapit` from the project folder.

`questionary` is optional at runtime: if it is missing or stdin is not a TTY, the menu falls back to
a numbered `input()` interface and plain text output.

## Quick start

Interactive:

```bash
scrapit
```

Non-interactive (only on an authorized target):

```bash
scrapit --target https://example.com \
        --paths sample_paths.txt \
        --delay 1 \
        --yes \
        --i-have-permission
```

`--yes` skips interactive confirmations. It does **not** replace `--i-have-permission`, which must be
passed explicitly for non-interactive runs.

Auto-scrape (no paths file — crawl the site instead):

```bash
scrapit --target https://example.com --discover --depth 3 --yes --i-have-permission
```

## Interactive TUI

```
 ┌─ SCRAPIT ──────────────────────────────────┐
 │ Target : https://example.com (93.184...)   │
 │ Paths  : paths.txt (12 loaded)             │
 │ Output : output                            │
 │ Mode   : paths file                        │
 └─ by @S4rtx - polite path fetcher ──────────┘
 1. Set target URL
 2. Load paths file (.txt)
 3. Auto-scrape this site (no paths file)
 4. Settings
 5. Preview URLs that will be requested
 6. Start fetching
 7. View last results
 8. Export report / ZIP the downloads
 9. Help / About
 10. Check for updates
 11. Exit
```

What each option does:

| Option | Action |
| --- | --- |
| Set target URL | Normalizes `example.com` to `https://example.com`, drops path/query, resolves the IP |
| Load paths file | Parses/cleans paths, reports duplicates/skips, previews the first 10 |
| Auto-scrape | One step: asks for the target, then crawls it (links + sitemaps) without a paths file |
| Settings | Delay, timeout, retries, redirects, size cap, robots, UA, headers, TLS, output, assets, private targets, path cap, discovery mode, crawl depth, sitemaps |
| Preview URLs | Table of every final URL and the planned request count (discovery mode shows starting points) |
| Start fetching | Scope confirmation, an optional run name for the output folder, then the sequential fetch with live progress |
| View last results | Rich table loaded from the latest `results.json` |
| Export | Regenerates reports and creates a ZIP of the run folder |
| Help | Version, author, paths-file format and status-color legend |
| Check for updates | Compares with the newest GitHub release/tag and can `git pull --ff-only` |
| Exit | Confirmation, then clean exit |

### Auto-scrape mode

Instead of supplying a paths file, scrapit can discover paths itself:

1. It requests the start path (`/` by default) plus `robots.txt`.
2. It reads any `Sitemap:` entries from `robots.txt` and `/sitemap.xml`, and queues those URLs.
3. For every HTML page it downloads, it extracts same-host `<a>`/`<area>`/`<form>`/`<iframe>` targets
   and queues them breadth-first.
4. It keeps going until the crawl depth, the path cap, or the queue runs out.

Auto-scrape still respects every safety control: the typed-hostname confirmation, the private-IP
policy, redirect re-checks, the request cap and robots.txt (disallowed links are never queued).
Non-HTML assets, off-host links and non-http(s) schemes are ignored.

From the CLI:

```bash
scrapit --target https://example.com --discover --depth 3 --yes --i-have-permission
```

`--depth` controls how many link hops to follow (`0` = only the start page), and `--no-sitemap`
stops it seeding the crawl from sitemaps. `--paths` still works alongside `--discover`: the listed
paths become extra starting points.

Ctrl+C during a run stops it cleanly, keeps partial results, writes the reports, and returns to the
menu. Ctrl+C at the menu exits gracefully.

## Command-line reference

```text
scrapit [--target URL] [--paths FILE] [flags]
```

| Flag | Meaning |
| --- | --- |
| `--target URL` | Base URL; path/query are dropped |
| `--paths FILE` | Paths file (one path per line) |
| `--delay SECONDS` | Delay between requests (default `1.0`, min `0.2`) |
| `--timeout SECONDS` | Per-request timeout (default `10`) |
| `--retries N` | Retries for timeouts / 429 / 502 / 503 / 504 (default `2`) |
| `--max-size-mb N` | Max download per file (default `10`) |
| `--output DIR` | Output folder (default `./output`) |
| `--user-agent STR` | Custom (honest) User-Agent |
| `--no-robots` | Skip robots.txt checks |
| `--no-redirects` | Do not follow redirects |
| `--no-verify` | Disable TLS verification (unsafe; own test servers only) |
| `--allow-private` | Allow localhost/private targets (own test servers only) |
| `--assets` | Download same-host JS/CSS/images linked from HTML |
| `--no-save-headers` | Do not write `headers/*.headers.txt` |
| `--max-paths N` | Request cap per run (default `500`) |
| `--zip` | ZIP the run folder at the end |
| `--name NAME` | Name for this run, used as a prefix on the output folder |
| `--discover` | Auto mode: crawl from the target instead of using a paths file |
| `--depth N` | Link-following depth in discovery mode (default `3`) |
| `--no-sitemap` | Do not seed the crawl from sitemaps |
| `--yes` | Skip interactive confirmations (not the permission flag) |
| `--i-have-permission` | Explicit authorization confirmation (required non-interactively) |
| `--debug` | Show a traceback for unexpected errors |
| `--check-updates` | Compare with the newest GitHub release/tag, then exit |
| `--update` | Check for a newer release and update this git checkout (`git pull --ff-only`) |
| `--version` | Print the version |

Passing `--target` and `--paths` with `--yes` but without `--i-have-permission` exits with code `2`.
Passing target/paths without either flag opens the interactive menu pre-filled and still asks you to
type the hostname.

## Paths file format

One path per line. Example [`sample_paths.txt`](sample_paths.txt):

```text
/admin
/js
user
robots.txt
/assets/app.js
# lines starting with # are ignored
```

Parsing rules:

1. UTF-8 (BOM tolerated), Windows CRLF tolerated.
2. Whitespace trimmed; blank lines and `#` comments skipped.
3. A leading `/` is added when missing (`robots.txt` → `/robots.txt`).
4. Absolute URLs are accepted only when host and port match the target; others are skipped with a warning.
5. Exact duplicates are removed; original order is preserved.
6. Paths with control characters are rejected; spaces are percent-encoded.
7. A missing/empty file, or a file with zero valid lines, shows a friendly error and returns to the menu.

## Output layout

```text
output/
└── example.com/
    ├── scrapit.log                            # append-only audit log (every request)
    └── S4rtx_2026-11-25_14-30-05/             # or S4rtx_<name>_2026-11-25_14-30-05/ with --name
        ├── files/
        │   ├── admin/index.html
        │   ├── js/index.html
        │   ├── assets/app.js
        │   └── robots.txt
        ├── headers/
        │   ├── admin.headers.txt
        │   ├── js.headers.txt
        │   └── assets/app.js.headers.txt
        ├── results.json
        ├── results.csv
        ├── report.md
        └── scrapit.log                        # detailed DEBUG log
```

- Every run folder is prefixed with the author name (`S4rtx`), and the ZIP archive uses the same name.
- Directory-like paths become `<path>/index.<ext>`; files keep their own extension.
- The extension comes from `Content-Type` when the path has none.
- Query strings add a short hash (`page_ab12cd.html`) so different URLs do not overwrite each other.
- Components are sanitized (`..`, `<>:"|?*\`, reserved Windows names, 100-char limit), and the final
  path is verified to stay inside the run folder.
- Bodies are written in binary exactly as received (after HTTP transfer decoding).

## Reports

| File | Contents |
| --- | --- |
| `results.json` | Full structured run: target, settings, counts, every result field |
| `results.csv` | `path,url,final_url,status,label,content_type,size_bytes,sha256,response_ms,saved_as,error` |
| `report.md` | Readable summary: settings, counts, found/protected/missing/error tables, robots and soft-404 notes |

A final panel shows the counts and output folder:

```text
Done in 14.2s
Found: 5   Redirects: 2   Protected: 1   Not found: 4   Errors: 0   Blocked by robots: 1
Saved to: output/example.com/S4rtx_2026-11-25_14-30-05/
```

## Safety model

Scrapit is deliberately conservative:

1. **Typed scope confirmation** before every interactive run — you must type the target hostname exactly.
2. **Explicit permission flag** (`--i-have-permission`) for non-interactive runs.
3. **Private-address refusal** — loopback, private, link-local, reserved and multicast addresses are
   blocked by default; enable *Allow localhost/private targets* only for your own test server.
4. **Redirect re-checks** — redirects are followed manually, and every hop is resolved and checked
   before a request is sent. A public site cannot redirect scrapit into an internal address.
5. **robots.txt** — fetched once, respected by default; disallowed paths are logged, not downloaded.
6. **Request cap** — 500 paths per run by default, with a warning above 200.
7. **Audit log** — timestamp, URL and status for every request.
8. **Honest User-Agent** — identifies the tool; no browser or search-engine impersonation.

It only sends plain GET requests. It does not implement authentication bypass, WAF evasion, header
spoofing, IP rotation, proxy chaining, credential brute force, or form submission.

## Architecture

```text
cli.py / menu.py
      │  target, paths, settings
      ▼
config.py  ── validated Config dataclass
      │
paths.py   ── load, clean, dedupe paths
      │
discovery.py ── links, sitemaps, crawl seeds (auto-scrape mode)
      │
safety.py  ── scope confirmation, DNS/IP policy, robots.txt rules
      │
fetcher.py ── sequential GETs, crawl queue, retries, redirects, soft-404, assets, live UI
      │            │
      │            ▼
      │        storage.py  ── safe file/header layout, ZIP
      ▼
report.py  ── results.json / results.csv / report.md + rich summary
```

| Module | Responsibility |
| --- | --- |
| `scrapit/cli.py` | Flags, config mapping, permission enforcement, entry point |
| `scrapit/menu.py` | Interactive menu, questionary + numbered fallback, settings |
| `scrapit/config.py` | Defaults and validation |
| `scrapit/paths.py` | Paths-file parsing and normalization |
| `scrapit/discovery.py` | Link/sitemap extraction and crawl seeds for auto-scrape mode |
| `scrapit/safety.py` | Scope confirmation, IP policy, robots rules |
| `scrapit/fetcher.py` | HTTP loop, retries, redirects, soft-404, assets, run orchestration |
| `scrapit/storage.py` | Run folder layout, sanitization, safe writes, ZIP |
| `scrapit/report.py` | JSON/CSV/Markdown generation and rich tables |
| `scrapit/update.py` | GitHub release check and `git pull --ff-only` self-update |
| `scrapit/utils.py` | URL normalization, IP checks, filename sanitizing, logging setup |

Redirects are handled manually (httpx auto-follow is off) so each hop is DNS-checked before the
request. The fetcher keeps a single `httpx.Client` for connection reuse and sends requests strictly
one at a time.

## Updates

`scrapit` checks itself against the newest GitHub release (falling back to tags). When it opens it
quietly checks for updates and, if a newer release exists, shows an update panel and asks whether to
update. The same check is available explicitly:

```bash
scrapit --check-updates   # report only
scrapit --update          # check, then git pull --ff-only
```

The same is available from the menu under **9. Check for updates**. Installations that are not a git
checkout are never modified; the command prints the `pip install --upgrade git+...` equivalent instead.
Update failures (local changes, diverged history, rate limits) are reported without touching the working
tree.

## Testing

```bash
pip install -e ".[dev]"
python -m pytest -q
```

The suite uses `httpx.MockTransport`; no real network access is required. It covers:

- path parsing: comments, blanks, duplicates, BOM/CRLF, missing slashes, absolute URLs, ports
- URL normalization and bare-scheme hardening (`javascript:` stays on the target host)
- filename sanitizing, reserved Windows names, traversal and safe path resolution
- private IPv4/IPv6 detection, allow-private behavior, redirect re-checks
- status classification, soft-404 fingerprinting, redirect chains, retries, size limits
- asset discovery (same-host only), robots blocking, report generation

CI runs the tests on Python 3.10, 3.12 and 3.14 via
[`.github/workflows/tests.yml`](.github/workflows/tests.yml).

A full click-through checklist is in [`docs/MANUAL_TESTING.md`](docs/MANUAL_TESTING.md).

## Local demo (own server)

Create a tiny site:

```bash
mkdir -p demo/admin demo/js
printf '<h1>home</h1>' > demo/index.html
printf '<h1>admin</h1>' > demo/admin/index.html
printf 'console.log(1)' > demo/js/app.js
cd demo && python -m http.server 8000
```

In a second terminal, from the project folder:

```bash
scrapit
```

Set target `http://127.0.0.1:8000`, enable **Settings → 13. Allow localhost/private targets**, load
`sample_paths.txt`, then **Start fetching**. Or:

```bash
scrapit --target http://127.0.0.1:8000 \
        --paths sample_paths.txt \
        --allow-private --yes --i-have-permission --delay 0.2
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `scrapit: command not found` | Activate the venv, or run `python -m scrapit` from the project folder |
| Menu looks plain / piped input | Expected: questionary is disabled without a TTY, numbered fallback is used |
| `Refusing to contact ... non-public address` | Enable *Allow localhost/private targets* only for your own local server |
| `TLS error` | Use a trusted certificate, or disable *Verify TLS* only for a test server you control |
| Everything marked `BLOCKED_BY_ROBOTS` | The target disallows those paths; only override when you are authorized |
| `Output folder error` | Pick a writable `--output` directory |
| All 200 responses marked `SOFT_404` | The site returns the same page for unknown paths; try a different target |

## Responsible use

Scrapit exists for authorized testing and coursework: your own site, a local server, a deliberately
vulnerable practice target, or a site your instructor authorized in writing. Sending path requests to
third-party sites without permission can violate their terms and computer-misuse laws.

The confirmation prompt, private-address checks, robots.txt handling, request cap and audit log are
there so you can demonstrate responsible practice in your write-up.
