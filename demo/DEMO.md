# Demo: access-control checking with ZeroDOM

A runnable walk-through of ZeroDOM's `scope → crawl → compare` flow against a local,
intentionally-vulnerable target — [OWASP Juice Shop](https://owasp.org/www-project-juice-shop/).
Everything stays on your machine; the target runs in a throwaway container.

ZeroDOM gives an agent the access-control primitives and surfaces the *tell*
(two users, one object id, identical bytes back). You confirm the finding — ZeroDOM
does not decide that a bug exists.

## 1. Start the target

```bash
docker run --rm -p 3000:3000 bkimminich/juice-shop
```

`--rm` means nothing persists after you stop it. Juice Shop is built to be broken into,
so it's safe (and legal) to test — unlike a site you don't own.

## 2. Run the demo

```bash
./demo/run_idor_demo.sh
```

It:

1. **Crawls** the app read-only, with `--deny 'logout|delete'` — every route, its forms, and the XHR/`fetch` API calls
   each page fires (that's where the per-user object ids live, e.g. `/rest/basket/1`).
2. Prints the **compare** step: the same endpoint fetched under two logged-in users.

For an unattended MCP-driven run, lock scope to a file with `zerodom_set_scope` /
`ZERODOM_SCOPE` (a YAML/JSON path) so the agent can't widen its own bounds; the CLI
crawl above is bounded by `--scope`/`--deny` instead.

## 3. The tell

You supply two users' sessions as `storage_state` JSON (log in as each in a real
browser, export the state):

```bash
uv run zerodom compare "http://localhost:3000/rest/basket/1" \
  --as alice=alice.json --as bob=bob.json
```

- **Byte-identical bodies across both identities** → both users were served the *same*
  basket. One user reading another's object by id is the cross-tenant access-control
  finding.
- `only_<name>` lists the actionable nodes each identity sees that the other doesn't —
  a quick map of where the two roles' surfaces diverge.

`zerodom compare` uses each identity's real session cookies, so it works past a login
wall where an anonymous fetch just gets a redirect.

## Not included

No GIF — recording needs screen capture. The script and this doc are enough to run it
and see the diff yourself.
