#!/usr/bin/env bash
# Access-control check demo against a LOCAL OWASP Juice Shop (intentionally vulnerable,
# for security education). Read-only: scope -> crawl -> compare two logged-in users.
# Start the target first (throwaway container, nothing persisted):
#   docker run --rm -p 3000:3000 bkimminich/juice-shop
set -euo pipefail
BASE="${BASE:-http://localhost:3000}"
STATE_DIR="${STATE_DIR:-$(mktemp -d)}"

echo "== 1. Map the app: routes, forms, and the XHR/fetch API calls each page fires =="
# --deny keeps the read-only crawl off destructive links; the crawl stays in-scope
# to the target host by default (--scope adds extra hosts, none needed here).
uv run zerodom crawl "$BASE" --deny 'logout|delete'

echo
echo "== 2. Save two users' sessions, then compare one API response across both =="
echo "Log in as each user in a real Chrome, export storage_state to:"
echo "  $STATE_DIR/alice.json   $STATE_DIR/bob.json"
echo "(zerodom relay + the extension, or Playwright's storageState())."
echo
echo "Then diff a per-user object by id — a basket is the classic case:"
echo "  uv run zerodom compare \"$BASE/rest/basket/1\" \\"
echo "    --as alice=$STATE_DIR/alice.json --as bob=$STATE_DIR/bob.json"
echo
echo "Byte-identical bodies across two identities = both users saw the same basket:"
echo "the cross-tenant access-control tell. 'only_<name>' lists what each sees alone."
