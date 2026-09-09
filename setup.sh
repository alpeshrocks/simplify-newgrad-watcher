#!/bin/bash
# One-time setup for the New-Grad-Positions watcher.
# Run from Terminal:   bash ~/Develop/simplify-newgrad-watcher/setup.sh
set -euo pipefail

REPO_DIR="$HOME/Develop/simplify-newgrad-watcher"
REPO_NAME="simplify-newgrad-watcher"
EMAIL_TO="alpeshsh@usc.edu"
GH="$REPO_DIR/bin/gh"

cd "$REPO_DIR"

# Clear lock files left behind by the sandboxed helper (it can't delete files).
rm -f .git/*.lock .git/objects/*.lock .git/objects/*/tmp_obj_* 2>/dev/null || true
xattr -d com.apple.quarantine "$GH" 2>/dev/null || true
chmod +x "$GH"

# Commit anything the sandboxed helper could not (it hit stale git locks).
git add -A
git -c commit.gpgsign=false commit -q -m "Add one-time setup script" 2>/dev/null || true

echo
echo "==> gh version: $("$GH" --version | head -1)"

# ---------------------------------------------------------------- 1. auth
if ! "$GH" auth status >/dev/null 2>&1; then
  echo
  echo "==> Step 1/5: Sign in to GitHub"
  echo "    Choose: GitHub.com  ->  HTTPS  ->  Yes (authenticate git)  ->  Login with a web browser"
  "$GH" auth login
else
  echo "==> Step 1/5: Already signed in as $("$GH" api user -q .login)"
fi
"$GH" auth setup-git

OWNER=$("$GH" api user -q .login)

# ------------------------------------------------------- 2. create + push
echo
echo "==> Step 2/5: Creating $OWNER/$REPO_NAME and pushing"
if "$GH" repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1; then
  echo "    Repo already exists, pushing to it."
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/$OWNER/$REPO_NAME.git"
  git push -u origin main
else
  "$GH" repo create "$REPO_NAME" \
    --public \
    --source=. \
    --remote=origin \
    --push \
    --description "Emails me when SimplifyJobs/New-Grad-Positions adds a new role"
fi

# ------------------------------------------------------ 3. secrets + vars
echo
echo "==> Step 3/5: Email credentials"
echo "    Need a Gmail APP PASSWORD (not your normal password)."
echo "    Get one at: https://myaccount.google.com/apppasswords"
echo
read -rp  "    Gmail address to send FROM: " SMTP_USER
read -rsp "    Gmail app password (hidden):  " SMTP_PASS
echo

# Google shows app passwords as 4 groups of 4; strip the spaces.
SMTP_PASS="${SMTP_PASS// /}"

printf '%s' "$SMTP_USER" | "$GH" secret   set SMTP_USER --repo "$OWNER/$REPO_NAME"
printf '%s' "$SMTP_PASS" | "$GH" secret   set SMTP_PASS --repo "$OWNER/$REPO_NAME"
printf '%s' "$EMAIL_TO"  | "$GH" variable set EMAIL_TO  --repo "$OWNER/$REPO_NAME"
unset SMTP_PASS
echo "    Stored SMTP_USER + SMTP_PASS (secrets) and EMAIL_TO=$EMAIL_TO (variable)."

# ------------------------------------------------------------ 4. test run
echo
echo "==> Step 4/5: Sending a test email to $EMAIL_TO"
sleep 8   # let GitHub register the workflow file
"$GH" workflow run "Watch New-Grad-Positions" \
  --repo "$OWNER/$REPO_NAME" -f send_test_email=true
echo "    Triggered. Watch it at:"
echo "    https://github.com/$OWNER/$REPO_NAME/actions"

# ------------------------------------------------- 5. issue notifications
echo
echo "==> Step 5/5: Instant issue alerts (manual, 3 clicks)"
echo "    Open:  https://github.com/SimplifyJobs/New-Grad-Positions"
echo "    Click: Watch  ->  Custom  ->  check 'Issues'  ->  Apply"
echo
"$GH" browse --repo SimplifyJobs/New-Grad-Positions >/dev/null 2>&1 || true

echo
echo "===================================================================="
echo " Done. The watcher now runs every 5 minutes."
echo " Repo:    https://github.com/$OWNER/$REPO_NAME"
echo " Actions: https://github.com/$OWNER/$REPO_NAME/actions"
echo "===================================================================="
