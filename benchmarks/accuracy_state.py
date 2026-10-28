"""How ACCURATE is the state capture? Precision + recall vs live-DOM ground truth.

"41/41 parsed" only says the engine didn't crash. This measures whether the markers are
RIGHT and whether any were MISSED:

  Part A — controlled, deterministic. A page built via set_content, then mutated through
  DOM *properties* so they diverge from the HTML attributes (a checkbox checked with no
  `checked` attr, a value set via .value, an option selected via .selected). This is the
  exact case static parsing can't see and live stamping must. Every node's captured state
  is asserted against the known truth — it must be 100% or the capture logic is wrong.

  Part B — real pages, read-only. For every node whose selector resolves in the live DOM,
  the captured state is compared against the element's actual property/attribute. Reports
  precision (markers that were correct) and recall (true states we caught), with
  unresolvable selectors (shadow-piercing, detached) counted separately, not as errors.

Run:  uv run python benchmarks/accuracy_state.py
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from zerodom.playwright_wrapper import ZeroDOM

# Ground truth for a LIST of ZeroDOM selectors, read in one pass at one instant (a
# per-node loop lets a mutating SPA re-render selectors stale between reads). Each entry
# is the element's TRUE state as the browser sees it (properties, not attributes), "BADSEL"
# if the selector throws, or null if it doesn't resolve.
GT_MANY = r"""(sels) => sels.map(sel => {
  let el; try { el = document.querySelector(sel); } catch (e) { return "BADSEL"; }
  if (!el) return null;
  const t = el.tagName, r = {};
  // checked truth: aria-checked wins on any element (switch/checkbox role on a button),
  // else the native .checked property for a real checkbox/radio input.
  const ac = el.getAttribute("aria-checked");
  if (ac === "true" || ac === "false") r.checked = ac === "true";
  if (t === "INPUT" || t === "TEXTAREA") {
    if (el.type === "checkbox" || el.type === "radio") { if (!("checked" in r)) r.checked = el.checked; }
    else { r.value = el.value || ""; r.sensitive = el.type === "password"; }
  } else if (t === "SELECT") r.value = el.value || "";
  const ae = el.getAttribute("aria-expanded"); if (ae === "true" || ae === "false") r.expanded = ae === "true";
  if (el.getAttribute("aria-selected") === "true" || (t === "OPTION" && el.selected)) r.selected = true;
  if (el.getAttribute("aria-pressed") === "true") r.pressed = true;
  return r;
})"""


def part_a() -> int:
    """Controlled: live properties diverge from attributes. Must be perfect."""
    html = """<!doctype html><title>T</title><body>
      <input id=c1 type=checkbox><label for=c1>Alpha</label>
      <input id=c2 type=checkbox checked><label for=c2>Beta</label>
      <input id=t1 type=text aria-label=Empty>
      <input id=t2 type=text aria-label=Named>
      <input id=pw type=password aria-label=Secret>
      <select id=s><option>A</option><option id=o2>B</option></select>
      <button id=exp aria-expanded=false>More</button>
      <div role=tab aria-selected=true tabindex=0>TabOne</div>
    </body>"""
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True); pg = b.new_page()
        pg.set_content(html)
        # mutate via PROPERTIES so they differ from the static HTML attributes:
        pg.eval_on_selector("#c1", "e => e.checked = true")     # checked, but no attribute
        pg.eval_on_selector("#t1", "e => e.value = ''")          # explicitly empty
        pg.eval_on_selector("#t2", "e => e.value = 'hello'")     # value via property
        pg.eval_on_selector("#o2", "e => e.selected = true")     # select B at runtime
        pg.eval_on_selector("#pw", "e => e.value = 'hunter2'")   # sensitive -> filled, redacted
        nodes = {n.get("label") or n["id"]: n for n in ZeroDOM.from_page(pg)["nodes"]}
        b.close()

    # (label, predicate, description) — ground truth we set above
    checks = [
        ("Alpha", lambda n: n.get("checked") is True, "checkbox checked via property (no attr)"),
        ("Beta", lambda n: n.get("checked") is True, "checkbox checked via attribute"),
        ("Empty", lambda n: n.get("action") == "fill" and not n.get("value") and not n.get("filled"),
         "empty text field -> empty marker"),
        ("Named", lambda n: n.get("value") == "hello", "text value via property"),
        ("Secret", lambda n: n.get("filled") is True and not n.get("value"),
         "password -> filled, value redacted"),
        ("More", lambda n: n.get("expanded") is False, "aria-expanded=false -> closed"),
        ("TabOne", lambda n: n.get("selected") is True, "aria-selected tab"),
    ]
    bad = 0
    print("Part A — controlled (live state vs attributes). Must be 100%.")
    for label, pred, desc in checks:
        n = nodes.get(label)
        ok = n is not None and pred(n)
        bad += not ok
        print(f"  {'OK ' if ok else 'XX '} {desc:<45} {'' if ok else f'(got {n})'}")
    # the select's selected option 'B' should surface as the select's value
    print(f"  (select#s value captured as: {nodes.get('Unlabelled Element', {}).get('value')!r})")
    print(f"  Part A: {len(checks) - bad}/{len(checks)} correct\n")
    return bad


def part_b() -> None:
    """Real pages, read-only: agreement between captured state and live DOM, per dimension.

    Agreement (not precision/recall) because a correct `empty`/unchecked call is itself an
    accuracy win — the precision/recall framing hid those as invisible true-negatives.
    Every mismatch is printed so a real miss is never averaged away."""
    urls = ["https://httpbin.org/forms/post", "https://www.wikipedia.org", "https://duckduckgo.com",
            "https://developer.mozilla.org/en-US/docs/Web/HTML", "https://www.python.org",
            "https://pypi.org", "https://news.ycombinator.com", "https://www.gov.uk"]
    UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/131.0.0.0 Safari/537.36")
    correct = wrong = unresolved = 0
    mismatches: list[str] = []
    print("Part B — real pages, read-only: captured state vs live DOM (agreement per dimension)")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True); ctx = b.new_context(user_agent=UA)
        ctx.route("**/*", lambda r: (r.abort if r.request.resource_type in ("image", "media", "font") else r.continue_)())
        for url in urls:
            pg = ctx.new_page()
            c = w = u = 0
            try:
                pg.goto(url, wait_until="load", timeout=45000)
                nodes = ZeroDOM.from_page(pg)["nodes"]
                gts = pg.evaluate(GT_MANY, [n["selector"] for n in nodes])  # one instant
                for n, gt in zip(nodes, gts):
                    if gt is None or gt == "BADSEL":
                        if _has_marker(n):
                            u += 1
                        continue
                    for dim, ok, detail in _compare(n, gt):
                        if ok:
                            c += 1
                        else:
                            w += 1
                            mismatches.append(f"    {url.split('//')[-1][:30]} [{n['id']}] {dim}: {detail}")
            except Exception as e:
                print(f"  {url.split('//')[-1][:46]:<46} skipped ({type(e).__name__})")
                pg.close(); continue
            pg.close()
            correct += c; wrong += w; unresolved += u
            print(f"  {url.split('//')[-1][:46]:<46} correct={c:<4} wrong={w:<3} unresolved={u}")
        b.close()
    total = correct + wrong
    print(f"\n  AGREEMENT {100*correct/total:.1f}%  ({correct}/{total} comparable dimensions matched the live DOM)")
    print(f"  unresolvable selectors carrying a marker (dynamic re-render / shadow-pierce): {unresolved}")
    if mismatches:
        print(f"  {len(mismatches)} mismatch(es):")
        print("\n".join(mismatches[:25]))
    else:
        print("  zero mismatches on every resolvable node.")


def _has_marker(n: dict) -> bool:
    return any(k in n for k in ("checked", "expanded", "selected", "pressed", "current")) \
        or bool(n.get("value")) or bool(n.get("filled"))


def _compare(n: dict, gt: dict):
    """Yield (dimension, correct: bool, detail) per comparable dimension — true-negatives
    (ZeroDOM correctly says 'empty'/unchecked) count as correct."""
    if "checked" in gt or "checked" in n:
        claimed, truth = n.get("checked") is True, gt.get("checked") is True
        yield ("checked", claimed == truth, f"zd={claimed} dom={truth}")
    for k in ("selected", "pressed"):
        if k in gt or k in n:
            claimed, truth = n.get(k) is True, gt.get(k) is True
            yield (k, claimed == truth, f"zd={claimed} dom={truth}")
    if "expanded" in gt or "expanded" in n:
        yield ("expanded", n.get("expanded") == gt.get("expanded"), f"zd={n.get('expanded')} dom={gt.get('expanded')}")
    # value: for fillable fields and selects. Verify the actual string, not just presence.
    if n.get("action") == "fill" or "value" in gt or "value" in n:
        dom_val = (gt.get("value") or "")[:80]
        if gt.get("sensitive"):
            # sensitive: ZeroDOM must claim `filled` (not the value) exactly when DOM has one
            yield ("filled", (n.get("filled") is True) == bool(dom_val), f"zd_filled={n.get('filled')} dom_has={bool(dom_val)}")
        else:
            zd_val = n.get("value") or ""
            yield ("value", zd_val == dom_val, f"zd={zd_val!r} dom={dom_val!r}")


def main() -> int:
    bad = part_a()
    print()
    part_b()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
