"""
Mock FastAPI SPA server for ZeroDOM demonstration.

Serves a dynamic test SPA that reproduces two common agent failure modes:
  A) CSS :hover dynamic menus that require real cursor positioning
  B) Virtual DOM node churn where static selectors lose references

Plus the T3.2 dynamic-component fixtures (reached directly, no demo flow):
  /virtual   React virtualized list that reorders/recreates between read and click
  /shadow    custom element with an open shadow root (light/shadow label collision)
  /hydration SSR page whose client handler lands late (pending-hydration flag)
  /graph     the interaction graph zerodom parses for any of the above —
             the demo app importing zerodom, not standing beside it

Run with: uvicorn server:app --port 8080
"""
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import pathlib

app = FastAPI(title="ZeroDOM Demo SPA Server")

# Mount static files
static_dir = pathlib.Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Fixture route path -> the static file that serves it (the fixture pages are
# routes, not URLs under /static, so tests exercise real navigation).
ROUTES = {
    "/": "test_spa.html",
    "/virtual": "virtual.html",
    "/shadow": "shadow.html",
    "/hydration": "hydration.html",
}


@app.get("/")
def root():
    """Serve the demo SPA."""
    return FileResponse(static_dir / "test_spa.html")


@app.get("/virtual")
def virtual():
    """React virtualized list — the T3.2 churn fixture."""
    return FileResponse(static_dir / "virtual.html")


@app.get("/shadow")
def shadow():
    """Shadow-DOM web component — the T3.2 resolve fixture."""
    return FileResponse(static_dir / "shadow.html")


@app.get("/hydration")
def hydration():
    """Delayed-hydration page — the T3.2 pending-hydration fixture."""
    return FileResponse(static_dir / "hydration.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": "zerodom-demo"}


@app.get("/graph")
def graph(path: str = "/"):
    """Compact interaction graph zerodom parses for a fixture route.

    The demo app itself importing zerodom (HYPOTHESIS-ACK-PLAN §3.2: the old
    standalone demo didn't import zerodom at all). Raw-HTML parse — no browser
    on the server — so it shows what the parser sees before any JS runs.
    """
    file = ROUTES.get(path)
    if file is None:
        return JSONResponse({"error": f"unknown route {path!r}"}, status_code=404)
    from zerodom import parse_html

    html = (static_dir / file).read_text(encoding="utf-8")
    g = parse_html(html, f"http://localhost:8080{path}")
    return {
        "path": path,
        "nodes": len(g["nodes"]),
        "graph": g.to_compact_text(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8080,
        log_level="info",
        reload=False
    )
