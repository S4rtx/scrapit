# Manual test checklist

Use this checklist before a demo or submission. Run everything from the project root with the
virtual environment activated.

## 0. Setup

- [ ] `pip install -e ".[dev]"` completes without errors
- [ ] `scrapit --version` prints `scrapit 2.2.0`
- [ ] `python -m scrapit --version` prints the same
- [ ] `python -m pytest -q` passes

## 1. Launch and menu

- [ ] `scrapit` shows the banner, target/paths/output summary and the ten menu options
- [ ] On launch the tool quietly checks GitHub; when a newer release exists it shows an update panel and asks whether to update
- [ ] An offline launch stays quiet and still opens the menu
- [ ] Arrow keys move the selection; Enter activates an option (questionary available)
- [ ] Piping input (for example `printf '10\ny\n' | python -m scrapit`) uses the numbered fallback
- [ ] Ordered numbers appear only once in the fallback menu
- [ ] Exit asks for confirmation; `y` exits cleanly, `n` returns to the menu

## 2. Set target

- [ ] `example.com` becomes `https://example.com`
- [ ] `HTTP://Example.COM/` becomes `http://example.com`
- [ ] A URL with a path/query/trailing slash is reduced to `scheme://host[:port]`
- [ ] The resolved IP is shown next to the target
- [ ] A private target shows a warning but can still be set
- [ ] Empty input leaves the target unchanged
- [ ] `ftp://example.com` and empty/invalid hosts show an error and return to the menu

## 3. Load paths

- [ ] `sample_paths.txt` reports 10 paths and previews the first 10
- [ ] Comments and blank lines are ignored
- [ ] Missing slashes are added (`robots.txt` → `/robots.txt`)
- [ ] Duplicates are removed and reported
- [ ] Full URLs on the target host are accepted; other hosts are skipped with a warning
- [ ] Spaces are percent-encoded; control characters cause a skip
- [ ] A missing, empty or comment-only file shows a friendly error; the menu still works

## 4. Settings

- [ ] Every setting displays its current value
- [ ] Delay below `0.2` is rejected
- [ ] Timeout outside `1–300` is rejected
- [ ] Retries outside `0–5` are rejected
- [ ] Max size outside `1 KB–1 GB` is rejected
- [ ] TLS off shows a warning panel
- [ ] Private targets on shows a warning panel
- [ ] Extra headers parse `Name: Value; Name2: Value2`; invalid input is rejected
- [ ] Invalid values do not change the active config; valid values persist
- [ ] Back returns to the main menu

## 5. Preview

- [ ] Preview shows one full URL per path
- [ ] The planned request count includes robots.txt and the soft-404 probe
- [ ] More than 100 paths are truncated with a note
- [ ] Paths above the cap produce a warning

## 6. Fetch and scope confirmation

- [ ] With no permission flag, the scope panel shows host, IP and request count
- [ ] Starting a run prompts for an optional run name used in the output folder name
- [ ] Leaving the run name blank falls back to the timestamp folder
- [ ] Typing the wrong hostname cancels with no requests
- [ ] Typing the exact hostname starts the run
- [ ] `--yes` alone does **not** bypass the typed confirmation
- [ ] Non-interactive mode without `--i-have-permission` exits with code `2`
- [ ] The live progress bar and results table update for each path
- [ ] The run finishes without `Unexpected error: list index out of range` on a real terminal
- [ ] 2xx is green, 3xx yellow, 401/403 orange, 404/5xx/errors red, robots-blocked dim
- [ ] Redirects are followed and their chains are recorded
- [ ] A private-address redirect is blocked before the request is sent
- [ ] `robots.txt` disallowed paths are marked `BLOCKED_BY_ROBOTS`
- [ ] A target that answers 200 for every path marks identical bodies as `SOFT_404`
- [ ] Oversized responses are marked `TOO_LARGE` and not saved
- [ ] Timeouts/DNS/TLS errors record a specific reason instead of crashing

## 7. Reports and output

- [ ] The run folder appears under `output/<host>/<timestamp>/` (or `output/<host>/<name>_<timestamp>/`)
- [ ] `files/` contains the downloaded bodies
- [ ] `headers/` contains one `.headers.txt` per saved file
- [ ] `results.json`, `results.csv` and `report.md` exist
- [ ] `results.csv` has exactly:
      `path,url,final_url,status,label,content_type,size_bytes,sha256,response_ms,saved_as,error`
- [ ] `report.md` contains settings, counts, found/protected/not-found/error tables and notes
- [ ] `scrapit.log` exists in the run folder and in the host folder (audit log)
- [ ] Every request appears in the host audit log with a timestamp and status

## 8. Results and export

- [ ] **View last results** loads the newest `results.json` and renders a table
- [ ] **Export** regenerates all three reports and creates a ZIP next to the run folder
- [ ] The ZIP contains `files/`, `headers/` and the reports
- [ ] Export on a missing/invalid run folder shows a friendly error

## 9. Updates

- [ ] Menu **Check for updates** reports "up to date" or shows installed vs latest with the release URL
- [ ] `scrapit --check-updates` prints the same and exits `0`
- [ ] `scrapit --update` fast-forwards a clean git checkout and says to restart
- [ ] A dirty checkout reports the `git pull failed` message and leaves files untouched
- [ ] A non-git install prints the `pip install --upgrade git+...` fallback instead of touching files
- [ ] With GitHub unreachable, the check reports an error and the menu keeps working

## 10. Interrupt and non-TTY behavior

- [ ] Ctrl+C during a run stops it, saves partial results and writes reports (no traceback)
- [ ] Ctrl+C at the menu exits gracefully
- [ ] Piped output contains no ANSI escape codes and no tracebacks
- [ ] Ctrl+D / EOF at the menu exits cleanly

## 11. End-to-end against a local server

```bash
mkdir -p /tmp/scrapit-demo/admin /tmp/scrapit-demo/js
printf '<h1>home</h1>' > /tmp/scrapit-demo/index.html
printf '<h1>admin</h1>' > /tmp/scrapit-demo/admin/index.html
printf 'console.log(1)' > /tmp/scrapit-demo/js/app.js
printf 'User-agent: *\nDisallow: /secret\n' > /tmp/scrapit-demo/robots.txt
python -m http.server 8000 --bind 127.0.0.1 --directory /tmp/scrapit-demo
```

In another terminal:

- [ ] `scrapit --target http://127.0.0.1:8000 --paths sample_paths.txt --allow-private --yes --i-have-permission --delay 0.2`
- [ ] `/robots.txt` and the real pages are downloaded
- [ ] Missing paths are `NOT_FOUND`
- [ ] Directory redirects (`/admin` → `/admin/`) are followed and recorded
- [ ] The summary panel reports the correct counts and run folder
- [ ] `report.md` opens and reads correctly

## 12. Safety regression

- [ ] Without `--allow-private`, `http://127.0.0.1:8000` is refused with an explanation
- [ ] A `10.x`, `192.168.x`, `169.254.x`, `::1` or `fc00::` target is refused
- [ ] A public site redirecting to `http://127.0.0.1/` is blocked before the internal request
- [ ] A paths file with 501 paths is refused; 201–500 warns and asks (unless `--yes`)
- [ ] No request is sent for any blocked path; the audit log reflects that
