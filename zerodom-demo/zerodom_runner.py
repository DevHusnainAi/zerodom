"""
ZeroDOM Runner: zerodom names the target, raw CDP clicks it

Demonstrates deterministic DOM interaction end to end:
- ZeroDOM parses the live DOM and NAMES the target (find_nodes by label,
  selector resolved server-side — never a stale handle held by the script)
- Raw CDP Input.dispatchMouseEvent for hardware-accurate interactions
- Actions are real input events: no synthetic MouseEvent dispatch

Expected outcome: Clean green success logs
"""
import asyncio
from playwright.async_api import async_playwright

from zerodom import ZeroDOM
from zerodom.parser import find_nodes

BASE_URL = "http://localhost:8080"

GREEN = "\033[92m"
BLUE = "\033[94m"
RESET = "\033[0m"

async def locate_node(page, query):
    """Name the target with zerodom: parse the live DOM, find by label,
    resolve the selector to viewport coordinates for the CDP click."""
    graph = await ZeroDOM.from_page(page, viewport_only=False)
    hits = find_nodes(graph["nodes"], query)
    if not hits:
        return None
    node = hits[0]
    try:
        box = await page.locator(node["selector"]).bounding_box()
    except Exception:
        return None  # node detached mid-churn — caller retries with a fresh parse
    if not box:
        return None
    return {
        "label": node["label"],
        "selector": node["selector"],
        "coords": (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)),
    }

async def cdp_mouse_click(page, client, x, y):
    """Dispatch native mouse events via CDP Input domain."""
    # Move mouse
    await client.send('Input.dispatchMouseEvent', {
        'type': 'mouseMoved',
        'x': x,
        'y': y
    })
    await asyncio.sleep(0.05)
    
    # Mouse down
    await client.send('Input.dispatchMouseEvent', {
        'type': 'mousePressed',
        'x': x,
        'y': y,
        'button': 'left',
        'clickCount': 1
    })
    await asyncio.sleep(0.05)
    
    # Mouse up
    await client.send('Input.dispatchMouseEvent', {
        'type': 'mouseReleased',
        'x': x,
        'y': y,
        'button': 'left',
        'clickCount': 1
    })

async def test_hover_menu_cdp(page):
    """Test CSS :hover menu: zerodom names it, raw CDP positions the cursor."""
    print(f"\n{BLUE}[ZeroDOM] Testing CSS :hover menu via raw CDP...{RESET}")
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    client = await page.context.new_cdp_session(page)
    # Input domain does not require explicit enable in newer CDP versions
    # await client.send('Input.enable')
    
    # Name the nav item with zerodom, then move a real cursor onto it
    nav_info = await locate_node(page, "Products")
    if not nav_info or not nav_info['coords']:
        print("Failed to locate nav item")
        return False
    
    x, y = nav_info['coords']
    
    print(f"[ZeroDOM] Target named: {nav_info['label']!r} ({nav_info['selector']})")
    print(f"[ZeroDOM] Viewport coordinates computed: ({x}, {y})")
    print(f"[ZeroDOM] Raw CDP hardware events dispatched...")
    
    # Move mouse over nav item to trigger CSS :hover
    await cdp_mouse_click(page, client, x, y)
    await asyncio.sleep(0.3)
    
    # Now click the dropdown item that appeared via :hover
    dropdown_info = await locate_node(page, "Open Settings")
    if not dropdown_info or not dropdown_info['coords']:
        print("Failed to locate dropdown target after hover")
        return False
    
    dx, dy = dropdown_info['coords']
    print(f"[ZeroDOM] Dropdown target named: {dropdown_info['label']!r}")
    print(f"[ZeroDOM] Dropdown coordinates: ({dx}, {dy})")
    
    await cdp_mouse_click(page, client, dx, dy)
    await asyncio.sleep(0.5)
    
    # Verify success
    success = await page.evaluate("""
        () => document.getElementById('success-hover').style.display !== 'none'
    """)
    
    if success:
        print(f"{GREEN}[PASS] Target action confirmed: Hover menu opened and clicked via raw CDP{RESET}")
        return True
    else:
        print("Hover action verification failed")
        return False

async def test_node_churn_cdp(page):
    """Test node churn: re-parse with zerodom each attempt, never hold a stale
    handle — the button's DOM clone is replaced every 800ms."""
    print(f"\n{BLUE}[ZeroDOM] Testing node churn via re-parse + raw CDP...{RESET}")
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    client = await page.context.new_cdp_session(page)
    # Input domain does not require explicit enable in newer CDP versions
    # await client.send('Input.enable')
    
    # Type to trigger churn
    input_box = page.locator("#churn-input")
    await input_box.fill("test")
    await asyncio.sleep(1.0)  # Let churn cycles start
    
    # Re-name the button with a fresh zerodom parse on every attempt — the
    # parse sees the *current* clone, so coordinates never go stale.
    for attempt in range(3):
        button_info = await locate_node(page, "Submit")
        if not button_info or not button_info['coords']:
            print(f"Attempt {attempt+1}: Button not found")
            await asyncio.sleep(0.3)
            continue
        
        x, y = button_info['coords']
        
        print(f"[ZeroDOM] Target named: {button_info['label']!r} ({button_info['selector']})")
        print(f"[ZeroDOM] Viewport coordinates computed: ({x}, {y})")
        print(f"[ZeroDOM] Raw CDP hardware events dispatched...")
        
        await cdp_mouse_click(page, client, x, y)
        await asyncio.sleep(0.5)
        
        # Check success
        success = await page.evaluate("""
            () => document.getElementById('success-churn').style.display !== 'none'
        """)
        
        if success:
            print(f"{GREEN}[PASS] Target action confirmed: Churn button clicked despite node recreation{RESET}")
            return True
    
    print("Failed to click churn button after retries")
    return False

async def main():
    print("="*70)
    print("ZERODOM RUNNER - zerodom naming + raw CDP input events")
    print("="*70)
    print("Actions are real CDP input events — no synthetic MouseEvent dispatch")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1280, 'height': 800})
        page = await context.new_page()
        
        hover_ok = await test_hover_menu_cdp(page)
        churn_ok = await test_node_churn_cdp(page)
        
        await browser.close()
    
    print("\n" + "="*70)
    if hover_ok and churn_ok:
        print(f"{GREEN}ZERODOM PASSED - Deterministic interactions succeeded{RESET}")
        print("="*70)
    else:
        print("ZeroDOM runner failed")
        print("="*70)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
