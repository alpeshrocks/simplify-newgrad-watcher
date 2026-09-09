# New-Grad-Positions Watcher

Emails me whenever a new role is added to
[SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions).

## How it works

| What | How | Latency |
|---|---|---|
| New roles in the README | This repo, GitHub Actions cron `*/5 * * * *` | ~5–15 min |
| New issues | GitHub's native Watch → Custom → Issues | instant |

Issues are deliberately **not** polled — GitHub's own notification email is
faster than any poller could be, and free.

## Notes

- The upstream repo's default branch is **`dev`**, not `main`.
- The README is read through the **GitHub API**, not `raw.githubusercontent.com`,
  which is CDN-cached for up to 5 minutes.
- Roles are identified by their Simplify UUID (`simplify.jobs/p/<uuid>`), so
  edits to a row's text don't cause a duplicate alert.
- Closed roles (🔒) never trigger an email.
- If more than 60 roles look "new" at once, that's an upstream ID reshuffle, not
  a hiring spree — state resyncs silently instead of flooding the inbox.

## Configuration

Repository **secrets** (Settings → Secrets and variables → Actions → Secrets):

| Secret | Value |
|---|---|
| `SMTP_USER` | Gmail address that sends the mail |
| `SMTP_PASS` | Gmail **app password** (16 chars, no spaces) |

Repository **variables** (same page → Variables tab):

| Variable | Value |
|---|---|
| `EMAIL_TO` | Where alerts go (comma-separate for multiple) |
| `SMTP_HOST` | optional, defaults to `smtp.gmail.com` |
| `SMTP_PORT` | optional, defaults to `587` |

## Manual controls

Actions → *Watch New-Grad-Positions* → **Run workflow**:

- **send_test_email** — sends a test email to confirm SMTP works
- **reset_state** — wipes `state.json` and re-seeds from the current README

## Filters

Edit `FILTERS` at the top of `watcher.py`. Empty lists mean "send everything".

```python
FILTERS = {
    "keywords":  ["Remote", "Los Angeles"],
    "companies": ["Google", "Meta"],
    "roles":     ["Software Engineer"],
}
```

## Keeping the cron alive

GitHub disables scheduled workflows in repos with **60 days of no commit
activity**. This one commits `state.json` whenever roles change, so it stays
awake on its own.
