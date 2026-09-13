#!/bin/bash
# Push changes and restart the watcher so it picks up the new code.
set -uo pipefail
cd "$HOME/Develop/simplify-newgrad-watcher" || exit 1
GH=./bin/gh
rm -f .git/*.lock .git/objects/*.lock .git/objects/*/tmp_obj_* 2>/dev/null

python3 -m py_compile watcher.py || { echo "watcher.py has a syntax error, aborting"; exit 1; }
rm -rf __pycache__ 2>/dev/null

SLUG="$("$GH" api user -q .login)/simplify-newgrad-watcher"

# The Actions bot commits state.json to origin, so this clone is behind and any
# local state.json is stale. Never let it overwrite the bot's version.
echo "Syncing with origin (the bot has been committing state.json)..."
git fetch -q origin main
git checkout -q origin/main -- state.json 2>/dev/null || true

git add -A
git -c commit.gpgsign=false commit -q -m "Watch speedyapply/2027-SWE-College-Jobs NEW_GRAD_USA.md alongside SimplifyJobs" 2>/dev/null || echo "  (no code changes to commit)"

git pull -q --rebase --autostash origin main || { echo "rebase onto origin failed"; exit 1; }
git push origin main || exit 1
echo "  pushed."

echo
echo "Cancelling the old run (it still has the previous code checked out)..."
for id in $("$GH" run list --repo "$SLUG" --status in_progress --json databaseId -q '.[].databaseId' 2>/dev/null); do
  "$GH" run cancel "$id" --repo "$SLUG" 2>/dev/null && echo "  cancelled $id"
done
for id in $("$GH" run list --repo "$SLUG" --status queued --json databaseId -q '.[].databaseId' 2>/dev/null); do
  "$GH" run cancel "$id" --repo "$SLUG" 2>/dev/null && echo "  cancelled queued $id"
done
sleep 10

echo "Starting a fresh run with both sources..."
"$GH" workflow run "Watch New-Grad-Positions" --repo "$SLUG"
sleep 35
"$GH" run list --repo "$SLUG" --limit 3
echo
echo "The first check seeds speedyapply's ~688 roles (no email - that's correct)."
echo "SimplifyJobs keeps its existing state, so it keeps alerting as normal."
echo "Live: https://github.com/$SLUG/actions"
