"""
New-grad job watcher. Emails every newly added role from several GitHub lists.

Sources are polled through the authenticated GitHub API (NOT raw.githubusercontent,
which is CDN-cached for up to 5 minutes and would silently cap how fresh alerts
can be). New issues are handled by GitHub's own Watch notifications, which are
instant -- this script deliberately does not poll issues.

State lives in state.json, keyed per source, and is committed back to the repo by
the workflow only when a source's set of known job IDs actually changes.
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

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
API = "https://api.github.com"
GH_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Each source: how to fetch it and which parser understands its table format.
SOURCES = [
    {
        "key": "simplify",
        "label": "SimplifyJobs / New-Grad-Positions",
        "owner": "SimplifyJobs",
        "repo": "New-Grad-Positions",
        "path": "README.md",
        "parser": "html_tables",
        "min_rows": 50,
    },
    {
        "key": "speedyapply",
        "label": "speedyapply / 2027-SWE-College-Jobs (New Grad USA)",
        "owner": "speedyapply",
        "repo": "2027-SWE-College-Jobs",
        "path": "NEW_GRAD_USA.md",
        "parser": "md_tables",
        "min_rows": 50,
    },
]

# Optional filters, applied to every source. Empty lists = send everything.
FILTERS = {
    "keywords": [],
    "companies": [],
    "roles": [],
}

SIMPLIFY_ID_RE = re.compile(r"simplify\.jobs/p/([0-9a-fA-F-]{36})")
_BRANCH_CACHE = {}


# --------------------------------------------------------------------------
# GitHub fetching
# --------------------------------------------------------------------------

def _get(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "newgrad-watcher")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if GH_TOKEN:
        req.add_header("Authorization", f"Bearer {GH_TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def default_branch(owner, repo, fallback="main"):
    """Cached: the branch never changes mid-run, and this halves API calls."""
    ck = f"{owner}/{repo}"
    if ck not in _BRANCH_CACHE:
        try:
            _BRANCH_CACHE[ck] = json.loads(
                _get(f"{API}/repos/{owner}/{repo}"))["default_branch"]
        except Exception as exc:
            print(f"  ! could not read default branch ({exc}), assuming {fallback!r}")
            _BRANCH_CACHE[ck] = fallback
    return _BRANCH_CACHE[ck]


def fetch_file(owner, repo, path, branch):
    try:
        return _get(f"{API}/repos/{owner}/{repo}/contents/{path}?ref={branch}",
                    accept="application/vnd.github.raw")
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"  ! API fetch failed ({exc}); falling back to raw CDN")
        return _get(
            f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}",
            accept="text/plain")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _norm(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _clean_url(url):
    """Drop tracking params so IDs stay stable if a site retunes them."""
    url = (url or "").split("#")[0]
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    keep = [p for p in query.split("&")
            if p and not p.lower().startswith(("utm_", "ref=simplify"))]
    return base + ("?" + "&".join(keep) if keep else "")


def _job_id(simplify_url, apply_url, company, role, location):
    m = SIMPLIFY_ID_RE.search(simplify_url or "")
    if m:
        return "sj:" + m.group(1).lower()
    if apply_url:
        return "url:" + hashlib.md5(
            _clean_url(apply_url).encode()).hexdigest()[:16]
    return "txt:" + hashlib.md5(
        f"{company}|{role}|{location}".encode()).hexdigest()[:16]


def _dedupe(jobs):
    seen, out = set(), []
    for j in jobs:
        if j["id"] in seen:
            continue
        seen.add(j["id"])
        out.append(j)
    return out


# --------------------------------------------------------------------------
# Parser: SimplifyJobs -- HTML <table> blocks under Markdown '## ' headers
# --------------------------------------------------------------------------

def _cell_text(td):
    for br in td.find_all("br"):
        br.replace_with(" | ")
    return _norm(td.get_text(" ", strip=True))


def parse_html_tables(text):
    # The repo writes '</br>' (a stray closing tag) as a line break; html.parser
    # discards it, which would glue multiple locations together.
    text = re.sub(r"</\s*br\s*>", "<br/>", text, flags=re.I)

    jobs = []
    section, buf = "Other", []
    chunks = []
    for line in text.splitlines():
        if line.startswith("## "):
            if buf:
                chunks.append((section, "\n".join(buf)))
            section = _norm(line[3:]).replace("New Grad Roles", "").strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        chunks.append((section, "\n".join(buf)))

    for section, chunk in chunks:
        if "<table" not in chunk:
            continue
        soup = BeautifulSoup(chunk, "html.parser")
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
                links = [a["href"] for a in tds[3].find_all("a", href=True)]
                closed = "🔒" in tds[3].get_text() or not links

                apply_url = simplify_url = ""
                for href in links:
                    if "simplify.jobs/p/" in href:
                        simplify_url = href
                    elif not apply_url:
                        apply_url = href

                jobs.append({
                    "id": _job_id(simplify_url, apply_url, company, role, location),
                    "company": company,
                    "role": role,
                    "location": location,
                    "section": section,
                    "salary": "",
                    "apply_url": _clean_url(apply_url),
                    "extra_url": _clean_url(simplify_url),
                    "age": _cell_text(tds[4]),
                    "closed": closed,
                })
    return _dedupe(jobs)


# --------------------------------------------------------------------------
# Parser: speedyapply -- Markdown pipe tables between <!-- TABLE_* --> markers.
# FAANG+/Quant tables carry a Salary column; the 'Other' table does not, so
# columns are resolved from each table's own header row.
# --------------------------------------------------------------------------

_MARKER_START = re.compile(r"<!--\s*TABLE(?:_([A-Z]+))?_START\s*-->")
_MARKER_END = re.compile(r"<!--\s*TABLE(?:_[A-Z]+)?_END\s*-->")
_SECTION_NAMES = {"FAANG": "FAANG+", "QUANT": "Quant", None: "Other"}


def _split_md_cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _md_cell(cell):
    """Strip the wrapping markup but keep the text, and pull out any href."""
    frag = BeautifulSoup(cell, "html.parser")
    href = ""
    a = frag.find("a", href=True)
    if a:
        href = a["href"]
    text = _norm(frag.get_text(" ", strip=True))
    if not text:  # image-only cells (the Apply button)
        text = ""
    return text, href


def parse_md_tables(text):
    jobs = []
    section = None
    cols = None
    in_table = False

    for line in text.splitlines():
        m = _MARKER_START.search(line)
        if m:
            in_table, section, cols = True, m.group(1), None
            continue
        if _MARKER_END.search(line):
            in_table = False
            continue
        if not in_table or not line.lstrip().startswith("|"):
            continue

        cells = _split_md_cells(line)
        if not cells:
            continue

        # Header row defines this table's columns; skip the |---|---| divider.
        if cols is None:
            if cells[0].lower().startswith("company"):
                cols = [c.strip().lower() for c in cells]
            continue
        if set("".join(cells)) <= set("-: "):
            continue

        row = dict(zip(cols, cells))
        company, _ = _md_cell(row.get("company", ""))
        role, _ = _md_cell(row.get("position", ""))
        location, _ = _md_cell(row.get("location", ""))
        salary, _ = _md_cell(row.get("salary", ""))
        age, _ = _md_cell(row.get("age", ""))
        _, apply_url = _md_cell(row.get("posting", ""))

        if not company and not role:
            continue

        jobs.append({
            "id": _job_id("", apply_url, company, role, location),
            "company": company,
            "role": role,
            "location": location,
            "section": _SECTION_NAMES.get(section, section or "Other"),
            "salary": salary,
            "apply_url": _clean_url(apply_url),
            "extra_url": "",
            "age": age,
            "closed": not apply_url,
        })
    return _dedupe(jobs)


PARSERS = {"html_tables": parse_html_tables, "md_tables": parse_md_tables}


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
        blob = " ".join([job["company"], job["role"], job["location"],
                         job["section"]]).lower()
        if not any(k.lower() in blob for k in FILTERS["keywords"]):
            return False
    return True


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def _esc(s):
    return html.escape(s or "", quote=True)


def build_email(groups):
    """groups: list of (source_label, source_url, [jobs])."""
    total = sum(len(j) for _, _, j in groups)
    companies = []
    for _, _, jobs in groups:
        for j in jobs:
            if j["company"] not in companies:
                companies.append(j["company"])
    head = ", ".join(companies[:3])
    if len(companies) > 3:
        head += f" +{len(companies) - 3} more"
    subject = f"[New Grad] {total} new role{'s' if total != 1 else ''}: {head}"

    plain, blocks = [], []
    for label, url, jobs in groups:
        plain.append(f"=== {label} ({len(jobs)}) ===\n")
        rows = []
        show_salary = any(j["salary"] for j in jobs)
        for j in jobs:
            plain.append(
                f"{j['company']} — {j['role']}\n"
                f"  Location: {j['location']}\n"
                + (f"  Salary: {j['salary']}\n" if j["salary"] else "")
                + f"  Category: {j['section']}\n"
                f"  Apply: {j['apply_url'] or 'n/a'}\n"
            )
            salary_cell = (
                f'<td style="padding:12px 10px;vertical-align:top;color:#1a7f37;'
                f'font-weight:600;font-size:13px;white-space:nowrap;">'
                f'{_esc(j["salary"])}</td>' if show_salary else ""
            )
            apply_cell = (
                f'<a href="{_esc(j["apply_url"])}" style="background:#1f6feb;'
                f'color:#fff;padding:6px 14px;border-radius:6px;'
                f'text-decoration:none;font-size:13px;white-space:nowrap;">Apply</a>'
                if j["apply_url"] else '<span style="color:#8b949e;">—</span>'
            )
            extra_cell = (
                f'<div style="margin-top:6px;"><a href="{_esc(j["extra_url"])}" '
                f'style="color:#1f6feb;font-size:12px;text-decoration:none;">'
                f'Simplify&nbsp;↗</a></div>' if j["extra_url"] else ""
            )
            rows.append(f"""
      <tr style="border-bottom:1px solid #e6e8eb;">
        <td style="padding:12px 10px;vertical-align:top;">
          <div style="font-weight:600;color:#0d1117;">{_esc(j['company'])}</div>
          <div style="font-size:11px;color:#8b949e;margin-top:2px;">{_esc(j['section'])}</div>
        </td>
        <td style="padding:12px 10px;vertical-align:top;color:#24292f;">{_esc(j['role'])}</td>
        <td style="padding:12px 10px;vertical-align:top;color:#57606a;font-size:13px;">{_esc(j['location'])}</td>
        {salary_cell}
        <td style="padding:12px 10px;vertical-align:top;text-align:right;">
          {apply_cell}{extra_cell}
        </td>
      </tr>""")

        blocks.append(f"""
  <h3 style="margin:26px 0 10px;font-size:14px;color:#0d1117;">
    <a href="{_esc(url)}" style="color:#0d1117;text-decoration:none;">{_esc(label)}</a>
    <span style="color:#8b949e;font-weight:400;">&middot; {len(jobs)} new</span>
  </h3>
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    <thead>
      <tr style="background:#f6f8fa;text-align:left;color:#57606a;font-size:12px;text-transform:uppercase;letter-spacing:.03em;">
        <th style="padding:8px 10px;">Company</th>
        <th style="padding:8px 10px;">Role</th>
        <th style="padding:8px 10px;">Location</th>
        {'<th style="padding:8px 10px;">Salary</th>' if show_salary else ''}
        <th style="padding:8px 10px;text-align:right;">Link</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}
    </tbody>
  </table>""")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;max-width:820px;margin:0 auto;padding:20px;">
  <h2 style="margin:0 0 4px;color:#0d1117;font-size:20px;">
    {total} new new-grad role{'s' if total != 1 else ''}
  </h2>
  <p style="margin:0;color:#57606a;font-size:13px;">detected {stamp}</p>
  {''.join(blocks)}
  <p style="margin-top:24px;font-size:12px;color:#8b949e;">
    Sent by your new-grad watcher. Checks every 60 seconds.
  </p>
</div>"""
    return subject, "\n".join(plain), body


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
        print("    SMTP_PASS must be a 16-char APP PASSWORD with 2-Step on:")
        print("    https://myaccount.google.com/apppasswords")
        return False
    except Exception as exc:
        print(f"  ! SMTP failed: {type(exc).__name__}: {exc}")
        return False
    print(f"  ✓ emailed {to}: {subject}")
    return True


