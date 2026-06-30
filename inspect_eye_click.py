"""Focused diagnostic: click row-0's eye icon on Adams, Doreen's Care
Assessment tab. Then dump URL, screenshot, and every visible button so
we can see exactly how the page changed."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from main import BASE_URL, login

ADAMS_DOREEN_ID = "55022624-8111-45c5-8fee-02efdd57fde0"
DETAIL_URL = f"{BASE_URL}/v2/client/client-detail?ClientID={ADAMS_DOREEN_ID}"


def dump_buttons(label, p, limit=80):
    js = (
        "(lim) => Array.from(document.querySelectorAll("
        "'button, a, [role=\"button\"], span.send-icon, "
        "span.material-icons, span.material-icons-outlined, "
        "span.material-icons-round, span.material-symbols-outlined'"
        ")).filter(e => e.offsetParent !== null).slice(0, lim).map(e => ({"
        "tag: e.tagName.toLowerCase(),"
        "text: (e.innerText || '').trim().slice(0, 60),"
        "cls: (e.getAttribute('class') || '').slice(0, 80),"
        "aria: e.getAttribute('aria-label') || '',"
        "title: e.getAttribute('title') || '',"
        "href: e.getAttribute('href') || ''"
        "}))"
    )
    items = p.evaluate(js, limit)
    print(f"\n--- {label}: URL={p.url!r} ({len(items)} visible clickables) ---")
    for i, c in enumerate(items):
        extra = " ".join(
            f"{k}={v!r}" for k, v in c.items() if v and k != "tag"
        )
        print(f"  [{i:>2}] <{c['tag']}> {extra}")


def main() -> int:
    load_dotenv()
    user = os.getenv("CARESMARTZ_USERNAME")
    pwd = os.getenv("CARESMARTZ_PASSWORD")
    if not user or not pwd:
        print("ERROR: missing credentials")
        return 2

    out = Path("debug")
    out.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        print("[*] logging in...")
        login(page, user, pwd)

        print(f"[*] opening {DETAIL_URL}")
        page.goto(DETAIL_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        tab = page.locator(
            '.client-detail-component-nav-link:has-text("Care Assessment")'
        ).first
        tab.wait_for(state="visible", timeout=15_000)
        tab.click()
        page.wait_for_timeout(2500)

        print(f"[*] URL before eye click: {page.url!r}")
        page.screenshot(path=str(out / "before_eye.png"), full_page=True)

        # Click row-0's eye.
        eye = page.locator(
            'tr[data-testid="cs-table-row-0"] span.send-icon'
        ).first
        if not eye.count():
            print("!! no eye on row 0")
            return 1

        print("[*] CLICKING row-0 eye (force=True)...")
        url_before = page.url
        pages_before = set(id(x) for x in ctx.pages)

        eye.click(force=True)
        page.wait_for_timeout(6000)  # generous

        new_pages = [x for x in ctx.pages if id(x) not in pages_before]
        print(f"[*] after 6s wait:")
        print(f"      main page URL: {page.url!r}")
        print(f"      URL changed?   {page.url != url_before}")
        print(f"      new tabs:      {len(new_pages)}")
        print(f"      total tabs:    {len(ctx.pages)}")

        page.screenshot(path=str(out / "after_eye.png"), full_page=True)
        print(f"[*] after-screenshot: {out / 'after_eye.png'}")

        if new_pages:
            np = new_pages[0]
            try:
                np.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            np.screenshot(path=str(out / "after_eye_newtab.png"), full_page=True)
            print(f"[*] new-tab URL:      {np.url!r}")
            print(f"[*] new-tab screenshot: {out / 'after_eye_newtab.png'}")
            dump_buttons("NEW TAB after eye click", np, limit=80)

        dump_buttons("MAIN PAGE after eye click", page, limit=80)

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
