# Contributing

Thanks for looking. This project has one hard rule and a handful of habits, and
the rule matters more than everything else here.

## The rule

> **A selector must resolve to exactly one element, or the node should not
> exist.**

Everything else is negotiable. An ambiguous selector does not fail — it clicks
the wrong element, the agent continues as if it worked, and nobody finds out
until something is broken in production. That failure mode is the reason this
project exists.

So: **any pull request that changes how selectors are generated must add a test
asserting uniqueness against a real browser.** `tests/test_playwright.py` has
plenty of examples.

## Getting set up

```bash
uv sync
uv run playwright install chromium   # browser tests skip without it
uv run pytest                        # 131 tests
```

Browser tests are skipped automatically if Chromium is missing, so a partial run
is not a failure — but please install it before submitting.

## What a good PR looks like

- **A bug fix comes with a failing test first.** If you can reproduce it with a
  URL, put the URL in the issue; that is usually enough for us to write the
  fixture ourselves.
- **Small and focused.** One behaviour per PR.
- **Comments explain *why*, not *what*.** The code says what it does. The
  comment should say what would break if it were written the obvious way.

## Changes that need extra evidence

Two files carry disproportionate risk. A change to either must be accompanied by
a benchmark run, not just unit tests:

| file | why |
|---|---|
| `zerodom/parser.py` (`_selector`, `_path`, `_disambiguate`) | this is where uniqueness is won or lost |
| `zerodom/label_linker.py` | a heuristic that improves one site often costs another |

```bash
python benchmarks/benchmark_sites.py results.jsonl
```

That runs 111 live sites and reports how many selectors resolve to exactly one
element. If your change moves that number down, say so in the PR — a small
regression may still be the right trade, but it has to be visible.

## Design invariants

Please do not break these without opening an issue to discuss it first:

**The page belongs to the caller.** The serializer marks hidden elements with
`data-zerodom-hidden` and strips every mark in a `finally` block. A parse must
leave the DOM byte-identical to how it found it.

**One place resolves a node to a locator.** `frames.locate()`. Click, fill and
the report measurer all route through it. Two code paths that disagree about
where a node lives is exactly how you get a wrong click.

**Node ids are positional and renumber on every parse.** That is why the MCP
diff matches on `(type, label, selector)` instead. Anything that caches across
reads needs content-derived stable ids *first*.

**Nothing leaves the machine.** No telemetry, no phone-home, no analytics. This
is a stated guarantee, not a default.

## Reporting a wrong click

This is the highest-priority issue type there is. Please include:

- the URL, or a minimal HTML file that reproduces it
- what you expected to be clicked, and what was clicked instead
- the output of `zerodom <url> --html report.html`, which shows exactly which
  element each node resolved to

Security issues go to `contact@vexralabs.com` — see [SECURITY.md](SECURITY.md).
Please don't file those publicly.

## What usually gets declined

Not to be discouraging — just so you don't spend a weekend on something we've
already thought about:

- **Caching selectors between runs.** A cached selector that silently matches
  the wrong element is the exact bug class above.
- **Truncating long labels.** A news headline *is* the link's label; shortening
  it removes the information an agent uses to choose.
- **New dependencies** for something a few lines of stdlib can do.
- **Speculative abstractions** — an interface with one implementation, a config
  option for a value that never changes.

If you're unsure whether something falls here, open an issue before writing the
code. A two-line question is cheaper than a rejected PR.

## Licence

By contributing you agree your work is licensed under
[Apache-2.0](LICENSE), the same as the rest of the project.
