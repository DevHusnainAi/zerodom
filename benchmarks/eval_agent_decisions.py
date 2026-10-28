"""Does the snapshot format actually let a model DECIDE? (effectiveness, not just tokens)

The token benchmarks prove ZeroDOM's `agent_snapshot` is cheaper than agent-browser.
Cheaper is worthless if the model can't act on it. This harness closes that gap: it
hands real LLMs a ZeroDOM snapshot + a task and checks whether they pick the correct
`[ref]` and action. Two things are under test:

  Part 2 (state) — scenarios where the RIGHT answer depends on interactive state the
  snapshot now carries (a toggle already on, a form already `filled`, a panel already
  `open`). If the state markers are unreadable, the model acts wrongly.
  Part 3 (delta) — scenarios where the model is shown a `~delta` snapshot and must say
  what changed. If the delta syntax is opaque, it can't.

Models run via `opencode` (free tier, $0) across several providers, so a pass isn't a
fluke of one model's priors. A format that only one frontier model can read is a bad
format; these are small/cheap models — if THEY get it, the format is legible.

Run:   opencode auth login           # once
       uv run python benchmarks/eval_agent_decisions.py
       uv run python benchmarks/eval_agent_decisions.py --models opencode/glm-5.3-flash-free,...
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

from zerodom.parser import parse_html, compact_line, diff_nodes

# The entire format contract, as an agent would be system-prompted with it. This IS the
# prompt snippet an integrator (Firecrawl / agent-browser) ships: terse DSL + state
# markers + delta syntax, in ~180 tokens. Kept here so the eval tests the real thing.
LEGEND = """\
You control a web page through a compact snapshot. Each interactive element is one line:

  [ref] type label  <state>  ph="placeholder"

- `[ref]` is the handle you act on (e.g. [03]). `type` is the tag (a link has no tag shown).
- State markers (only shown when they apply):
    x        = checked (checkbox/switch/radio is ON)
    *        = selected (tab/option is the active one)
    on       = pressed (toggle button is active)
    open / closed = a disclosure/menu is expanded or collapsed
    current  = this nav item is the current page
    =text    = the field currently contains `text`
    filled   = a sensitive field (password/card) is filled (value hidden)
  The ABSENCE of a marker is meaningful: a field with no `=text`/`filled` is EMPTY, a
  checkbox/switch with no `x` is OFF, a panel with no `open` is COLLAPSED. Don't repeat
  an action whose state marker shows it's already done.
- A line starting with `~[ref]` in a delta means that element CHANGED since last turn.
  `+[ref]` means it appeared, `-[ref]` means it was removed. `(no change)` means nothing moved.
