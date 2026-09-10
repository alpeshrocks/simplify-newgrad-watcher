#!/bin/bash
# Diagnose why no email arrived. Run:  bash ~/Develop/simplify-newgrad-watcher/doctor.sh
set -uo pipefail   # deliberately NOT -e: we want to see every check

REPO_DIR="$HOME/Develop/simplify-newgrad-watcher"
GH="$REPO_DIR/bin/gh"
EMAIL_TO="alpeshsh@usc.edu"
cd "$REPO_DIR" || exit 1

OWNER=$("$GH" api user -q .login 2>/dev/null)
SLUG="$OWNER/simplify-newgrad-watcher"

line(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

line "1. Signed in as: ${OWNER:-NOT SIGNED IN}"

line "2. What GitHub actually has stored"
echo "   -- secrets --"
"$GH" secret list   --repo "$SLUG" 2>&1 | sed 's/^/   /'
echo "   -- variables --"
"$GH" variable list --repo "$SLUG" 2>&1 | sed 's/^/   /'
echo
echo "   Expect: secrets SMTP_USER + SMTP_PASS, variable EMAIL_TO."
echo "   Anything missing there is the whole problem."

line "3. Why the last run failed (from GitHub)"
"$GH" run view --repo "$SLUG" --log-failed 2>/dev/null \
  | grep -Ev '^\s*$' | tail -30 | sed 's/^/   /' \
  || echo "   (could not fetch log)"

line "4. Testing Gmail login from this Mac"
echo "   This proves whether the app password itself works."
read -rp  "   Gmail address: " U
read -rsp "   App password (hidden): " P; echo
P="${P// /}"

python3 - "$U" "$P" "$EMAIL_TO" <<'PYEOF'
import smtplib, sys, ssl
from email.mime.text import MIMEText
u, p, to = sys.argv[1], sys.argv[2], sys.argv[3]
print(f"   app password length: {len(p)} (Google app passwords are 16)")
try:
    s = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
    s.starttls(context=ssl.create_default_context())
    s.login(u, p)
    m = MIMEText("Local SMTP test from your New-Grad-Positions watcher. Login works.")
    m["Subject"] = "[New Grad] local SMTP test"
    m["From"], m["To"] = u, to
    s.sendmail(u, [to], m.as_string())
    s.quit()
    print(f"\n   ==> SUCCESS. Mail sent to {to}. The password is GOOD.")
    print("   ==> So the failure is the GitHub secrets, not Gmail. Fixing below.")
    sys.exit(0)
except smtplib.SMTPAuthenticationError as e:
    print(f"\n   ==> GMAIL REJECTED IT: {e.smtp_code} {e.smtp_error!r}")
    print("   ==> The app password is wrong/revoked, or 2-Step Verification is off.")
    print("   ==> Make a NEW one at https://myaccount.google.com/apppasswords")
    print("       (pick 'Mail' + 'Mac'; paste the 16 chars, spaces don't matter)")
    sys.exit(1)
except Exception as e:
    print(f"\n   ==> SMTP error: {type(e).__name__}: {e}")
    sys.exit(1)
PYEOF
SMTP_OK=$?

if [ $SMTP_OK -ne 0 ]; then
  line "Stopping: fix the app password, then run this script again."
  exit 1
fi

line "5. Re-storing the working credentials on GitHub"
printf '%s' "$U"        | "$GH" secret   set SMTP_USER --repo "$SLUG" && echo "   SMTP_USER set"
printf '%s' "$P"        | "$GH" secret   set SMTP_PASS --repo "$SLUG" && echo "   SMTP_PASS set"
"$GH" variable set EMAIL_TO --repo "$SLUG" --body "$EMAIL_TO"        && echo "   EMAIL_TO set"
unset P

line "6. Re-running the test on GitHub"
"$GH" workflow run "Watch New-Grad-Positions" --repo "$SLUG" -f send_test_email=true
sleep 20
"$GH" run list --repo "$SLUG" --limit 3
echo
echo "   Following the run (Ctrl-C is safe once it starts):"
"$GH" run watch --repo "$SLUG" --exit-status 2>/dev/null | tail -20

line "Done. If step 4 said SUCCESS you should have TWO emails now:"
echo "   - '[New Grad] local SMTP test'  (from your Mac)"
echo "   - '[New Grad] Watcher test email' (from GitHub Actions)"
