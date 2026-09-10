"""
SimplifyJobs/New-Grad-Positions watcher.

Polls the repo README via the authenticated GitHub API (NOT raw.githubusercontent,
which is CDN-cached for up to 5 minutes) and emails every newly added role.

New issues are handled separately by GitHub's own Watch notifications, which are
instant -- this script deliberately does not poll issues.

State lives in state.json and is committed back to the repo by the workflow only
when the set of known job IDs actually changes.
"""

import os
import sys
import re
import json
import html
import hashlib
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bs4 import BeautifulSoup

OWNER = "SimplifyJobs"
REPO = "New-Grad-Positions"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

API = "https://api.github.com"
GH_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Optional filters. Empty lists = send everything.
FILTERS = {
    "keywords": [],
    "companies": [],
    "roles": [],
}

SIMPLIFY_ID_RE = re.compile(r"simplify\.jobs/p/([0-9a-fA-F-]{36})")


# --------------------------------------------------------------------------
# GitHub fetching
# --------------------------------------------------------------------------

def _get(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "simplify-newgrad-watcher")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if GH_TOKEN:
        req.add_header("Authorization", f"Bearer {GH_TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def default_branch():
    try:
        return json.loads(_get(f"{API}/repos/{OWNER}/{REPO}"))["default_branch"]
    except Exception as exc:
        print(f"  ! could not read default branch ({exc}), assuming 'dev'")
        return "dev"


def fetch_readme(branch):
    """Prefer the API: raw.githubusercontent is CDN-cached for up to 5 minutes,
    which would silently cap how fresh our alerts can be."""
    try:
        return _get(
            f"{API}/repos/{OWNER}/{REPO}/contents/README.md?ref={branch}",
            accept="application/vnd.github.raw",
        )
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"  ! API fetch failed ({exc}); falling back to raw CDN")
        return _get(
            f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{branch}/README.md",
            accept="text/plain",
        )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _cell_text(td):
    for br in td.find_all("br"):
        br.replace_with(" | ")
    text = td.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _clean_url(url):
    """Drop Simplify's tracking params so IDs stay stable if they retune them."""
    url = url.split("#")[0]
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    keep = [
        p for p in query.split("&")
        if p and not p.lower().startswith(("utm_", "ref=simplify"))
    ]
    return base + ("?" + "&".join(keep) if keep else "")


def _split_sections(readme_text):
    """The README uses Markdown '## ' headers, not <h2>, so split on those."""
    chunks, section, buf = [], "Other", []
    for line in readme_text.splitlines():
        if line.startswith("## "):
            if buf:
                chunks.append((section, "\n".join(buf)))
            section = re.sub(r"\s+", " ", line[3:]).replace(
                "New Grad Roles", "").strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        chunks.append((section, "\n".join(buf)))
    return chunks


def parse_jobs(readme_text):
    # The repo writes '</br>' (a stray closing tag) as a line break; html.parser
    # discards it, which would glue multiple locations together.
    readme_text = re.sub(r"</\s*br\s*>", "<br/>", readme_text, flags=re.I)

    jobs = []
    for section, chunk in _split_sections(readme_text):
        if "<table" not in chunk:
            continue
        _parse_chunk(BeautifulSoup(chunk, "html.parser"), section, jobs)

    # De-duplicate while preserving order (a role can appear in two sections).
    seen, unique = set(), []
    for job in jobs:
        if job["id"] in seen:
            continue
        seen.add(job["id"])
        unique.append(job)
    return unique


def _parse_chunk(soup, section, jobs):
    last_company = "Unknown"
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue

            company = _cell_text(tds[0])
            if company in ("↳", "->", ""):
                company = last_company
            else:
                last_company = company
            company = company.replace("🔥", "").strip()

            role = _cell_text(tds[1])
            location = _cell_text(tds[2])
            age = _cell_text(tds[4])

            links = [a["href"] for a in tds[3].find_all("a", href=True)]
            closed = "🔒" in tds[3].get_text() or not links

            apply_url = ""
            simplify_url = ""
            for href in links:
                if "simplify.jobs/p/" in href:
                    simplify_url = href
                elif not apply_url:
                    apply_url = href

            # Stable identity: Simplify's own UUID is best, then the raw apply
            # link, then a hash of the row's text as a last resort.
            m = SIMPLIFY_ID_RE.search(simplify_url)
            if m:
                job_id = "sj:" + m.group(1).lower()
            elif apply_url:
                job_id = "url:" + hashlib.md5(
                    _clean_url(apply_url).encode()).hexdigest()[:16]
            else:
                job_id = "txt:" + hashlib.md5(
                    f"{company}|{role}|{location}".encode()).hexdigest()[:16]

            jobs.append({
                "id": job_id,
                "company": company,
                "role": role,
                "location": location,
                "section": section,
                "apply_url": _clean_url(apply_url),
                "simplify_url": _clean_url(simplify_url),
                "age": age,
                "closed": closed,
            })


