# New-Grad Job Watcher

Emails me whenever a new role appears in any watched list.

## Sources

| List | File | Format |
|---|---|---|
| [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) | `README.md` on **`dev`** | HTML `<table>` |
| [speedyapply/2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs) | `NEW_GRAD_USA.md` on `main` | Markdown pipe tables |

Add another by appending to `SOURCES` in `watcher.py` and pointing it at a parser.

## Speed

| What | How | Latency |
|---|---|---|
| New roles | This repo, polled every **60 s** | ~1 min |
| New issues | GitHub's native Watch → Custom → Issues | instant |

GitHub's cron is unreliable at short intervals — firings get delayed or dropped.
So cron is *not* the polling clock: each run stays alive and re-checks every 60 s
for 50 minutes. Cron fires every 10 min only to keep the chain going, and because
one run can be active while another queues, the next starts the instant the
current one ends. A skipped firing costs nothing.

## Notes

- SimplifyJobs' default branch is **`dev`**, not `main`.
- Files are read via the **GitHub API**, not `raw.githubusercontent.com`, which
  is CDN-cached for up to 5 minutes and would cap freshness.
- IDs prefer a stable key: Simplify's UUID, else the apply URL with tracking
  params stripped, else a hash of company+role+location. So edits to a row's
  text don't re-alert.
- Closed roles (🔒) never email.
- More than 60 "new" roles in one source means an upstream ID reshuffle, not a
  hiring spree — state resyncs silently instead of flooding the inbox.
- A source that fails to fetch or returns implausibly few rows is skipped that
  cycle, leaving its state untouched, so one list breaking can't lose the other.
- speedyapply's own header says "815 available"; the file really contains 688
  rows (it double-counts FAANG+/Quant). 688 is correct.

## Config

Secrets (Settings → Secrets and variables → Actions → Secrets):

| Secret | Value |
|---|---|
| `SMTP_USER` | Gmail address that sends |
| `SMTP_PASS` | Gmail **app password**, 16 chars |

Variables (same page → Variables):

| Variable | Value |
|---|---|
| `EMAIL_TO` | Where alerts go (comma-separate for several) |
| `SMTP_HOST` / `SMTP_PORT` | optional; default `smtp.gmail.com` / `587` |

## Manual controls

Actions → *Watch New-Grad-Positions* → **Run workflow**:

- **send_test_email** — confirm SMTP works
- **reset_state** — wipe `state.json`, re-seed from current lists
- **once** — single check, skip the 50-minute loop

## Filters

`FILTERS` at the top of `watcher.py`, applied to every source. Empty = send all.

```python
FILTERS = {
    "keywords":  ["Remote", "Los Angeles"],
    "companies": ["Google", "Meta"],
    "roles":     ["Software Engineer"],
}
```
