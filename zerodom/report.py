"""Visual inspection: annotated screenshots and a self-contained HTML report.

Everything here is for humans. The graph tells you node [03] exists; only a
picture tells you [03] is the link you meant.
"""

from __future__ import annotations

import base64
import html
from collections import Counter
from string import Template
from typing import Any

BADGE_COLOR = "#16a34a"
FILL_COLOR = "#3b82f6"  # report only: separates "type here" from "click here"
LAYER_ID = "__zerodom_layer"

# Measures every node in one round trip. Boxes come back in *document* coordinates
# so they line up with a full-page screenshot rather than the current viewport.
_MEASURE_JS = """
(selectors) => {
  const doc = document.documentElement, body = document.body;
  const width = Math.max(doc.scrollWidth, body ? body.scrollWidth : 0, doc.clientWidth);
  const height = Math.max(doc.scrollHeight, body ? body.scrollHeight : 0, doc.clientHeight);
  const boxes = {};
  for (const [id, selector] of Object.entries(selectors)) {
    let el;
    try { el = document.querySelector(selector); } catch (e) { continue; }
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    boxes[id] = {
      x: r.left + window.scrollX, y: r.top + window.scrollY,
      w: r.width, h: r.height,
    };
  }
  return { boxes, width, height };
}
"""

_ANNOTATE_JS = """
({ boxes, color, layerId }) => {
  document.getElementById(layerId)?.remove();
  const layer = document.createElement('div');
  layer.id = layerId;
  Object.assign(layer.style, {
    position: 'absolute', top: '0', left: '0', width: '100%',
    height: document.documentElement.scrollHeight + 'px',
    pointerEvents: 'none', zIndex: '2147483647',
  });
  for (const [id, b] of Object.entries(boxes)) {
    const box = document.createElement('div');
    Object.assign(box.style, {
      position: 'absolute', left: b.x + 'px', top: b.y + 'px',
      width: b.w + 'px', height: b.h + 'px',
      border: '2px solid ' + color, background: color + '1f',
      borderRadius: '2px', boxSizing: 'border-box',
    });
    const tag = document.createElement('span');
    tag.textContent = id.replace('node_', '');
    Object.assign(tag.style, {
      position: 'absolute', left: '0', top: '-16px', padding: '0 4px',
      font: '700 11px/16px ui-monospace, SFMono-Regular, Menlo, monospace',
      color: '#fff', background: color, borderRadius: '2px', whiteSpace: 'nowrap',
    });
    box.appendChild(tag);
    layer.appendChild(box);
  }
  (document.body || document.documentElement).appendChild(layer);
  return layer.childElementCount;
}
"""


def measure(page: Any, graph: Any) -> dict[str, Any]:
    """Bounding boxes for every node, in document coordinates."""
    selectors = graph.selector_map()
    layout = page.evaluate(_MEASURE_JS, selectors)

    # `querySelector` can't see into a shadow root and doesn't know Playwright's
    # `:light()` / `>> nth=` syntax, so shadow nodes come back unmeasured. Playwright's
    # engine resolves all of them — one round trip each, but only for the misses, so
    # pages without shadow DOM pay nothing.
    missed = {i: s for i, s in selectors.items() if i not in layout["boxes"]}
    if missed:
        scroll_x, scroll_y = page.evaluate("() => [window.scrollX, window.scrollY]")
        for node_id, selector in missed.items():
            try:
                box = page.locator(selector).first.bounding_box(timeout=250)
            except Exception:  # genuinely unresolvable — that's the report's whole point
                continue
            if box and (box["width"] or box["height"]):
                layout["boxes"][node_id] = {
                    "x": box["x"] + scroll_x, "y": box["y"] + scroll_y,
                    "w": box["width"], "h": box["height"],
                }
    return layout


def annotate(page: Any, layout: dict[str, Any], color: str = BADGE_COLOR) -> int:
    """Draw numbered badges over the live page. Returns how many were drawn."""
    return page.evaluate(
        _ANNOTATE_JS, {"boxes": layout["boxes"], "color": color, "layerId": LAYER_ID}
    )


