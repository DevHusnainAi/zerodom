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

Two runs, both ~90%, consistent:

**Run A — clean, authenticated endpoint (primary).** 3 Nvidia models (30B / 120B / 550B),
zero errors:

| model | accuracy |
|---|---|
| nemotron-3.5-lightning-30b | 7/8 (88%) |
| nemotron-3.5-super-vl-120b | **8/8 (100%)** |
| nemotron-3-ultra-550b | 7/8 (88%) |
| **overall** | **22/24 (92%)** |

**Run B — cross-family breadth.** 5 models from 5 families via opencode's free proxy
(glm, deepseek, nemotron, mimo, flint), scored under the same ERROR/FAIL rule:

| | count |
|---|---|
| pass | 33 |
| fail (wrong decision) | 4 |
| error (timeout / unparsable — infra, excluded) | 3 |
| **accuracy (33 / 37 answered)** | **89%** |

Every state marker and the `~delta` syntax was interpreted correctly by the majority
across both runs. The format is legible to small, cheap models from multiple families —
it does not depend on a strong prior.

## Failures, debugged (honest)

- **The one persistent format edge — implicit emptiness.** Both Run-A failures are the
  *same* scenario: an empty password field (no `filled` marker) with the form otherwise
  ready; the 30B and 550B clicked Submit instead of filling it, missing that the
  *absence* of a marker means the field is empty. The 120B got it. A legend line now
  states the implicit negative explicitly ("no marker = empty / off / collapsed"), which
  helps but doesn't fully close it for the smallest models. See the named ceiling below.
- **Model weakness (Run B, deepseek):** clicked a toggle already showing `x` ("turn ON
  dark mode" when it's already on → correct answer is *none*). Read the marker, ignored
  "already satisfied". Other models got it.
- **My ambiguous task (Run B, glm):** "both fields already filled" was read as "already
  logged in". **Fixed** — the task now says the form is filled but not yet submitted.
- **Infra, not format (Run B, 3 errors):** opencode's free proxy has highly variable
  latency and intermittently times out — a model that answered seconds earlier can later
  time out at 280s. These are excluded as missing data. Run A avoids them entirely by
  using the authenticated endpoint.

## What this validates

- State capture is **actionable**, not just emitted: toggles, filled forms, collapsed
  panels and active tabs all drove correct decisions.
- The delta format is **comprehensible**: models correctly reported the post-delta
  selection from `~` / `(N unchanged)` lines alone.

## Ceilings (named, not hidden)

- `# ponytail:` **implicit emptiness.** Absence of a `=value`/`filled` marker encodes
  "empty", which the smallest models under-weight. Encoding an explicit `empty` marker
  would fix it but costs tokens on nearly every field (most fields are empty), defeating
  terseness — a bad trade. Left as-is; larger models read it correctly.
- `# ponytail:` 8 hand-built scenarios — a legibility check, not a statistical
  benchmark. Scale the set if a format change needs a tighter signal.
- `# ponytail:` free-proxy latency caps reliability; the harness now defaults to the
  authenticated Nvidia endpoint and classifies timeouts as ERROR, not FAIL.
