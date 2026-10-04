"""
Baseline failure demo: Standard Playwright agent using synthetic events / page.evaluate()

This script demonstrates the common failure modes:
1. CSS :hover menus do NOT open with synthetic MouseEvent dispatch
2. Node churn causes stale element references

Expected outcome: Timeout / element occlusion errors, red failure logs
"""
import asyncio
import sys
from playwright.async_api import async_playwright

BASE_URL = "http://localhost:8080"

RED = "\033[91m"
RESET = "\033[0m"

async def fail_hover_menu(page):
    """Attempt to open hover menu via synthetic event dispatch."""
    print("\n[Baseline] Attempting CSS :hover menu via synthetic event...")
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    
    # Try synthetic mouseover via evaluate - this does NOT trigger CSS :hover
    try:
        await page.evaluate("""
            () => {
                const navItem = document.querySelector('.nav-item');
                const event = new MouseEvent('mouseover', { bubbles: true, cancelable: true });
                navItem.dispatchEvent(event);
            }
        """)
        await page.wait_for_timeout(500)
        
        # Check if dropdown is visible
        is_visible = await page.evaluate("""
            () => {
                const dropdown = document.querySelector('.dropdown');
                if (!dropdown) return false;
                const style = window.getComputedStyle(dropdown);
                return style.display === 'block';
            }
        """)
        
        if not is_visible:
            print(f"{RED}[FAIL] Synthetic mouseover did NOT open CSS :hover menu{RESET}")
            print("Reason: CSS :hover requires real pointer positioning, not synthetic events")
            return False
        else:
            print("[UNEXPECTED] Menu opened with synthetic event")
            return True
    except Exception as e:
        print(f"{RED}[FAIL] Exception during synthetic hover: {e}{RESET}")
        return False

async def fail_node_churn(page):
    """Attempt to click churn button after triggering node recreation."""
    print("\n[Baseline] Attempting node churn interaction via static selector...")
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    
    try:
        # Start typing to trigger churn
        input_box = page.locator("#churn-input")
        await input_box.fill("test")
        await page.wait_for_timeout(1000)  # Let churn start
        
        # Try to get button via static selector - will be stale
        button = page.locator("#churn-button")
        await page.wait_for_timeout(1000)  # Wait for churn cycle
        
        # This will likely fail with stale element or click intercepted
        await button.click(timeout=2000)
        print("[UNEXPECTED] Click succeeded despite node churn")
        return True
    except Exception as e:
        print(f"{RED}[FAIL] Node churn caused failure: {e}{RESET}")
        print("Reason: Static selector references stale DOM node after React-style re-render")
        return False

async def main():
    print("="*70)
    print("BASELINE FAILURE DEMO - Synthetic Events / Static Selectors")
    print("="*70)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        hover_ok = await fail_hover_menu(page)
        churn_ok = await fail_node_churn(page)
        
        await browser.close()
    
    print("\n" + "="*70)
    if not hover_ok or not churn_ok:
        print(f"{RED}BASELINE FAILED AS EXPECTED - Synthetic approaches cannot handle these interactions{RESET}")
        print("="*70)
        sys.exit(1)
    else:
        print("Baseline unexpectedly succeeded")
        sys.exit(0)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)