def clear(page: Any) -> None:
    """Remove the badge layer so it never leaks into a later parse."""
    page.evaluate(f"document.getElementById('{LAYER_ID}')?.remove()")


def screenshot(page: Any, graph: Any, path: str | None = None) -> tuple[bytes, dict[str, Any]]:
    """Clean full-page screenshot plus the measured layout."""
    layout = measure(page, graph)
    png = page.screenshot(full_page=True)
    if path:
        annotate(page, layout)
        page.screenshot(path=path, full_page=True)
        clear(page)
    return png, layout


_TEMPLATE = Template("""<!doctype html>
<meta charset="utf-8">
<title>zerodom — $title</title>
<style>
  :root { color-scheme: dark; --bg:#0d1117; --panel:#161b22; --line:#30363d;
          --text:#e6edf3; --dim:#8b949e; --accent:$color; --fill:$fill; }
  * { box-sizing: border-box; }
  [hidden] { display:none !important; }
  body { margin:0; height:100vh; display:flex; flex-direction:column;
         background:var(--bg); color:var(--text);
         font:13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }

  header { padding:10px 16px; border-bottom:1px solid var(--line); display:flex;
           gap:8px 16px; align-items:baseline; flex-wrap:wrap; }
  header b { font-size:15px; }
  header .logo { align-self:center; margin-right:-6px; }
  header .url { color:var(--dim); font-size:12px; overflow:hidden;
                text-overflow:ellipsis; white-space:nowrap; max-width:46vw; }
  .stats { margin-left:auto; display:flex; gap:14px; align-items:center;
           color:var(--dim); font-size:12px; }
  .stats b { color:var(--text); font-weight:700; }

  main { flex:1; display:flex; min-height:0; }
  #graph { width:44%; min-width:300px; max-width:640px; display:flex;
           flex-direction:column; border-right:1px solid var(--line); }
  #shot { flex:1; overflow:auto; background:var(--panel); position:relative; }

  .toolbar { padding:8px 12px; border-bottom:1px solid var(--line);
             display:flex; gap:8px; flex-wrap:wrap; align-items:center;
             background:var(--panel); }
  #q { flex:1; min-width:140px; padding:5px 9px; border-radius:6px;
       border:1px solid var(--line); background:var(--bg); color:var(--text);
       font:inherit; }
  #q:focus { outline:none; border-color:var(--accent); }
  .chip { padding:3px 9px; border-radius:999px; border:1px solid var(--line);
          background:var(--bg); color:var(--dim); font:inherit; font-size:12px;
          cursor:pointer; }
  .chip:hover { color:var(--text); }
  .chip.on { border-color:var(--accent); color:var(--text); background:$color1f; }
  .chip i { font-style:normal; opacity:.6; }

  #rows { flex:1; overflow:auto; padding:6px 0; }
  .row { display:grid; grid-template-columns:auto auto 1fr; gap:0 8px;
         padding:4px 14px; cursor:pointer; border-left:3px solid transparent;
         align-items:baseline; }
  .row:hover, .row.on { background:#1f6feb26; border-left-color:var(--accent); }
  .row .n { color:var(--accent); }
  .row[data-act="fill"] .n { color:var(--fill); }
  .row .t { color:#79c0ff; }
  .row .l { color:var(--text); word-break:break-word; }
  /* The selector is the noisiest thing on screen and the least often needed —
     it's on the row's tooltip, and one toggle away. */
  .row .s { grid-column:1/-1; color:var(--dim); font-size:11px; opacity:.7;
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap; display:none; }
  body.selectors .row .s { display:block; }  /* not on :hover — 230 rows jumping */
  .row[data-missing] { opacity:.45; font-style:italic; }

  /* Native size by default: fitting a 1280px page into a 900px pane makes the
     page text unreadable, which defeats the point of showing the page. */
  #canvas { position:relative; width:${doc_w}px; max-width:none; }
  #canvas.fit { width:100%; }
  #canvas img { display:block; width:100%; height:auto; }
  /* Outline, not a wash: 230 filled boxes hide the page underneath them. */
  .box { position:absolute; border:1px solid ${color}99; border-radius:2px; }
  .box[data-act="fill"] { border-color:${fill}99; }
  .box .tag { position:absolute; left:0; top:-14px; padding:0 3px; font-size:10px;
              line-height:14px; font-weight:700; color:#fff; background:var(--accent);
              border-radius:2px; display:none; }
  .box[data-act="fill"] .tag { background:var(--fill); }
  /* Badges only when they would be legible — auto below the crowding threshold. */
  body.labels .box .tag, .box.on .tag { display:block; }
  .box.on { border-width:2px; background:$color1f; z-index:5;
            box-shadow:0 0 0 3px $color66, 0 0 0 9999px #000c; }
  .box.on[data-act="fill"] { background:${fill}1f; box-shadow:0 0 0 3px ${fill}66, 0 0 0 9999px #000c; }
  .empty { padding:24px; color:var(--dim); }
</style>
<header>
  <svg class="logo" viewBox="0 0 64 64" width="22" height="22" aria-hidden="true">
    <g fill="currentColor" opacity=".45">
      <circle cx="10" cy="10" r="3"/><circle cx="22" cy="10" r="3"/>
      <circle cx="34" cy="10" r="3"/><circle cx="46" cy="10" r="3"/>
      <circle cx="10" cy="22" r="3"/><circle cx="22" cy="22" r="3"/>
      <circle cx="34" cy="22" r="3"/><circle cx="10" cy="34" r="3"/>
      <circle cx="22" cy="34" r="3"/><circle cx="10" cy="46" r="3"/>
    </g>
    <g fill="none" stroke="$color" stroke-width="3" stroke-linecap="round">
      <path d="M34 46 L52 34 M34 46 L52 56"/>
    </g>
    <g fill="$color">
      <circle cx="34" cy="46" r="5.5"/><circle cx="53" cy="33" r="4.5"/>
      <circle cx="53" cy="57" r="4.5"/>
    </g>
  </svg>
  <b>$title</b><span class="url">$url</span>
  <span class="stats">
    <span><b id="shown">$count</b> shown</span>
    <span><b>$boxed</b>/$count located</span>
    <span><b>$latency</b> ms</span>
    <button class="chip" data-toggle="selectors">selectors</button>
    <button class="chip" data-toggle="labels">badges</button>
    <button class="chip" id="zoom" hidden>fit</button>
  </span>
</header>
<main>
  <div id="graph">
    <div class="toolbar">
      <input id="q" placeholder="filter  (press /)" autocomplete="off">
      $chips
    </div>
    <div id="rows">$rows</div>
  </div>
  <div id="shot">$canvas</div>
</main>
<script>
const boxes = new Map([...document.querySelectorAll('.box')].map(b => [b.dataset.id, b]));
const rows = [...document.querySelectorAll('.row')];
const q = document.getElementById('q'), shown = document.getElementById('shown');

for (const row of rows) {
  const box = boxes.get(row.dataset.id);
  if (!box) continue;
  const on = () => { box.classList.add('on'); row.classList.add('on'); };
  const off = () => { box.classList.remove('on'); row.classList.remove('on'); };
  row.addEventListener('mouseenter', on);
  row.addEventListener('mouseleave', off);
  box.addEventListener('mouseenter', on);
  box.addEventListener('mouseleave', off);
  row.addEventListener('click', () =>
    box.scrollIntoView({ block: 'center', behavior: 'smooth' }));
}

const LABEL_LIMIT = 40;  // above this, badges cover more page than they explain
let filter = 'all', forceLabels = false;

function apply() {
  const term = q.value.trim().toLowerCase();
  let n = 0;
  for (const row of rows) {
    const ok = (filter === 'all' ? true
                : filter === 'missing' ? 'missing' in row.dataset
                : row.dataset.type === filter)
            && (!term || row.textContent.toLowerCase().includes(term));
    row.hidden = !ok;
    boxes.get(row.dataset.id)?.toggleAttribute('hidden', !ok);
    n += ok;
  }
  shown.textContent = n;
  // Narrow the list down and the badges become readable, so bring them back.
  document.body.classList.toggle('labels', forceLabels || n <= LABEL_LIMIT);
}
q.addEventListener('input', apply);
for (const chip of document.querySelectorAll('.chip[data-filter]')) {
  chip.addEventListener('click', () => {
    document.querySelector('.chip[data-filter].on')?.classList.remove('on');
    chip.classList.add('on');
    filter = chip.dataset.filter;
    apply();
  });
}
for (const chip of document.querySelectorAll('.chip[data-toggle]')) {
  chip.addEventListener('click', () => {
    const on = chip.classList.toggle('on');
    if (chip.dataset.toggle === 'labels') forceLabels = on;
    document.body.classList.toggle(chip.dataset.toggle, on);
    apply();
  });
}
// `/` jumps to the filter box; Escape clears it. 230 rows need a way in.
addEventListener('keydown', e => {
  if (e.key === '/' && e.target !== q) { e.preventDefault(); q.focus(); }
  if (e.key === 'Escape') { q.value = ''; q.blur(); apply(); }
});

const canvas = document.getElementById('canvas'), zoom = document.getElementById('zoom');
if (canvas) {
  zoom.hidden = false;
  zoom.addEventListener('click', () => {
    const fit = canvas.classList.toggle('fit');
    zoom.textContent = fit ? '1:1' : 'fit';
    zoom.classList.toggle('on', fit);
  });
}
apply();
</script>
""")


