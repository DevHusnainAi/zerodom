"""Decision accuracy HEAD-TO-HEAD: ZeroDOM format vs Stagehand's real observe() tree.

`eval_agent_decisions.py` measured that models decide correctly from ZeroDOM's format.
This closes the last asserted-not-measured claim: do they decide *as well* from Stagehand's
actual tree? Same scenarios, same models, two representations.

Fairness guarantees:
- The Stagehand trees are its REAL serializer output — captured by running `observe()` on
  each scenario through a custom LLM client that records the exact prompt (see
  `RESULTS_VS_STAGEHAND.md` for the capture technique). Not a hand-rolled imitation.
- Each arm gets a legend that faithfully describes ITS OWN format — including how to read
  state. Where a format genuinely does not carry a piece of state, its legend cannot
  describe it; that asymmetry is the thing being measured, not a handicap we imposed.

Run:   uv run python benchmarks/eval_head_to_head.py
       uv run python benchmarks/eval_head_to_head.py --models nvidia/nvidia/nemotron-3-ultra-550b-a55b
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from eval_agent_decisions import SCENARIOS, LEGEND, ANSWER_RULE, ask, _VERB

# Stagehand's real captured trees, keyed by the scenario HTML name.
_TREES = json.load(open(os.path.join(os.path.dirname(__file__), "stagehand_scenario_trees.json")))

# Faithful legend for Stagehand's accessibility tree — describes exactly what the real
# tree carries (verified against the captures), nothing it doesn't.
SH_LEGEND = """\
You control a web page through its accessibility tree. Each element is one line:

  [id] role: name [state]

- `[id]` (e.g. [0-15]) is the handle you act on. `role` is the element kind (button,
  textbox, switch, radio, tab, link...).
- A `StaticText:` line nested under an element is that element's text content — for a
  textbox it is the value currently entered. A textbox with NO StaticText child beneath
  it is empty.
- `[checked]` marks a switch/checkbox/radio that is ON; `[selected]` marks the active
  tab/option. Their absence means OFF / not selected.
- Structural lines (RootWebArea, scrollable, html, body) are not actionable.
"""

# Per scenario (by name): which captured tree it uses, and the gold decision in
# Stagehand's ref scheme (read off the captured trees). Only Part-2 (action) scenarios —
# Part 3 is a ZeroDOM-delta-format test, not applicable to Stagehand's line tree.
SH = {
    "toggle already on":      ("SETTINGS",    {"none"}),        # Dark mode [checked]
    "toggle off -> act":      ("SETTINGS",    {"click [0-11]"}),# Email alerts, no [checked]
    "form ready -> submit":   ("LOGIN_READY", {"click [0-15]"}),# both fields show values
    "form incomplete -> fill":("LOGIN_EMPTY", {"fill [0-6]"}),  # Password has no StaticText child
    "expand collapsed panel": ("SETTINGS",    {"click [0-13]"}),# Advanced (no expand state carried)
    "radio already selected": ("SHIPPING",    {"none"}),        # Express [checked]
    "switch active tab":      ("TABS",        {"click [0-10]"}),# Overview [selected], Reports [0-10]
}

_REF = re.compile(r"\b(" + "|".join(_VERB) + r")\b(?:[^\[\d]{0,12}\[?(\d+(?:-\d+)?)\]?)?", re.I)


def grade(gold: set[str], raw: str, status: str) -> tuple[str, str]:
    """-> (outcome, detail). 'error' = no answer (missing data), excluded from accuracy."""
    if status == "error":
        return "error", "<no answer>"
    txt = raw.strip().lower()
    m = _REF.search(txt)
    if not m:
        return "error", "<unparsed>"
    verb, ref = _VERB[m.group(1).lower()], m.group(2)
    ans = verb if verb == "none" else (f"{verb} [{ref}]" if ref else verb)
    return ("pass" if ans in gold else "fail"), ans


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join([
        "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
        "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b",
        "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
    ]))
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    scen = [s for s in SCENARIOS if s["part"] == 2]
    tally = {arm: {m: {"pass": 0, "fail": 0, "error": 0} for m in models} for arm in ("zerodom", "stagehand")}
    print(f"{len(scen)} decision scenarios × {len(models)} models × 2 formats (opencode, $0)")
    print("accuracy = pass / (pass+fail); errors (timeout/unparsable) excluded\n")

    for sc in scen:
        tree_name, sh_gold = SH[sc["name"]]
        zd_prompt = f"{LEGEND}\n{ANSWER_RULE}\n\nPAGE:\n{sc['snap']}\n\nTASK: {sc['task']}"
        sh_prompt = f"{SH_LEGEND}\n{ANSWER_RULE}\n\nPAGE:\n{_TREES[tree_name]}\n\nTASK: {sc['task']}"
        print(f"[{sc['name']}]  zd-gold {sorted(sc['gold'])}  sh-gold {sorted(sh_gold)}")
        for m in models:
            for arm, prompt, gold in (("zerodom", zd_prompt, sc["gold"]), ("stagehand", sh_prompt, sh_gold)):
                raw, status = ask(m, prompt, args.timeout)
                outcome, got = grade(gold, raw, status)
                tally[arm][m][outcome] += 1
                mark = {"pass": "PASS", "fail": "FAIL", "error": " -- "}[outcome]
                print(f"    {arm:<9} {mark}  {m.split('/')[-1]:<30} -> {got}")
        print()

    print("=" * 66)
    print("DECISION ACCURACY — ZeroDOM format vs Stagehand observe() tree")
    for arm in ("zerodom", "stagehand"):
        tp = sum(tally[arm][m]["pass"] for m in models)
        tf = sum(tally[arm][m]["fail"] for m in models)
        te = sum(tally[arm][m]["error"] for m in models)
        ans = tp + tf
        acc = f"{100*tp/ans:.0f}%" if ans else "n/a"
        print(f"  {arm:<10} {tp}/{ans}  ({acc})" + (f"   [{te} err]" if te else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