# --------------------------------------------------------------------------
# State (per source, with migration from the old single-source format)
# --------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"version": 2, "sources": {}}
    try:
        raw = json.load(open(STATE_FILE))
    except json.JSONDecodeError:
        print("  ! state.json unreadable, treating as first run")
        return {"version": 2, "sources": {}}

    if "sources" in raw:
        return raw
    # Old format: a flat {"seeded":..,"ids":[..]} that was SimplifyJobs only.
    print("  migrating state.json to multi-source format")
    return {
        "version": 2,
        "sources": {
            "simplify": {
                "seeded": raw.get("seeded", False),
                "updated": raw.get("updated"),
                "count": raw.get("count", len(raw.get("ids", []))),
                "ids": raw.get("ids", []),
            }
        },
    }


def save_state(state):
    state["version"] = 2
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


# --------------------------------------------------------------------------

def main():
    if os.getenv("SEND_TEST_EMAIL") == "true":
        print("Sending test email...")
        ok = send_email(
            "[New Grad] Watcher test email",
            "Your new-grad watcher is wired up correctly.",
            '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,'
            'sans-serif;padding:24px;"><h2 style="color:#0d1117;margin:0 0 8px;">'
            '✓ Watcher is live</h2><p style="color:#57606a;">Watching '
            + "; ".join(s["label"] for s in SOURCES)
            + '.</p></div>')
        sys.exit(0 if ok else 1)

    state = load_state()
    groups = []
    dirty = False

    for src in SOURCES:
        key = src["key"]
        branch = default_branch(src["owner"], src["repo"])
        print(f"[{key}] {src['owner']}/{src['repo']}@{branch}/{src['path']}")

        try:
            text = fetch_file(src["owner"], src["repo"], src["path"], branch)
            jobs = PARSERS[src["parser"]](text)
        except Exception as exc:
            print(f"  ! fetch/parse failed: {type(exc).__name__}: {exc}")
            continue

        open_jobs = [j for j in jobs if not j["closed"]]
        print(f"  parsed {len(jobs)} rows ({len(open_jobs)} open)")

        if len(jobs) < src["min_rows"]:
            print(f"  ! only {len(jobs)} rows (<{src['min_rows']}), "
                  f"skipping to avoid corrupting state")
            continue

        entry = state["sources"].setdefault(
            key, {"seeded": False, "ids": [], "count": 0})
        known = set(entry.get("ids", []))
        all_ids = {j["id"] for j in jobs}

        if not entry.get("seeded"):
            entry.update({"seeded": True, "ids": sorted(all_ids),
                          "count": len(all_ids),
                          "updated": datetime.now(timezone.utc)
                          .isoformat(timespec="seconds")})
            dirty = True
            print(f"  first run — seeded {len(all_ids)} roles, no email")
            continue

        new_jobs = [j for j in open_jobs if j["id"] not in known]
        to_send = [j for j in new_jobs if matches_filters(j)]
        print(f"  {len(new_jobs)} new"
              + (f", {len(to_send)} after filters" if len(to_send) != len(new_jobs) else ""))

        # Guard against an upstream ID scheme change flooding the inbox.
        if len(to_send) > 60:
            print(f"  ! {len(to_send)} 'new' roles looks like an ID reshuffle; "
                  f"resyncing without emailing")
            to_send = []

        if to_send:
            groups.append((src["label"],
                           f"https://github.com/{src['owner']}/{src['repo']}",
                           to_send))

        if all_ids != known:
            entry.update({"ids": sorted(all_ids), "count": len(all_ids),
                          "updated": datetime.now(timezone.utc)
                          .isoformat(timespec="seconds")})
            dirty = True

    if groups:
        send_email(*build_email(groups))
    else:
        print("No new roles to email.")

    if dirty:
        save_state(state)
        print("State updated.")


if __name__ == "__main__":
    main()