def html_report(graph: Any, png: bytes | None = None, layout: dict[str, Any] | None = None) -> str:
    """One self-contained HTML file: graph on the left, annotated page on the right."""
    esc = html.escape
    boxes = (layout or {}).get("boxes", {})
    doc_w = (layout or {}).get("width") or 1
    doc_h = (layout or {}).get("height") or 1

    rows = []
    kinds: Counter[str] = Counter()
    actions = {}
    for node in graph["nodes"]:
        index = node["id"].removeprefix("node_")
        flags = ("*" if node.get("required") else "") + ("!" if node.get("disabled") else "")
        missing = "" if node["id"] in boxes else ' data-missing="1"'
        kinds[node["type"]] += 1
        actions[node["id"]] = node["action"]
        rows.append(
            f'<div class="row" data-id="{node["id"]}" data-type="{esc(node["type"])}"'
            f' data-act="{node["action"]}"{missing} title="{esc(node["selector"])}">'
            f'<span class="n">[{index}]</span>'
            f'<span class="t">{esc(node["type"])}{flags}</span>'
            f'<span class="l">{esc(repr(node["label"]))}</span>'
            f'<span class="s">{esc(node["selector"])}</span></div>'
        )

    # Filter chips: every tag present, plus "missing" only when something is missing.
    counts = [("all", len(graph["nodes"]))] + kinds.most_common()
    if unlocated := len(graph["nodes"]) - len(boxes):
        counts.append(("missing", unlocated))
    chips = "".join(
        f'<button class="chip{" on" if key == "all" else ""}" data-filter="{key}">'
        f"{esc(key)} <i>{n}</i></button>"
        for key, n in counts
    )

    if png:
        overlays = "".join(
            f'<div class="box" data-id="{node_id}" data-act="{actions.get(node_id, "click")}"'
            f" style=\"left:{b['x'] / doc_w:.4%};top:{b['y'] / doc_h:.4%};"
            f"width:{b['w'] / doc_w:.4%};height:{b['h'] / doc_h:.4%}\">"
            f'<span class="tag">{node_id.removeprefix("node_")}</span></div>'
            for node_id, b in boxes.items()
        )
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode()
        canvas = f'<div id="canvas"><img src="{data_uri}" alt="page">{overlays}</div>'
    else:
        canvas = '<div class="empty">No screenshot captured — run with --html and a browser.</div>'

    meta = graph["metadata"]
    return _TEMPLATE.substitute(
        title=esc(meta["page_title"]),
        url=esc(meta["url"]),
        count=meta["total_interactive_nodes"],
        latency=meta["parsing_latency_ms"],
        boxed=len(boxes),
        color=BADGE_COLOR,
        color1f=BADGE_COLOR + "1f",
        color66=BADGE_COLOR + "66",
        fill=FILL_COLOR,
        doc_w=int(doc_w),
        chips=chips,
        rows="".join(rows) or '<div class="empty">No interactive nodes.</div>',
        canvas=canvas,
    )

