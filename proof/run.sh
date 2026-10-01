#!/usr/bin/env bash
# Reproduce ZeroDOM catching a broken-access-control bug on a PortSwigger
# Web Security Academy lab. Labs are ephemeral (per-session hostname, ~15-min
# idle / ~1-hr cap), so there is no permalink — you launch your own free
# instance and paste its host here. Everything below is read-only detection,
# not the destructive "solved" step.
#
# Usage:
#   1. Log into https://portswigger.net/web-security and launch the lab
#      "User role controlled by request parameter".
#   2. Capture wiener's session to user.json (Playwright storage_state):
#        wiener:peter  ->  proof/user.json
#      e.g. `playwright codegen --save-storage=proof/user.json <LAB>/login`
#      then log in as wiener and close the window.
#   3. Make proof/admin.json: a copy of user.json with the exploit cookie added
#      ({"name":"Admin","value":"true","domain":"<lab-host>","path":"/"}).
#   4. LAB=https://<your-instance>.web-security-academy.net ./proof/run.sh
set -euo pipefail

: "${LAB:?set LAB=https://<your-instance-id>.web-security-academy.net}"
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/output.jsonl"
: > "$out"

echo "== Lab 1: broken access control via a two-identity diff (compare) =="
echo "   GET $LAB/admin  as {user, admin}"
# user  -> 401/403 (no admin panel);  admin -> 200 with the delete-user controls.
# only_admin lists the actionable nodes the admin identity sees and user doesn't.
zerodom compare "$LAB/admin" \
  --as user="$here/user.json" \
  --as admin="$here/admin.json" | tee -a "$out"

echo
echo "== Lab 2 (optional, zero auth): deterministic surface discovery =="
echo "   scan + crawl find the unprotected admin panel disclosed in robots.txt"
# Launch the "Unprotected admin functionality" lab into $LAB2 to run this block.
if [ -n "${LAB2:-}" ]; then
  echo "$LAB2" | zerodom scan - | tee -a "$out"
  zerodom crawl "$LAB2" --max-pages 20 | tee -a "$out"
fi

echo
echo "Done. Machine-readable findings appended to $out"
echo "The tell is real; you confirm and (on a lab you own) exploit."
