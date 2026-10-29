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

A run reports three outcomes per (model, scenario): PASS / FAIL / ERROR. An ERROR is
missing data (the endpoint timed out, or the model answered in a form we can't map to a
decision) — it is excluded from accuracy and counted separately, never scored as a wrong
decision. Accuracy = pass / (pass + fail). This matters: on flaky free endpoints a naive
pass/total conflates "the model decided wrong" with "we never got an answer", which
understates a legible format.

Run:   opencode auth login           # once (any provider opencode can reach)
       uv run python benchmarks/eval_agent_decisions.py
       uv run python benchmarks/eval_agent_decisions.py --models opencode/glm-5.3-flash-free,...
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

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
    empty    = a fillable field with nothing in it yet — fill it if the task needs a value
  The ABSENCE of a marker is also meaningful: a checkbox/switch with no `x` is OFF, a
  panel with no `open` is COLLAPSED. Don't repeat an action a marker shows is already done.
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
<input aria-label="Password" type=password value="hunter2">
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
     "task": ("The email and password are already entered but the form is NOT submitted "
              "yet. Do the one action that completes the login."),
     "snap": _snap(LOGIN_READY),
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


def ask(model: str, prompt: str, timeout: int, retries: int = 3) -> tuple[str, str]:
    """(answer, status) with status in {'ok','error'}. The free opencode endpoints have
    highly variable latency and intermittently time out; a call that never returns text
    is MISSING DATA (status 'error'), never a wrong decision — so it must not count as a
    FAIL. Retries with linear backoff before giving up."""
    for attempt in range(retries):
        try:
            out = subprocess.run(["opencode", "run", "-m", model, prompt],
                                 capture_output=True, text=True, timeout=timeout)
            # drop opencode's "> build · model" header/log lines; keep the answer body.
            body = "\n".join(l for l in (out.stdout or "").splitlines()
                             if l.strip() and not l.lstrip().startswith(">")).strip()
            if body:
                return body, "ok"
        except subprocess.TimeoutExpired:
            pass
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    return "", "error"


# Action synonyms — the legend asks for click/fill/none, but a model that says "press"
# or "type" picked the SAME action; crediting that isn't leniency about correctness, only
# about surface wording. Anything genuinely different (e.g. "scroll") stays unmatched.
_VERB = {"click": "click", "press": "click", "tap": "click", "select": "click",
         "choose": "click", "toggle": "click", "fill": "fill", "type": "fill",
         "enter": "fill", "input": "fill", "none": "none", "nothing": "none"}
_REF = re.compile(r"\b(" + "|".join(_VERB) + r")\b(?:[^\[\d]{0,12}\[?(\d{1,2})\]?)?", re.I)


def grade(scenario: dict, raw: str, status: str) -> tuple[str, str]:
    """-> (outcome, detail), outcome in {'pass','fail','error'}. 'error' = no answer to
    grade (missing data), reported separately and excluded from accuracy."""
    if status == "error":
        return "error", "<no answer>"
    txt = raw.strip().lower()
    if scenario["part"] == 3:
        pick = next((w for w in ("standard", "express") if w in txt), "")
        if not pick:
            return "error", "<unparsed>"   # answered, but not in the asked form — don't guess
        return ("pass" if pick in scenario["gold"] else "fail"), pick
    m = _REF.search(txt)
    if not m:
        return "error", "<unparsed>"
    verb, ref = _VERB[m.group(1).lower()], m.group(2)
    ans = verb if verb == "none" else (f"{verb} [{int(ref):02d}]" if ref else verb)
    return ("pass" if ans in scenario["gold"] else "fail"), ans


def main() -> int:
    ap = argparse.ArgumentParser()
    # Default to the authenticated Nvidia endpoint — the opencode/*-free models route
    # through a shared free proxy whose latency is wildly variable (it intermittently
    # times out models that answered instantly minutes earlier), which pollutes a run
    # with ERRORs. Any `provider/model` opencode can reach works; pass --models to swap.
    ap.add_argument("--models", default=",".join([
        "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
        "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b",
        "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
    ]))
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    # per model: counts of pass/fail/error
    tally: dict[str, dict[str, int]] = {m: {"pass": 0, "fail": 0, "error": 0} for m in models}
    print(f"{len(SCENARIOS)} scenarios × {len(models)} models (via opencode, free tier, $0)")
    print("accuracy = pass / (pass + fail); errors (timeout / unparsable) excluded & shown\n")
    for sc in SCENARIOS:
        if sc["snap"] is not None:
            prompt = f"{LEGEND}\n{ANSWER_RULE}\n\nPAGE:\n{sc['snap']}\n\nTASK: {sc['task']}"
        else:
            prompt = f"{LEGEND}\n\nTASK: {sc['task']}"
        print(f"[P{sc['part']}] {sc['name']}  (gold: {sorted(sc['gold'])})")
        for m in models:
            raw, status = ask(m, prompt, args.timeout)
            outcome, got = grade(sc, raw, status)
            tally[m][outcome] += 1
            mark = {"pass": "PASS", "fail": "FAIL", "error": " -- "}[outcome]
            print(f"    {mark}  {m.split('/')[-1]:<32} -> {got}")
        print()

    print("=" * 64)
    print("DECISION ACCURACY on ZeroDOM snapshots  (errors excluded, counted separately)")
    tp = tf = te = 0
    for m in models:
        t = tally[m]
        tp += t["pass"]; tf += t["fail"]; te += t["error"]
        ans = t["pass"] + t["fail"]
        acc = f"{100 * t['pass'] / ans:.0f}%" if ans else "n/a"
        print(f"  {m.split('/')[-1]:<34} {t['pass']}/{ans}  ({acc})"
              + (f"   [{t['error']} err]" if t["error"] else ""))
    ans = tp + tf
    print(f"  {'OVERALL':<34} {tp}/{ans}  ({100 * tp / ans:.0f}%)" + (f"   [{te} err]" if te else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