def matches_filters(job):
    if not any(FILTERS.values()):
        return True
    if FILTERS["companies"] and not any(
            c.lower() in job["company"].lower() for c in FILTERS["companies"]):
        return False
    if FILTERS["roles"] and not any(
            r.lower() in job["role"].lower() for r in FILTERS["roles"]):
        return False
    if FILTERS["keywords"]:
        blob = f"{job['company']} {job['role']} {job['location']} {job['section']}".lower()
        if not any(k.lower() in blob for k in FILTERS["keywords"]):
            return False
    return True


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def _esc(s):
    return html.escape(s or "", quote=True)


def build_email(jobs):
    companies = []
    for j in jobs:
        if j["company"] not in companies:
            companies.append(j["company"])
    head = ", ".join(companies[:3])
    if len(companies) > 3:
        head += f" +{len(companies) - 3} more"
    subject = f"[New Grad] {len(jobs)} new role{'s' if len(jobs) != 1 else ''}: {head}"

    plain = [f"{len(jobs)} new role(s) added to {OWNER}/{REPO}\n"]
    rows = []
    for j in jobs:
        plain.append(
            f"{j['company']} — {j['role']}\n"
            f"  Location: {j['location']}\n"
            f"  Category: {j['section']}\n"
            f"  Apply: {j['apply_url'] or 'n/a'}\n"
            f"  Simplify: {j['simplify_url'] or 'n/a'}\n"
        )
        apply_cell = (
            f'<a href="{_esc(j["apply_url"])}" style="background:#1f6feb;color:#fff;'
            f'padding:6px 14px;border-radius:6px;text-decoration:none;'
            f'font-size:13px;white-space:nowrap;">Apply</a>'
            if j["apply_url"] else '<span style="color:#8b949e;">—</span>'
        )
        simplify_cell = (
            f'<a href="{_esc(j["simplify_url"])}" style="color:#1f6feb;'
            f'font-size:12px;text-decoration:none;">Simplify&nbsp;↗</a>'
            if j["simplify_url"] else ""
        )
        rows.append(f"""
      <tr style="border-bottom:1px solid #e6e8eb;">
        <td style="padding:12px 10px;vertical-align:top;">
          <div style="font-weight:600;color:#0d1117;">{_esc(j['company'])}</div>
          <div style="font-size:11px;color:#8b949e;margin-top:2px;">{_esc(j['section'])}</div>
        </td>
        <td style="padding:12px 10px;vertical-align:top;color:#24292f;">{_esc(j['role'])}</td>
        <td style="padding:12px 10px;vertical-align:top;color:#57606a;font-size:13px;">{_esc(j['location'])}</td>
        <td style="padding:12px 10px;vertical-align:top;text-align:right;white-space:nowrap;">
          {apply_cell}<div style="margin-top:6px;">{simplify_cell}</div>
        </td>
      </tr>""")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body_html = f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;max-width:760px;margin:0 auto;padding:20px;">
  <h2 style="margin:0 0 4px;color:#0d1117;font-size:20px;">
    {len(jobs)} new new-grad role{'s' if len(jobs) != 1 else ''}
  </h2>
  <p style="margin:0 0 18px;color:#57606a;font-size:13px;">
    <a href="https://github.com/{OWNER}/{REPO}" style="color:#1f6feb;text-decoration:none;">{OWNER}/{REPO}</a>
    &middot; detected {stamp}
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    <thead>
      <tr style="background:#f6f8fa;text-align:left;color:#57606a;font-size:12px;text-transform:uppercase;letter-spacing:.03em;">
        <th style="padding:8px 10px;">Company</th>
        <th style="padding:8px 10px;">Role</th>
        <th style="padding:8px 10px;">Location</th>
        <th style="padding:8px 10px;text-align:right;">Link</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}
    </tbody>
  </table>
  <p style="margin-top:22px;font-size:12px;color:#8b949e;">
    Sent by your New-Grad-Positions watcher. Checks every 5 minutes.
  </p>
