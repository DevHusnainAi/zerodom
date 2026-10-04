# Crawl coverage: headless vs relay

`uv run benchmarks/benchmark_crawl.py` writes this table;
`ZERODOM_E2E=1` adds the Juice Shop target. One row per (target, leg).

| target | leg | pages | routes | apis | forms | recall | seconds | note |
|---|---|---:|---:|---:|---:|---:|---:|---|
| fixture | headless | 4 | 4/7 | 2/4 | 2 | 55% | 2.9 | missed: /account, /orders, /settings, /api/account, /api/orders |
| fixture | relay | — | — | — | — | — | — | no relay listening — run `zerodom relay` + the extension (or set ZERODOM_CDP_ENDPOINT) |
| juice-shop | headless | 1 | 1/18 | 1/12 | 0 | 7% | 4.5 | missed: search, login, register, basket, contact, complain, score-board, recycle, about, privacy-policy, forgot-password, delivery-method, order-history, change-password, wallet, data-export, administration, /api/Products, /api/Feedbacks, /api/Challenges, /rest/captcha, /api/SecurityQuestions, /rest/basket, /rest/country-mapping, /rest/user/whoami, /rest/order-history, /rest/wallet/balance, /api/BasketItems; +6 extra |
| juice-shop | relay | — | — | — | — | — | — | no relay listening — run `zerodom relay` + the extension (or set ZERODOM_CDP_ENDPOINT) |

## Reading this

- **recall** = (routes + API endpoints discovered) / ground truth. The
  crawl is read-only, so POST-only endpoints are out of the truth set
  and an endpoint counts only when a page load fires it — the number is
  conservative by construction.
- **fixture** is the deterministic session contrast: the guest nav has
  no links to /account /orders /settings (they 302 to /login), so the
  headless leg stalls at the public surface; the relay leg carries the
  seeded zd_session cookie and reaches every authed route and endpoint.
  Its one miss — /login — is real app behavior: the authed nav hides the
  sign-in link. The cookie is removed after the relay leg.
- **juice-shop** truth is source-derived (app.routing.ts + service GET
  endpoints, verified 2026-10-05) and runs only under ZERODOM_E2E=1.
  A down target is reported as unreachable rather than crawled as an
  error page. Relay rows there use the operator's real session as-is.
- **juice-shop's recall is honestly low**: its whole nav sits behind a
  hamburger drawer that is closed at every tested viewport (1280, 1920,
  2560 — all six nav anchors serialize data-zerodom-hidden, so the parser
  correctly drops them: they are not clickable). Read-only BFS never
  clicks (cli.crawl_site's contract), so the crawl maps the entry point
  and stops at 0 in-scope links. Expanding drawer-gated IAs belongs to
  the interaction layer (_act), not the crawler — a crawl-only ceiling,
  not a claim that the surface was fully mapped.
- **skipped relay**: no relay listening or no extension attached — this
  script never spawns a relay and never closes the operator's browser
  (crawl --relay opens and closes its own tab).
- **WSTG mapping** (OWASP WSTG latest, 4.1 Information Gathering):
  routes recall evidences 4.1.4 Attack Surface Identification and 4.1.10
  Map Application Architecture; the forms column evidences 4.1.6
  Identify Application Entry Points; api recall evidences 4.1.7 Map
  Execution Paths Through Application. Search-engine recon, server and
  framework fingerprinting, metafile and content review (4.1.1–3, 5,
  8–9) are different techniques and are not claimed here.
- **max_pages** = 15 (ZERODOM_CRAWL_MAX_PAGES raises the cap;
  a bigger map needs a bigger budget on the live target).

## Ground truth

- fixture routes: /, /products, /contact, /login, /account, /orders, /settings
- fixture apis: /api/products, /api/feedback, /api/account, /api/orders
- juice-shop routes (18): #/, #/search, #/login, #/register, #/basket, #/contact, #/complain, #/score-board, #/recycle, #/about, #/privacy-policy, #/forgot-password, #/delivery-method; authed: #/order-history, #/change-password, #/wallet, #/data-export, #/administration
- juice-shop apis (12): /api/Products, /api/Feedbacks, /rest/products/search, /api/Challenges, /rest/captcha, /api/SecurityQuestions, /rest/basket, /rest/country-mapping, /rest/user/whoami, /rest/order-history, /rest/wallet/balance, /api/BasketItems

## Run

- date: 2026-10-05 00:25:14 +0500
- e2e: 1 (juice-shop rows included)
- juice target: https://juice-shop.herokuapp.com/
- relay endpoint: absent (relay rows skip)
- no_solver: 1

