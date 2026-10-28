# Decision effectiveness — can a model ACT on a ZeroDOM snapshot?

The token benchmarks (`RESULTS_AGENT_SNAPSHOT*.md`) prove ZeroDOM's `agent_snapshot`
is cheaper than agent-browser. Cheaper is worthless if the model can't act on it. This
eval closes that gap: it hands real LLMs a ZeroDOM snapshot + a task and checks they
pick the correct `[ref]` and action — including decisions that only the new **state
markers** make answerable, and one that requires reading a **`~delta`**.

Run via `opencode` across five small, free models from different families. The point of
small models: a format only a frontier model can read is a bad format. If cheap models
read it, it's legible.

    uv run python benchmarks/eval_agent_decisions.py

## Result

| model | accuracy |
|---|---|
| glm-5.3-flash-free | 7/8 (88%) |
| deepseek-v4.1-flash-free | 6/8 (75%) |
| nemotron-3.5-lightning-free | 7/8 (88%) |
| mimo-v2.6-flash-free | 6/8 (75%) |
| flint-40b-free | 7/8 (88%) |
| **overall** | **33/40 (82%)** |

Every state marker — `x` checked, `*` selected, `closed`, `=value`, `filled`, and the
`~delta` syntax — was interpreted correctly by the majority of models. The format is
legible to cheap models; it does not depend on a strong prior.

## The 7 failures, classified (honest)

Debugged each one rather than reporting a bare number:

- **2 — model weakness (deepseek):** clicked a toggle that already showed `x`
  ("turn ON dark mode" when dark mode is already on → correct answer is *none*). It
  read the marker but ignored "already satisfied." Not a format problem — other models
  got both.
- **1 — reasoning/phrasing (glm):** on "log in, both fields already filled" it answered
  *none*, over-reading "filled" as "already logged in." The `filled`/`=value` markers
  were read correctly; the slip is task interpretation.
- **1 — genuine format subtlety (nemotron):** on an empty password field it clicked
  Submit instead of filling, missing that the *absence* of a `filled` marker means the
  field is empty. **Fixed:** the prompt legend now states the implicit negative
  explicitly ("no marker = empty / off / collapsed").
- **3 — infra, not format:** free-tier `opencode` endpoints have highly variable
  latency; these calls timed out (on re-run the same model/endpoint timed out even at
  280s, having answered the other 7 scenarios minutes earlier). Nothing to do with the
  snapshot.

## What this validates

- State capture is not just emitted, it's **actionable**: toggles, filled forms,
  collapsed panels and active tabs all drove correct decisions.
- The delta format is **comprehensible**: models correctly reported the post-delta
  selection from `~`/`(N unchanged)` lines alone.

## Ceiling (named, not hidden)

- `# ponytail:` 8 hand-built scenarios, 5 models — a legibility check, not a
  statistical benchmark. Scale the scenario set if a format change needs a tighter
  signal. Free-tier latency caps reliability; a paid endpoint would remove the 3
  infra timeouts.