</div>"""
    return subject, "\n".join(plain), body_html


def send_email(subject, plain, body_html):
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pwd = os.getenv("SMTP_PASS")
    sender = os.getenv("EMAIL_FROM") or user
    to = os.getenv("EMAIL_TO")

    missing = [n for n, v in (("SMTP_USER", user), ("SMTP_PASS", pwd),
                              ("EMAIL_TO", to)) if not v]
    if missing:
        print("  ! NOT CONFIGURED — these are empty: " + ", ".join(missing))
        print("    SMTP_USER/SMTP_PASS are repo *secrets*; EMAIL_TO is a repo *variable*.")
        print("    Settings -> Secrets and variables -> Actions")
        print(f"  [dry run] would send: {subject}")
        return False

    print(f"  connecting to {host}:{port} as {user} -> {to}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, pwd)
            server.sendmail(sender, [t.strip() for t in to.split(",")],
                            msg.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        print("  ! GMAIL REJECTED THE LOGIN.")
        print(f"    {exc.smtp_code} {exc.smtp_error!r}")
        print("    Almost always one of:")
        print("      - SMTP_PASS is your normal Google password, not an APP PASSWORD")
        print("      - 2-Step Verification is off (app passwords need it on)")
        print("      - the app password was revoked, or belongs to a different account")
        print("    Make a fresh one: https://myaccount.google.com/apppasswords")
        return False
    except smtplib.SMTPRecipientsRefused as exc:
        print(f"  ! recipient refused: {exc.recipients}")
        return False
    except Exception as exc:
        print(f"  ! SMTP failed: {type(exc).__name__}: {exc}")
        return False
    print(f"  ✓ emailed {to}: {subject}")
    return True


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("  ! state.json unreadable, treating as first run")
    return {"seeded": False, "ids": []}


def save_state(ids):
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "seeded": True,
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "count": len(ids),
                "ids": sorted(ids),
            },
            f,
            indent=1,
        )


# --------------------------------------------------------------------------

def main():
    if os.getenv("SEND_TEST_EMAIL") == "true":
        print("Sending test email...")
        ok = send_email(
            "[New Grad] Watcher test email",
            "Your SimplifyJobs New-Grad-Positions watcher is wired up correctly.",
            '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'padding:24px;"><h2 style="color:#0d1117;margin:0 0 8px;">✓ Watcher is live</h2>'
            '<p style="color:#57606a;">Your SimplifyJobs New-Grad-Positions watcher is '
            'wired up correctly. You will get an email like this whenever new roles are '
            'added to the README.</p></div>',
        )
        sys.exit(0 if ok else 1)

    branch = default_branch()
    print(f"Fetching README from {OWNER}/{REPO}@{branch}")
    jobs = parse_jobs(fetch_readme(branch))
    open_jobs = [j for j in jobs if not j["closed"]]
    print(f"Parsed {len(jobs)} rows ({len(open_jobs)} open, "
          f"{len(jobs) - len(open_jobs)} closed)")

    if len(jobs) < 50:
        print("  ! suspiciously few rows parsed — refusing to update state")
        sys.exit(1)

    state = load_state()
    known = set(state.get("ids", []))
    all_ids = {j["id"] for j in jobs}

    if not state.get("seeded"):
        save_state(all_ids)
        print(f"First run — seeded {len(all_ids)} known roles, no email sent.")
        return

    new_jobs = [j for j in open_jobs if j["id"] not in known]
    print(f"{len(new_jobs)} new role(s)")

    to_send = [j for j in new_jobs if matches_filters(j)]
    if len(to_send) != len(new_jobs):
        print(f"  {len(to_send)} after filters")

    if to_send:
        # Guard against a repo-side ID scheme change flooding the inbox.
        if len(to_send) > 60:
            print(f"  ! {len(to_send)} 'new' roles looks like an ID reshuffle; "
                  f"resyncing state without emailing")
            save_state(all_ids)
            return
        subject, plain, body_html = build_email(to_send)
        send_email(subject, plain, body_html)

    if all_ids != known:
        save_state(all_ids)
        print("State updated.")
    else:
        print("No change.")


if __name__ == "__main__":
    main()
