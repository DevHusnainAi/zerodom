# ZeroDOM rule packs

Community detection rules for `zerodom scan`. Each pack is deterministic YAML —
`yaml.safe_load` in, `re.search` out, **nothing is evaluated**, so there is nothing
to sandbox. A rule flags something the parser already found in the interaction
graph (a link, an input, a prefilled value); it never sends a request.

```bash
zerodom scan https://app.example.com --rules surfaces/secrets.yaml
zerodom scan https://app.example.com --rules surfaces/admin-surfaces.yaml --format md
```

The bundled default pack (`zerodom/surfaces.yaml`) runs when you pass no `--rules`.

## Write a detection in 5 lines

A rule is an `id`, a `severity` (`low` / `medium` / `high`), a `description`, and a
`match` of fixed keys ANDed together. That's it:

```yaml
rules:
  - id: exposed-admin-link
    severity: medium
    description: Link into an /admin surface reachable from this page.
    match:
      href_regex: "/admin(/|$|\\?)"
```

### Match keys

| key | matches against | kind |
|---|---|---|
| `type`, `role`, `input_type` | that node field | exact (case-insensitive) |
| `label_regex`, `href_regex`, `selector_regex`, `value_regex`, `placeholder_regex` | that node field | `re.search` |
| `has: [attr, …]` / `missing: [attr, …]` | node attributes present / absent | presence |
| `page: {present_hidden_regex, missing_hidden_regex}` | the page's hidden-field names (CSRF gate) | page-level |

An **unknown key is a load-time error**, not a silent no-op — so a pack that loads is
a pack that fires. A rule with only a `page:` gate and no `match:` produces one
finding per page.

## Contribute a pack

PRs welcome. Add a `surfaces/<name>.yaml` (or a rule to an existing pack), keep each
rule's `description` actionable, and prefer **low false positives** over coverage —
a rule that cries wolf gets the whole pack distrusted. `tests/test_surfaces.py`
loads every pack in this directory, so a malformed rule fails CI.