"""

ANSWER_RULE = (
    "Answer with ONE line only: the action then the ref, e.g. `click [03]` or `fill [05]` "
    "or `none` if the task is already satisfied and no action is needed. No explanation."
)


# ---- scenarios -------------------------------------------------------------------
# gold = set of acceptable "action ref" answers (lowercased). "none" is its own answer.
# Part 2: decision depends on captured state. Part 3: decision depends on reading a delta.

def _snap(html: str) -> str:
    return "\n".join(compact_line(n, terse=True) for n in parse_html(html)["nodes"])


SETTINGS = """<title>Settings</title>
<div role=switch aria-checked="true">Dark mode</div>
<div role=switch>Email alerts</div>
<button aria-expanded="false">Advanced</button>"""

LOGIN_READY = """<title>Login</title>
<input aria-label="Email" value="me@acme.com">
<input aria-label="Password" type=password value="s3cret">
<button>Sign in</button>"""

LOGIN_EMPTY = """<title>Login</title>
<input aria-label="Email" value="me@acme.com">
<input aria-label="Password" type=password>
<button>Sign in</button>"""

SHIPPING = """<title>Checkout</title>
<input id=s type=radio name=sh><label for=s>Standard</label>
<input id=e type=radio name=sh checked><label for=e>Express</label>
<button>Place order</button>"""

TABS = """<title>Dashboard</title>
<div role=tab aria-selected="true">Overview</div>
<div role=tab>Reports</div>
<div role=tab>Settings</div>"""

SCENARIOS = [
    # Part 2 — state-dependent decisions
    {"part": 2, "name": "toggle already on",
     "task": "Turn ON dark mode.", "snap": _snap(SETTINGS),
     "gold": {"none"}},  # Dark mode shows `x` (already on) → nothing to do
    {"part": 2, "name": "toggle off -> act",
     "task": "Turn ON email alerts.", "snap": _snap(SETTINGS),
     "gold": {"click [02]"}},  # Email alerts has no `x`
    {"part": 2, "name": "form ready -> submit",
     "task": "Log in. Both fields are already filled.", "snap": _snap(LOGIN_READY),
     "gold": {"click [03]"}},  # email `=...`, password `filled` → just submit
    {"part": 2, "name": "form incomplete -> fill",
     "task": "Log in.", "snap": _snap(LOGIN_EMPTY),
     "gold": {"fill [02]"}},  # password has no `filled` marker → must fill it
    {"part": 2, "name": "expand collapsed panel",
     "task": "Open the Advanced options.", "snap": _snap(SETTINGS),
     "gold": {"click [03]"}},  # Advanced is `closed`
    {"part": 2, "name": "radio already selected",
     "task": "Choose Express shipping.", "snap": _snap(SHIPPING),
     "gold": {"none"}},  # Express already shows `x` (checked)
    {"part": 2, "name": "switch active tab",
     "task": "Go to the Reports tab.", "snap": _snap(TABS),
     "gold": {"click [02]"}},  # Overview is `*`, Reports isn't
]

# Part 3 — delta comprehension. before -> after, model sees the delta text.
_before_nodes = parse_html(SHIPPING)["nodes"]
_after_html = SHIPPING.replace('id=s type=radio name=sh>', 'id=s type=radio name=sh checked>').replace('name=sh checked><label for=e', 'name=sh><label for=e')
_delta_text, _, _ = diff_nodes(parse_html(_after_html)["nodes"],
                               {"entries": [], "next": 1})  # fresh
# build a real delta: snapshot SHIPPING, then re-snapshot after switching the radio
_s1, _, _st = diff_nodes(_before_nodes, None)
_delta_text, _, _ = diff_nodes(parse_html(_after_html)["nodes"], _st)

SCENARIOS += [
    {"part": 3, "name": "delta: selection moved",
     "task": ("A moment ago you saw the shipping options. This turn's delta is below. "
              "Which shipping option is selected NOW?\n\n" + _delta_text
              + "\n\nAnswer with ONE word: Standard or Express."),
     "snap": None, "gold": {"standard"}},
]


def ask(model: str, prompt: str, timeout: int) -> str:
    try:
        out = subprocess.run(["opencode", "run", "-m", model, prompt],
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "<timeout>"
    return (out.stdout or "") + (out.stderr or "")


_REF = re.compile(r"\b(click|fill|none)\b(?:\s+\[?(\d{1,2})\]?)?", re.I)


def grade(scenario: dict, raw: str) -> tuple[bool, str]:
    """Lenient: pull the model's chosen action+ref (or a one-word answer) and match gold."""
    txt = raw.strip().lower()
    if scenario["part"] == 3:
        # one-word answer scenarios
        pick = next((w for w in ("standard", "express") if w in txt), "")
        return pick in scenario["gold"], pick or "<none>"
    m = _REF.search(txt)
    if not m:
        return False, "<unparsed>"
    verb, ref = m.group(1), m.group(2)
    ans = verb if verb == "none" else f"{verb} [{int(ref):02d}]" if ref else verb
    return ans in scenario["gold"], ans


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join([
        "opencode/glm-5.3-flash-free",
        "opencode/deepseek-v4.1-flash-free",
        "opencode/nemotron-3.5-lightning-free",
        "opencode/mimo-v2.6-flash-free",
        "opencode/flint-40b-free",
    ]))
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    scores: dict[str, list[bool]] = {m: [] for m in models}
    print(f"{len(SCENARIOS)} scenarios × {len(models)} models (opencode free tier, $0)\n")
    for sc in SCENARIOS:
        if sc["snap"] is not None:
            prompt = f"{LEGEND}\n{ANSWER_RULE}\n\nPAGE:\n{sc['snap']}\n\nTASK: {sc['task']}"
        else:
            prompt = f"{LEGEND}\n\nTASK: {sc['task']}"
        print(f"[P{sc['part']}] {sc['name']}  (gold: {sorted(sc['gold'])})")
        for m in models:
            raw = ask(m, prompt, args.timeout)
            ok, got = grade(sc, raw)
            scores[m].append(ok)
            print(f"    {'PASS' if ok else 'FAIL'}  {m.split('/')[-1]:<32} -> {got}")
        print()

    print("=" * 60)
    print("ACCURACY (decision correctness on ZeroDOM snapshots)")
    for m in models:
        s = scores[m]
        print(f"  {m.split('/')[-1]:<34} {sum(s)}/{len(s)}  ({100*sum(s)/len(s):.0f}%)")
    allv = [v for s in scores.values() for v in s]
    print(f"  {'OVERALL':<34} {sum(allv)}/{len(allv)}  ({100*sum(allv)/len(allv):.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
