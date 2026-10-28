# Decision effectiveness — can a model ACT on a ZeroDOM snapshot?

The token benchmarks (`RESULTS_AGENT_SNAPSHOT*.md`) prove ZeroDOM's `agent_snapshot` is
cheaper than agent-browser. Cheaper is worthless if the model can't act on it. This eval
closes that gap: it hands real LLMs a ZeroDOM snapshot + a task and checks they pick the
correct `[ref]` and action — including decisions only the new **state markers** make
answerable (`x`, `*`, `open`/`closed`, `=value`, `filled`), and one that requires reading
a **`~delta`**. Models run via `opencode` at $0.

## Methodology (and the measurement fix)

Three outcomes per (model, scenario): **PASS** / **FAIL** / **ERROR**. An ERROR is
*missing data* — the endpoint timed out, or the model answered in a form we can't map to
a decision. **Accuracy = pass / (pass + fail); errors are excluded and reported
separately.** This is the whole point: free LLM endpoints are flaky, and scoring a
timeout as a wrong decision conflates "the model decided wrong" with "we never got an
answer", which understates a format that is actually legible. Action wording is matched
by meaning (a model that says "press"/"type" chose the same action as "click"/"fill").

## Results

**Primary run — clean, authenticated endpoint: 100%.** 3 Nvidia models (30B / 120B /
550B), every scenario, zero errors:

| model | accuracy |
|---|---|
| nemotron-3.5-lightning-30b | **8/8 (100%)** |
| nemotron-3.5-super-vl-120b | **8/8 (100%)** |
| nemotron-3-ultra-550b | **8/8 (100%)** |
| **overall** | **24/24 (100%)** |

**Cross-family breadth.** 5 models from 5 families via opencode's free proxy (glm,
deepseek, nemotron, mimo, flint), scored under the same ERROR/FAIL rule: **33 pass,
4 fail, 3 error → 33/37 answered (89%)**, run before the `empty` fix and on the degraded
free proxy. It establishes that the format is legible across model families; the primary
run establishes the ceiling once the format carries emptiness explicitly.

Every state marker and the `~delta` syntax was interpreted correctly. The format is
legible to small, cheap models — it does not depend on a strong prior.

## How 100% was reached (root cause, not a nudge)

The earlier 92% had one failure class, the *same* scenario across the 30B and 550B: an
empty password field with the form otherwise ready — they clicked Submit instead of
filling it, because "this field is empty" had to be inferred from the **absence** of a
marker, and smaller models under-weight an absent signal. (The 120B inferred it; the
others didn't.)

The fix makes emptiness **explicit**: a fillable field with no value now renders `empty`.
This was first dismissed as "too many tokens" — wrong, and that dismissal was the actual
debt: only *fillable* fields carry it (a handful per page, not the mostly-link/button
majority), so the cost is negligible while the signal is decisive. With `empty` in the
snapshot, all three models fill the field: **24/24**.

## Other failures from the cross-family run, debugged (honest)

- **Model weakness (deepseek):** clicked a toggle already showing `x` ("turn ON dark
  mode" when it's already on → correct answer is *none*). Read the marker, ignored
  "already satisfied". Other models got it; a model limit, not a format one.
- **My ambiguous task (glm):** "both fields already filled" was read as "already logged
  in". **Fixed** — the task now says the form is filled but not yet submitted.
- **Infra, not format (3 errors):** opencode's free proxy has highly variable latency and
  intermittently times out — a model that answered seconds earlier can later time out at
  280s. Excluded as missing data. The primary run avoids them via the authenticated
  endpoint.

## What this validates

- State capture is **actionable**, not just emitted: toggles, filled forms, collapsed
  panels and active tabs all drove correct decisions.
- The delta format is **comprehensible**: models correctly reported the post-delta
  selection from `~` / `(N unchanged)` lines alone.

## Ceilings (named, not hidden)

- `# ponytail:` 8 hand-built scenarios — a legibility check, not a statistical
  benchmark. 100% here means the format is unambiguous on these decisions, not that no
  model will ever err; scale the set if a format change needs a tighter signal.
- `# ponytail:` free-proxy latency caps reliability; the harness defaults to the
  authenticated endpoint and classifies timeouts as ERROR, not FAIL.
- The cross-family run's deepseek miss (acting on an already-satisfied toggle) is a model
  limitation, not a format one — no snapshot change fixes a model that ignores a marker
  it correctly read.
