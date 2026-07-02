"""
download-targeter2 — mass-download Care Assessment PDFs from Caresmartz360.

Flow per run:
  1. Log in (creds from .env).
  2. Walk the Client List for each status (Active + Inactive), every page,
     and collect every (name, ClientID) pair.
  3. For each client: open the detail page, click the Care Assessment tab,
     pre-walk every inner page to catalog (assessment_id, status), then
     download each assessment by clicking the row's eye (visibility) icon.
     The eye click either navigates this tab to a modern detail page with
     a PDF download button, OR pops a new tab with a legacy CSV export —
     both are handled.

Files land at:
    <out>/<active|inactive>/[_FLAG_|_IN_PROGRESS_]<LastName_FirstName>_<N>/
        <LastName_FirstName>_<assessment_id>_<pos>of<N>.<pdf|csv>

  * <N> is the total assessment count for that client (dynamic,
    read from the live Care Assessment table).
  * <pos> is the row's 1-based position in pre-walk order,
    zero-padded so the folder sorts naturally.

The folder name is prefixed:
  * "_IN_PROGRESS_" if any row has status "In-Progress" (those rows
    are SKIPPED — their action icon is a trash can, not an eye).
  * "_FLAG_"        if any row is non-Completed for some other
    reason (e.g. Pending, Submitted).
  * otherwise unprefixed (all rows Completed).

Already-saved files are skipped (resume-on-rerun), based on the
client+assessment-id prefix matching any existing file in the
client folder. Old-style filenames are migrated to the new
"<pos>of<N>" convention on the next run.

Run:
    python main.py                          # both statuses, headed
    python main.py --status Inactive        # inactive only
    python main.py --headless               # production mode
    python main.py --out downloads          # change output folder
    python main.py --limit 5                # only first 5 clients (debug)
    python main.py --client-id <guid>       # single client by ID (debug)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# ─── constants ────────────────────────────────────────────────────────────

BASE_URL = "https://hospall.caresmartz360.com"
LOGIN_URL = f"{BASE_URL}/Login.aspx"
CLIENT_LIST_URL = f"{BASE_URL}/v2/client/client-list"

NAME_LINK_SELECTOR = 'a[id^="fullname-"]'
STATUS_TRIGGER_SELECTOR = '[data-testid="cs-select-trigger"]'
CARE_TAB_SELECTOR = '.client-detail-component-nav-link:has-text("Care Assessment")'

# Care Assessment table — the row's Action column has an eye icon
# (`<span class="... material-icons send-icon">visibility</span>`) and a
# CMS-485 button. The eye is the per-assessment open/view trigger.
ASSESSMENT_ROW_SELECTOR = 'tr[data-testid^="cs-table-row-"]'
ASSESSMENT_ID_CELL_SELECTOR = 'span.client-detail-assessment-id'
ASSESSMENT_STATUS_CELL_SELECTOR = (
    'td[data-testid$="-assessmentStatus"] span, '
    'td.cdk-column-assessmentStatus span'
)
ASSESSMENT_EYE_ICON_SELECTOR = (
    'span.send-icon, '
    'span.material-icons:text-is("visibility"), '
    'span.material-icons-outlined:text-is("visibility")'
)

DOWNLOAD_BUTTON_SELECTOR = (
    'button:has-text("Download"), a:has-text("Download"), '
    'button:has-text("Print"),    a:has-text("Print"), '
    'button:has-text("PDF"),      a:has-text("PDF"), '
    'button:has-text("Export"),   a:has-text("Export"), '
    'button:has-text("CSV"),      a:has-text("CSV")'
)

# Modern assessments render the detail view INLINE on the same URL
# (Angular component swap). A "Back" button appears next to the
# Download button as the way back to the assessment table.
BACK_BUTTON_SELECTOR = (
    'button:has-text("Back"), a:has-text("Back")'
)

INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Toggled by --debug-paginator. When True, _find_paginator_next dumps a
# screenshot + visible paginator-like button info the first time it can't
# find a next button. Useful for discovering custom paginator markup.
_DEBUG_PAGINATOR = False
_DEBUG_PAGINATOR_DUMPED = False

# Toggled by --debug-downloads. When True, every failed eye-click /
# in-place-download attempt writes a screenshot + page-state dump
# under `debug/<client_id>_<file_stem>_<reason>.{png,txt}`.
_DEBUG_DOWNLOADS = False


@dataclass
class Client:
    status: str
    name: str
    client_id: str
    href: str


# ─── filename helpers ─────────────────────────────────────────────────────


def safe_filename(s: str, fallback: str = "untitled") -> str:
    s = INVALID_FS.sub("_", s or "").strip(" .")
    s = re.sub(r"\s+", " ", s)
    return s[:120] or fallback


def name_to_folder(display_name: str) -> str:
    """'Adams, Doreen' -> 'Adams_Doreen'."""
    parts = [p.strip() for p in display_name.split(",")]
    if len(parts) == 2:
        last, first = parts
        return safe_filename(f"{last}_{first}".replace(" ", "_"))
    return safe_filename(display_name.replace(" ", "_"))


# ─── login + outer (client list) walk ─────────────────────────────────────


def login(page: Page, username: str, password: str) -> None:
    """
    Sign in via the legacy WebForms login page.

    Selectors are intentionally generic so they survive minor DOM tweaks.
    If the site ever changes its form, regenerate them with:
        playwright codegen https://hospall.caresmartz360.com/Login.aspx
    """
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    user_input = page.locator(
        'input[type="text"], input[type="email"], input[name*="ser" i]'
    ).first
    user_input.wait_for(state="visible", timeout=15_000)
    user_input.fill(username)

    pwd_input = page.locator('input[type="password"]').first
    pwd_input.fill(password)

    submit = page.locator(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Sign In"), button:has-text("Login"), button:has-text("Log In")'
    ).first
    if submit.count():
        submit.click()
    else:
        pwd_input.press("Enter")

    page.wait_for_url(re.compile(r"/v2/"), timeout=30_000)


def _find_dropdown_option(page: Page, desired: str) -> Locator:
    """
    Locate an option named `desired` in whatever overlay is currently open.

    `cs-select` is a custom Caresmartz component — it does NOT necessarily
    render into Angular Material's `.cdk-overlay-pane`. We try a sequence
    of locators that cover the common variants. On total failure, we save
    a screenshot and a list of visible option-like texts so the selector
    can be tuned without re-running blind.
    """
    text_re = re.compile(rf"^\s*{re.escape(desired)}\s*$", re.I)
    candidates: list[Locator] = [
        page.locator(".cdk-overlay-pane").get_by_text(text_re),
        page.locator(
            '[class*="cs-select-panel"], [class*="cs-select-option"], '
            '[class*="cs-option"], [class*="select-option"], '
            '.mat-mdc-option, .mat-option'
        ).filter(has_text=text_re),
        page.get_by_role("option", name=text_re),
        page.locator('li, [role="option"]').filter(has_text=text_re),
        # Last resort: any visible exact-text match. Relies on the option
        # being the only place this text appears while the overlay is open.
        page.get_by_text(text_re),
    ]
    for cand in candidates:
        if cand.first.count() == 0:
            continue
        try:
            cand.first.wait_for(state="visible", timeout=2_000)
            return cand.first
        except PlaywrightTimeoutError:
            continue

    # Dump diagnostics so the user can see what the overlay actually contains.
    debug_dir = Path("debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    shot = debug_dir / f"overlay_missing_{safe_filename(desired)}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception:
        shot = None

    visible_options: list[str] = []
    for sel in (
        '[role="option"]',
        '[class*="cs-option"]',
        '[class*="cs-select-option"]',
        '.mat-mdc-option',
        '.mat-option',
        'li',
    ):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 30)):
                if loc.nth(i).is_visible():
                    txt = (loc.nth(i).inner_text() or "").strip()
                    if txt and txt not in visible_options:
                        visible_options.append(txt)
        except Exception:
            continue

    msg = [
        f"Could not find option {desired!r} in the open dropdown.",
        f"Screenshot: {shot}" if shot else "Screenshot: (failed to capture)",
        f"Visible option-like texts on page: {visible_options[:30]}",
        "Inspect the screenshot and update _find_dropdown_option selectors.",
    ]
    raise RuntimeError("\n  ".join(msg))


def _find_status_trigger(page: Page) -> Locator:
    """
    Pick the `cs-select-trigger` that controls the Active/Inactive status
    filter. The page has several `cs-select-trigger` elements (agency,
    status, care coordinator, page-size, etc.), so we identify the right
    one by the text it currently shows. Two cases:

      1. An option is selected → trigger shows "Active" / "Inactive"
         (alongside the chevron icon's "expand_more" textContent).
      2. Nothing is selected → trigger shows the placeholder
         "Select Status" (rendered inside a `cs-no-selected-header`
         wrapper). The other dropdowns' placeholders are "Select Office",
         "Select Care Coordinator", a bare page-size number, etc. —
         none contain the token "status".
    """
    page.wait_for_selector(STATUS_TRIGGER_SELECTOR, timeout=15_000)
    triggers = page.locator(STATUS_TRIGGER_SELECTOR)
    icon_words = {"expand_more", "expand_less", "arrow_drop_down", "chevron_right"}
    for i in range(triggers.count()):
        t = triggers.nth(i)
        try:
            txt = (t.inner_text() or "").strip()
        except Exception:
            continue
        tokens = [w.lower() for w in txt.split() if w.lower() not in icon_words]
        # Substring check on "status" — placeholder is "Status(s)"
        # (with parens), not bare "status".
        if (
            "active" in tokens
            or "inactive" in tokens
            or any("status" in tok for tok in tokens)
        ):
            return t
    seen = []
    for i in range(triggers.count()):
        try:
            seen.append((triggers.nth(i).inner_text() or "").strip())
        except Exception:
            seen.append("<unreadable>")
    raise RuntimeError(
        "Could not find the Active/Inactive status filter trigger.\n"
        f"  Triggers found ({len(seen)}): {seen}"
    )


def set_status_filter(page: Page, desired: str) -> None:
    """
    Switch the `cs-select` status dropdown to "Active" or "Inactive".

    Material gotchas guarded against:
      1. Skip the click if the trigger already shows the desired value —
         re-toggling some Material selects empties the filter.
      2. Never press Escape between option click and table repaint —
         Material discards the pending change.
    """
    trigger = _find_status_trigger(page)
    trigger.wait_for(state="visible", timeout=15_000)
    # Early-return ONLY if the trigger currently shows the desired option
    # value. The trigger text is e.g. "Active\nexpand_more" (icon text is
    # appended), so a strict equality check never matches. Tokenize and
    # check membership. NEVER early-return on the placeholder
    # "Select Status" — that means nothing is selected and we must apply
    # the filter.
    icon_words = {"expand_more", "expand_less", "arrow_drop_down", "chevron_right"}
    raw = (trigger.inner_text() or "").strip()
    tokens = [w.lower() for w in raw.split() if w.lower() not in icon_words]
    if desired.lower() in tokens:
        return

    first = page.locator(NAME_LINK_SELECTOR).first
    snapshot = first.get_attribute("href") if first.count() else None

    trigger.click()
    # Let the overlay render before searching for the option.
    page.wait_for_timeout(300)

    option = _find_dropdown_option(page, desired)
    option.click()
    page.wait_for_timeout(300)

    # The cs-select overlay does not auto-close on option click; its
    # transparent cdk-overlay-backdrop stays in the DOM and intercepts
    # clicks aimed at the Apply button. Dismiss the backdrop first.
    # Clicking the backdrop is the standard "dismiss" gesture and does
    # NOT discard the staged selection (the option click already updated
    # the cs-select's internal state).
    backdrop = page.locator(".cdk-overlay-backdrop").first
    if backdrop.count():
        try:
            backdrop.click(force=True, timeout=2_000)
            page.wait_for_timeout(200)
        except PlaywrightTimeoutError:
            pass

    apply_btn = page.get_by_role("button", name=re.compile(r"^Apply$", re.I))
    if apply_btn.count() and apply_btn.first.is_visible():
        apply_btn.first.click()

    try:
        page.wait_for_function(
            """([prev, sel]) => {
                const a = document.querySelector(sel);
                if (!a) return false;
                return prev === null || a.getAttribute('href') !== prev;
            }""",
            arg=[snapshot, NAME_LINK_SELECTOR],
            timeout=20_000,
        )
    except PlaywrightTimeoutError:
        pass


def _find_paginator_next(page: Page) -> Locator | None:
    """
    Locate the "next page" control on whatever paginated widget is
    currently visible.

    Three paginator shapes are supported:
      A. Angular Material's `mat-paginator` — uses real `<button>`s
         with `aria-label="Next page"`.
      B. Caresmartz custom `<table-paginator>` — uses
         `<div class="right-left-icon">` wrappers around
         `<span class="material-icons-outlined">chevron_right</span>`.
         IMPORTANT: these are NOT `<button>` / `<a>` / `[role=button]`,
         and disabled state is signaled by a `--disabled` class token
         (NOT the standard `disabled` attribute or `aria-disabled`).
      C. Generic chevron_right inside a paginator-like container, as a
         last resort.

    Returns the locator for an enabled, visible next-control, or None.
    """
    strategies: list[Locator] = [
        # A. Material paginator.
        page.locator(
            'button.mat-mdc-paginator-navigation-next, '
            'button[aria-label="Next page"], '
            'button[aria-label*="next" i]'
        ),
        # B. Caresmartz <table-paginator>.
        page.locator(
            'div.right-left-icon:has('
            'span.material-icons-outlined:has-text("chevron_right"))'
        ),
        # C. Generic chevron_right inside an explicit paginator container.
        page.locator(
            ':is([class*="pagination"], [class*="paginator"], '
            'nav, [role="navigation"])'
        ).locator(
            ':is(button, a, [role="button"], div)'
            ':has(span.material-icons-outlined:has-text("chevron_right"))'
        ),
    ]

    for strat in strategies:
        for i in range(strat.count()):
            b = strat.nth(i)
            try:
                if not b.is_visible():
                    continue
            except Exception:
                continue
            if (
                b.get_attribute("disabled") is not None
                or b.get_attribute("aria-disabled") == "true"
            ):
                continue
            classes = (b.get_attribute("class") or "").split()
            if "--disabled" in classes or "disabled" in classes:
                continue
            return b
    _maybe_dump_paginator_debug(page)
    return None


def _maybe_dump_paginator_debug(page: Page) -> None:
    """Save screenshot + paginator-like button info on the first failure."""
    global _DEBUG_PAGINATOR_DUMPED
    if not _DEBUG_PAGINATOR or _DEBUG_PAGINATOR_DUMPED:
        return
    _DEBUG_PAGINATOR_DUMPED = True

    debug_dir = Path("debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    shot = debug_dir / "paginator_missing.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
        print(f"[debug-paginator] screenshot: {shot}")
    except Exception as e:
        print(f"[debug-paginator] screenshot failed: {e}")

    keywords = ("page", "next", "prev", "chevron", "arrow", "pagin", "footer")
    btns = page.locator("button, a, [role='button']")
    n = btns.count()
    print(f"[debug-paginator] scanning {n} clickable elements...")
    hits: list[dict[str, str]] = []
    for i in range(min(n, 300)):
        b = btns.nth(i)
        try:
            if not b.is_visible():
                continue
            label = (b.get_attribute("aria-label") or "").strip()
            cls = (b.get_attribute("class") or "")[:80]
            txt = (b.inner_text() or "").strip().replace("\n", " | ")[:60]
            blob = f"{label} {cls} {txt}".lower()
            if any(k in blob for k in keywords):
                hits.append({"aria": label, "text": txt, "class": cls})
        except Exception:
            continue

    print(f"[debug-paginator] {len(hits)} paginator-like element(s):")
    for h in hits[:40]:
        print(f"    aria='{h['aria']}' text='{h['text']}' class='{h['class']}'")

    # Also dump any element whose text matches "Showing N - M of K".
    summary = page.get_by_text(re.compile(r"showing\s+\d+", re.I))
    if summary.count():
        for i in range(min(summary.count(), 5)):
            try:
                txt = (summary.nth(i).inner_text() or "").strip()
                print(f"[debug-paginator] summary text: {txt!r}")
            except Exception:
                pass

        # Dump the HTML of the paginator's likely container (an ancestor
        # of the "Showing" summary that also contains some <button>s or
        # <a>s — that ancestor is the row holding the page controls).
        try:
            html = summary.first.evaluate(
                """el => {
                    let cur = el;
                    for (let i = 0; i < 8 && cur; i++) {
                        if (cur.parentElement) cur = cur.parentElement;
                        if (cur.querySelectorAll('button, a, [role="button"], li').length >= 2)
                            return cur.outerHTML;
                    }
                    return cur ? cur.outerHTML : '';
                }"""
            )
            html_path = debug_dir / "paginator_container.html"
            html_path.write_text(html or "", encoding="utf-8")
            print(f"[debug-paginator] container HTML: {html_path} ({len(html)} chars)")
        except Exception as e:
            print(f"[debug-paginator] container HTML dump failed: {e}")


def click_paginator_next(page: Page, snapshot_selector: str) -> bool:
    """
    Advance the paginator on the currently visible table.

    `snapshot_selector` MUST be a plain-CSS selector pointing at an
    element inside the table body whose value (href or text) differs
    between pages. We use Playwright's `Locator` API for the snapshot
    and the wait loop — NEVER `page.wait_for_function(... querySelector
    ...)`, because that injects raw JS where Playwright-specific syntax
    like `:has-text(...)` is rejected as invalid CSS.

    Returns False if no enabled next-button is found (last page) OR if
    the table content didn't change after the click.
    """
    next_btn = _find_paginator_next(page)
    if next_btn is None:
        return False

    first = page.locator(snapshot_selector).first
    snapshot: str | None = None
    if first.count():
        snapshot = first.get_attribute("href") or (first.inner_text() or "").strip()

    next_btn.click()

    # Poll up to ~15s for the first matching element's value to change.
    deadline_ms = 15_000
    poll_ms = 200
    elapsed = 0
    while elapsed < deadline_ms:
        try:
            cur_first = page.locator(snapshot_selector).first
            if cur_first.count():
                cur = cur_first.get_attribute("href") or (
                    cur_first.inner_text() or ""
                ).strip()
                if snapshot is None or cur != snapshot:
                    return True
        except Exception:
            pass
        page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    return False


def collect_clients(page: Page, statuses: list[str]) -> list[Client]:
    """Walk Active/Inactive client lists once and return every client."""
    found: list[Client] = []
    for status in statuses:
        print(f"[*] Walking {status} client list...")
        set_status_filter(page, status)
        page.wait_for_selector(NAME_LINK_SELECTOR, timeout=20_000, state="attached")
        page_no = 1
        while True:
            anchors = page.locator(NAME_LINK_SELECTOR)
            count = anchors.count()
            print(f"    page {page_no}: {count} clients")
            for i in range(count):
                a = anchors.nth(i)
                href = a.get_attribute("href") or ""
                name = (a.inner_text() or "").strip()
                client_id = ""
                if href:
                    qs = parse_qs(urlparse(href).query)
                    client_id = (qs.get("ClientID") or [""])[0]
                if not client_id:
                    continue
                found.append(
                    Client(status=status, name=name, client_id=client_id, href=href)
                )
            if not click_paginator_next(page, NAME_LINK_SELECTOR):
                break
            page_no += 1
    return found


# ─── inner: Care Assessment PDF downloads per client ───────────────────────


def open_care_assessment_tab(page: Page, client: Client) -> bool:
    """Navigate to the client detail page and activate the Care Assessment tab."""
    href = client.href or f"/v2/client/client-detail?ClientID={client.client_id}"
    url = f"{BASE_URL}{href}" if href.startswith("/") else href
    page.goto(url, wait_until="domcontentloaded")

    tab = page.locator(CARE_TAB_SELECTOR).first
    if not tab.count():
        # Fallback: any element whose visible text matches exactly.
        tab = page.get_by_text(
            re.compile(r"^\s*Care Assessment\s*$", re.I)
        ).first
    try:
        tab.wait_for(state="visible", timeout=15_000)
        tab.click()
    except PlaywrightTimeoutError:
        return False

    # Give the inner table time to render. Either we find assessment
    # rows (assessments exist) or we time out (empty state — OK).
    try:
        page.wait_for_selector(
            ASSESSMENT_ROW_SELECTOR, timeout=10_000, state="visible"
        )
    except PlaywrightTimeoutError:
        pass
    return True


def _row_assessment_id(row: Locator) -> str:
    cell = row.locator(ASSESSMENT_ID_CELL_SELECTOR).first
    if not cell.count():
        return ""
    return (cell.inner_text() or "").strip()


def _row_status(row: Locator) -> str:
    cell = row.locator(ASSESSMENT_STATUS_CELL_SELECTOR).first
    if not cell.count():
        return ""
    return (cell.inner_text() or "").strip()


def collect_assessment_rows(page: Page) -> list[dict]:
    """
    Pre-walk every inner page of the Care Assessment table.

    Returns a list of dicts:
        [{"assessment_id": "1845", "status": "Completed", "page_no": 1}, ...]

    Used to:
      1. Decide whether to flag the client's folder name (any row with
         status != "Completed" -> flag).
      2. Drive the download loop deterministically by assessment ID, so
         we don't get confused when the page re-renders after a download.
    """
    rows: list[dict] = []
    page_no = 1
    while True:
        trs = page.locator(ASSESSMENT_ROW_SELECTOR)
        n = trs.count()
        print(f"        inner page {page_no}: {n} row(s)")
        for i in range(n):
            tr = trs.nth(i)
            rows.append(
                {
                    "assessment_id": _row_assessment_id(tr),
                    "status": _row_status(tr),
                    "page_no": page_no,
                }
            )
        if not click_paginator_next(page, ASSESSMENT_ID_CELL_SELECTOR):
            break
        page_no += 1
    return rows


def _current_paginator_page(page: Page) -> int:
    """
    Read the currently-selected inner page number from the custom
    paginator (the `<div class="index__item selected">` element).
    Returns -1 when the paginator isn't visible or the label isn't
    numeric. Used to decide whether we can reuse the current table
    view instead of doing a full re-navigation.
    """
    try:
        selected = page.locator("div.index__item.selected").first
        if not selected.count():
            return -1
        raw = (selected.inner_text() or "").strip()
        return int(raw)
    except (ValueError, TypeError, Exception):  # noqa: BLE001
        return -1


def _navigate_to_inner_page(page: Page, client: Client, target_page_no: int) -> bool:
    """
    Get the Care Assessment table showing inner page `target_page_no`.

    Fast paths (avoid a full page.goto + Care Assessment tab click):

      1. If the table is visible AND the paginator already shows
         `target_page_no`, return immediately.
      2. If the table is visible AND we're on an earlier page,
         click paginator-next until we reach `target_page_no`.

    Slow path (fall-back): re-open the client detail URL, click
    Care Assessment, then paginate from page 1.
    """
    trs_visible = page.locator(ASSESSMENT_ROW_SELECTOR).count() > 0
    current = _current_paginator_page(page) if trs_visible else -1

    if trs_visible and current == target_page_no:
        return True

    if trs_visible and 0 < current < target_page_no:
        # Just click next until we arrive.
        for _ in range(target_page_no - current):
            if not click_paginator_next(page, ASSESSMENT_ID_CELL_SELECTOR):
                # Fell short; drop to full re-nav below.
                break
            if _current_paginator_page(page) == target_page_no:
                return True
        if _current_paginator_page(page) == target_page_no:
            return True

    # Slow path: full reload of Care Assessment then paginate from 1.
    if not open_care_assessment_tab(page, client):
        return False
    for _ in range(max(0, target_page_no - 1)):
        if not click_paginator_next(page, ASSESSMENT_ID_CELL_SELECTOR):
            return False
    return True


def _try_download_on_page(
    target_page: Page, save_dir: Path, file_stem: str
) -> Path | None:
    """
    Find a download/print/export button on `target_page` and use it
    inside `expect_download`. Returns the saved Path on success.

    Honors the server's suggested filename extension so legacy CSV
    exports land as `.csv` and modern PDF exports as `.pdf`.
    `file_stem` is already sanitized (no extension).
    """
    target_page.wait_for_timeout(800)  # let the detail page render its actions
    btn = target_page.locator(DOWNLOAD_BUTTON_SELECTOR).first
    if not btn.count():
        return None
    try:
        with target_page.expect_download(timeout=20_000) as dl_info:
            btn.click()
        dl = dl_info.value
        suggested = dl.suggested_filename or ""
        ext = Path(suggested).suffix.lower() or ".pdf"
        save_path = save_dir / f"{file_stem}{ext}"
        dl.save_as(str(save_path))
        return save_path
    except PlaywrightTimeoutError:
        return None


# SSRS ReportViewer (classic ASP.NET) toolbar — the export icon is a
# floppy-disk image with a small dropdown arrow. The control renders
# differently across SSRS versions, so we try several candidate
# selectors. The dropdown opens a menu with options like
# "Word / Excel / PowerPoint / PDF / TIFF file / MHTML / CSV / XML".
#
# Observed on Caresmartz360 (ReportViewer 11.0.3452.0): the anchor
# looks like:
#   <a id="ReportViewerSummary_..._ButtonLink"
#      title="Export drop down menu"   <-- SPACES, not hyphens
#      href="javascript:void(0)">
#     <img alt="Export drop down menu" .../>
#     <img alt="Export drop down menu" src=".../ArrowDo..."/>
#   </a>
# So the ORIGINAL "drop-down" selectors never matched — the working
# fallback is `title*="Export" i]`. Keep both spellings for safety.
SSRS_EXPORT_TOGGLE_SELECTORS = (
    'a[title="Export drop down menu"]',
    'a[title="Export drop-down menu"]',
    'a[title*="drop down" i]',
    'a[title*="drop-down" i]',
    'input[type="image"][title*="Export drop" i]',
    'a[title="Export"]',
    'a[title*="Export" i]',
    'input[type="image"][title*="Export" i]',
    'input[type="image"][title*="Save" i]',
    'a[title*="Save" i]',
    # Classic ReportViewer (no title attr — only the icon src tells
    # you what the button is). "SaveSplitDown" is the dropdown
    # arrow; "Save" alone is the default-format button. We prefer
    # the dropdown arrow when present so the user reliably gets PDF
    # rather than whatever the default is.
    'input[type="image"][src*="SaveSplitDown" i]',
    'input[type="image"][src*="Save" i]',
    'img[src*="SaveSplitDown" i]',
    'img[src*="Save" i]',
)
SSRS_PDF_OPTION_SELECTORS = (
    # Exact-title matches first (most reliable). Classic SSRS 2008+
    # renders the PDF option as: `<a title="Acrobat (PDF) file">...</a>`.
    'a[title="Acrobat (PDF) file"]',
    'a[title*="Acrobat" i]',
    'a[title="PDF"]',
    'a[title*="PDF" i]',
    # Text-based fallbacks scoped to <a> so we can't accidentally
    # grab a giant ancestor <td> that ALSO contains the PDF anchor
    # (e.g. the whole toolbar) — that's what happened when we used
    # `td:has-text("PDF")` and the click landed on the wrong node.
    'a:text-is("PDF")',
    'a:text-is("Acrobat (PDF) file")',
    'a:has-text("Acrobat (PDF) file")',
    'a:has-text("PDF")',
)


def _ssrs_wait_ready(target_page: Page, timeout_ms: int = 30_000) -> bool:
    """
    Poll the SSRS ReportViewer component until it reports it's done
    loading, so a subsequent `exportReport('PDF')` call doesn't throw
    `Sys.InvalidOperationException: The report or page is being
    updated.`

    Readiness signal (in order of preference):
        1. `rv.get_isLoading()` returns false.
        2. `rv.get_reportAreaHasReport()` returns true.
        3. Sys.WebForms.PageRequestManager isn't in an AJAX request
           (`get_isInAsyncPostBack()` returns false).

    Returns True on ready-signal, False if the timeout elapsed
    (call-site should still attempt the export — the readiness
    check is best-effort, not a hard gate).
    """
    poll_ms = 250
    elapsed = 0
    last_state: dict | None = None
    while elapsed < timeout_ms:
        try:
            state = target_page.evaluate(
                r"""
                () => {
                    let rv = null;
                    try {
                        if (typeof Sys !== 'undefined' && Sys.Application
                            && typeof Sys.Application.getComponents === 'function') {
                            for (const c of Sys.Application.getComponents()) {
                                if (c && typeof c.exportReport === 'function') {
                                    rv = c;
                                    break;
                                }
                            }
                        }
                    } catch (e) { /* ignore */ }
                    const out = {found: !!rv};
                    if (rv) {
                        try { out.isLoading = rv.get_isLoading ? rv.get_isLoading() : null; } catch (e) { out.isLoading = 'err'; }
                        try { out.hasReport = rv.get_reportAreaHasReport ? rv.get_reportAreaHasReport() : null; } catch (e) { out.hasReport = 'err'; }
                    }
                    try {
                        const prm = (typeof Sys !== 'undefined' && Sys.WebForms
                            && Sys.WebForms.PageRequestManager
                            && typeof Sys.WebForms.PageRequestManager.getInstance === 'function')
                            ? Sys.WebForms.PageRequestManager.getInstance() : null;
                        out.inAsyncPostBack = prm && typeof prm.get_isInAsyncPostBack === 'function' ? prm.get_isInAsyncPostBack() : null;
                    } catch (e) { out.inAsyncPostBack = 'err'; }
                    return out;
                }
                """
            )
        except Exception:  # noqa: BLE001
            state = None

        last_state = state
        if state and state.get("found"):
            is_loading = state.get("isLoading")
            has_report = state.get("hasReport")
            in_async = state.get("inAsyncPostBack")
            # Ready when NOT loading AND NOT in AJAX postback AND
            # (report content is present OR the flag isn't queryable).
            if (
                is_loading is False
                and in_async is not True
                and (has_report is True or has_report is None)
            ):
                print(
                    f"        [ssrs] ready: isLoading=False "
                    f"hasReport={has_report} inAsyncPostBack={in_async}"
                )
                return True
        target_page.wait_for_timeout(poll_ms)
        elapsed += poll_ms

    print(
        f"        [ssrs] ready-wait timed out after {timeout_ms}ms; "
        f"last state = {last_state}"
    )
    return False


def _try_legacy_ssrs_export_pdf(
    target_page: Page, save_dir: Path, file_stem: str
) -> Path | None:
    """
    Drive the SSRS ReportViewer toolbar on the legacy
    ReportSummaryForServicePlan.aspx popup.

    The toolbar has a floppy-disk "Export" icon with a dropdown
    arrow; clicking the arrow reveals a menu of formats. We pick
    "PDF" and capture the download.

    Landmark-verified flow — each step prints its outcome and
    aborts cleanly if it can't verify success before moving on:

        L1: locate + click the floppy-disk / Export toggle
            (frame-aware — SSRS ReportViewer commonly renders
            the toolbar inside a nested <iframe>).
        L2: wait for the export menu to open and reveal PDF.
        L3: click PDF; capture the download or PDF response body.

    Returns the saved Path on success, or None to let the caller
    fall back to the generic `_try_download_on_page`.
    """
    print(f"        [ssrs] popup URL: {target_page.url}")

    # ── Landmark 1: find + click the floppy-disk toggle ────────
    toggle, toggle_frame = _find_ssrs_toggle(target_page, timeout_ms=15_000)
    if toggle is None:
        print("        [ssrs] L1 FAIL: floppy-disk toggle not found")
        _dump_popup_debug(target_page, file_stem, "ssrs_no_toggle")
        return None
    frame_label = (
        "main"
        if toggle_frame is target_page.main_frame
        else f"iframe:{toggle_frame.name or toggle_frame.url[:60]!r}"
    )
    print(f"        [ssrs] L1 OK: toggle found in {frame_label}")
    try:
        toggle.click()
    except Exception as exc:  # noqa: BLE001
        print(f"        [ssrs] L1 FAIL: click raised: {exc}")
        _dump_popup_debug(target_page, file_stem, "ssrs_toggle_click_failed")
        return None
    print("        [ssrs] L1 OK: toggle clicked")

    # ── Landmark 2: wait for the export menu to open + reveal PDF
    pdf_link = _wait_for_ssrs_pdf_option(toggle_frame, timeout_ms=8_000)
    if pdf_link is None:
        # Some builds put the menu in a DIFFERENT frame than the
        # toggle (or in the main frame). Sweep all frames as
        # fallback before giving up.
        pdf_link, pdf_frame = _find_ssrs_pdf_option_any_frame(target_page)
        if pdf_link is None:
            print("        [ssrs] L2 FAIL: PDF option didn't appear")
            _dump_popup_debug(target_page, file_stem, "ssrs_no_pdf_option")
            return None
        print(
            f"        [ssrs] L2 OK: PDF option found in fallback "
            f"frame ({pdf_frame.url[:60]!r})"
        )
    else:
        print("        [ssrs] L2 OK: PDF option visible in export menu")

    # ── Landmark 3: get the PDF and save it ─────────────────────
    save_path = save_dir / f"{file_stem}.pdf"

    # Attempt 3a: many SSRS builds render PDF menu items as real
    # anchors whose `href` is the direct PDF URL. Grab it and use
    # the browser's request client so the session cookies come
    # along. This bypasses onclick JS + `window.open(...)` entirely.
    pdf_href = ""
    try:
        pdf_href = pdf_link.evaluate(
            "(el) => el.getAttribute('href') || el.href || ''"
        ) or ""
    except Exception:  # noqa: BLE001
        pdf_href = ""
    if pdf_href and "javascript:" not in pdf_href.lower():
        # Resolve relative to the popup URL.
        try:
            resolved = pdf_link.evaluate(
                "(el) => new URL(el.getAttribute('href') || el.href, "
                "document.baseURI).toString()"
            )
        except Exception:  # noqa: BLE001
            resolved = pdf_href
        try:
            api = target_page.context.request
            response = api.get(resolved)
            if response.ok:
                body = response.body()
                if body[:4] == b"%PDF":
                    save_path.write_bytes(body)
                    print(
                        f"        [ssrs] L3 OK: direct-fetch -> "
                        f"{save_path.name} ({len(body):,} bytes)"
                    )
                    return save_path
        except Exception as exc:  # noqa: BLE001
            print(f"        [ssrs] L3 direct-fetch failed: {exc}")

    # Attempt 3b: register download + response listeners, THEN click
    # the PDF option. Classic SSRS ReportViewer's `exportReport('PDF')`
    # sends a request whose response has `Content-Disposition:
    # attachment`, which Chromium turns into a DOWNLOAD before the
    # response body is delivered to the renderer. That means
    # `response.body()` in the response listener is EMPTY / unavailable.
    # The reliable capture path is `page.on("download", ...)` — see
    # Playwright docs, downloads are page-level events.
    #
    # Three delivery mechanisms are covered:
    #   (i)  hidden <iframe> appended to target_page → download event
    #        fires on target_page.
    #   (ii) window.open(pdf_url) → a NEW tab opens → download event
    #        fires on that new tab. `ctx.on("page")` wires the
    #        listener before the new tab has a chance to close.
    #   (iii) navigation to a PDF URL (content-disposition: inline)
    #        → the new tab's URL becomes the PDF URL → we fetch it
    #        directly via `ctx.request.get()`.
    ctx = target_page.context
    captured: list[Path] = []
    new_tabs: list[Page] = []

    def _save_download(dl) -> None:
        if captured:
            return
        try:
            suggested = dl.suggested_filename or ""
            # Honor .csv if that's what the server sent, otherwise pdf.
            ext = Path(suggested).suffix.lower() or ".pdf"
            actual = save_dir / f"{file_stem}{ext}"
            dl.save_as(str(actual))
            print(
                f"        [ssrs] L3 download event fired -> "
                f"{actual.name} (suggested={suggested!r})"
            )
            captured.append(actual)
        except Exception as exc:  # noqa: BLE001
            print(f"        [ssrs] L3 download save failed: {exc}")

    def _on_new_page(np: Page) -> None:
        new_tabs.append(np)
        try:
            np.on("download", _save_download)
        except Exception:  # noqa: BLE001
            pass
        try:
            print(f"        [ssrs] L3 new tab opened: {np.url[:100]!r}")
        except Exception:  # noqa: BLE001
            pass

    def _on_response(resp) -> None:
        if captured:
            return
        try:
            url = resp.url.lower()
            ct = (resp.headers.get("content-type") or "").lower()
            if (
                "pdf" not in ct
                and not url.endswith(".pdf")
                and "format=pdf" not in url
            ):
                return
            # If Chromium turned this into a download, body() will
            # raise or return empty. That's fine — the download
            # handler above will catch it. This listener is a
            # last-ditch safety net for the rare case where the
            # server sends `Content-Disposition: inline` and body()
            # actually works.
            body = resp.body()
            if not body or body[:4] != b"%PDF":
                return
            save_path.write_bytes(body)
            print(
                f"        [ssrs] L3 response body captured -> "
                f"{save_path.name} ({len(body):,} bytes)"
            )
            captured.append(save_path)
        except Exception:  # noqa: BLE001
            pass

    target_page.on("download", _save_download)
    ctx.on("page", _on_new_page)
    ctx.on("response", _on_response)

    # Also snoop every request/response on target_page during L3
    # so we can SEE what URLs SSRS's exportReport is (or isn't)
    # hitting. Filter to ReportViewerWebControl.axd + PDF-ish URLs
    # so we don't drown in noise from static-asset traffic.
    seen_urls: list[str] = []

    def _snoop_request(req) -> None:
        u = req.url
        lc = u.lower()
        if (
            "reportviewerwebcontrol" in lc
            or "format=pdf" in lc
            or lc.endswith(".pdf")
        ):
            seen_urls.append(u)
            print(f"        [ssrs] L3 request: {req.method} {u[:180]}")

    target_page.on("request", _snoop_request)

    try:
        # Attempt 3b.i — DIRECT API call. Bypass the menu-opening
        # dance entirely by invoking the ReportViewer's client-side
        # `exportReport('PDF')` method directly. But FIRST wait
        # until the ReportViewer says it's done loading, otherwise
        # exportReport throws:
        #   Sys.InvalidOperationException:
        #     The report or page is being updated.  Please wait
        #     for the current action to complete.
        #
        # We poll the component's `.get_isLoading()` state (falling
        # back to `.get_reportAreaHasReport()` if that isn't
        # available) for up to 30s.
        _ssrs_wait_ready(target_page, timeout_ms=30_000)

        api_result = None
        try:
            api_result = target_page.evaluate(
                r"""
                async () => {
                    // Locate the ReportViewer client component.
                    function findRV() {
                        try {
                            if (typeof Sys !== 'undefined' && Sys.Application
                                && typeof Sys.Application.getComponents === 'function') {
                                for (const c of Sys.Application.getComponents()) {
                                    if (c && typeof c.exportReport === 'function') {
                                        return c;
                                    }
                                }
                            }
                        } catch (e) { /* fall through */ }
                        if (typeof $find === 'function') {
                            const withCtl = document.querySelector('[id*="_ctl"]');
                            if (withCtl) {
                                const m = withCtl.id.match(/^([A-Za-z0-9]+)_ctl/);
                                if (m) {
                                    const r = $find(m[1]);
                                    if (r && typeof r.exportReport === 'function') return r;
                                }
                            }
                            for (const rid of ['ReportViewerSummary', 'ReportViewer', 'ReportViewerCtrl']) {
                                const r = $find(rid);
                                if (r && typeof r.exportReport === 'function') return r;
                            }
                        }
                        return null;
                    }
                    const rv = findRV();
                    if (!rv) return {ok: false, reason: 'no-reportviewer'};
                    const rvId = (typeof rv.get_id === 'function' ? rv.get_id() : (rv.id || '<?>'));

                    // Retry loop: exportReport throws
                    //   "The report or page is being updated"
                    // until the ReportViewer's internal state is
                    // ready. Poll for up to 45s at 500ms intervals.
                    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                    const deadline = Date.now() + 45_000;
                    let lastErr = null;
                    let attempts = 0;
                    while (Date.now() < deadline) {
                        attempts++;
                        try {
                            rv.exportReport('PDF');
                            return {ok: true, method: 'component', reportId: rvId, attempts};
                        } catch (e) {
                            lastErr = (e && e.message) || String(e);
                            // Only retry on the "being updated" error — other
                            // errors are fatal.
                            if (!/being updated|current action/i.test(lastErr)) {
                                return {ok: false, reason: 'exportReport-threw', message: lastErr, reportId: rvId, attempts};
                            }
                        }
                        await sleep(500);
                    }
                    return {ok: false, reason: 'timeout', message: lastErr, reportId: rvId, attempts};
                }
                """
            )
        except Exception as exc:  # noqa: BLE001
            api_result = {"ok": False, "reason": f"eval-raised: {exc}"}

        if api_result and api_result.get("ok"):
            print(
                f"        [ssrs] L3 direct API: "
                f"exportReport('PDF') on {api_result.get('reportId')!r} "
                f"(attempts={api_result.get('attempts')})"
            )
        else:
            print(f"        [ssrs] L3 direct API skipped: {api_result}")

        # Diagnostic — log what the pdf_link is pointing to before
        # any click, so if the click also fails we can see what was
        # actually attempted.
        try:
            info = pdf_link.evaluate(
                "(el) => ({tag: el.tagName.toLowerCase(), "
                "text: (el.innerText || el.textContent || '').trim().slice(0, 60), "
                "href: el.getAttribute('href') || '', "
                "onclick: (el.getAttribute('onclick') || '').slice(0, 120), "
                "title: el.getAttribute('title') || ''})"
            )
            print(f"        [ssrs] L3 pdf_link: {info}")
        except Exception as exc:  # noqa: BLE001
            print(f"        [ssrs] L3 pdf_link introspection failed: {exc}")

        # If the direct API didn't take, DOM-click the anchor
        # (bypasses Playwright's mouse move so the menu doesn't
        # close before the click).
        if not (api_result and api_result.get("ok")):
            try:
                pdf_link.evaluate("(el) => el.click()")
                print("        [ssrs] L3 fallback: DOM click on PDF anchor dispatched")
            except Exception as exc:  # noqa: BLE001
                print(f"        [ssrs] L3 DOM click raised: {exc}")

        deadline_ms = 30_000
        poll_ms = 250
        elapsed = 0
        while elapsed < deadline_ms and not captured:
            target_page.wait_for_timeout(poll_ms)
            elapsed += poll_ms

            # If we saw a PDF-looking request URL fly by but no
            # download event fired, fetch it directly via the
            # context request client (session cookies come along).
            for candidate_url in list(seen_urls):
                if captured:
                    break
                lc = candidate_url.lower()
                if not (
                    "format=pdf" in lc
                    or lc.endswith(".pdf")
                    or "reportviewerwebcontrol" in lc
                ):
                    continue
                try:
                    response = ctx.request.get(candidate_url)
                    if response.ok:
                        body = response.body()
                        if body[:4] == b"%PDF":
                            save_path.write_bytes(body)
                            captured.append(save_path)
                            print(
                                f"        [ssrs] L3 refetched URL -> "
                                f"{save_path.name} ({len(body):,} bytes)"
                            )
                            break
                except Exception as exc:  # noqa: BLE001
                    print(f"        [ssrs] L3 refetch raised: {exc}")

            # Poll new-tab URLs: if one navigated to a PDF-looking
            # URL and no download event fired, fetch it directly
            # via the context's request client (session cookies
            # come along). Covers the inline-render case.
            for np in list(new_tabs):
                if captured:
                    break
                try:
                    nurl = np.url or ""
                except Exception:  # noqa: BLE001
                    continue
                lc = nurl.lower()
                if not (
                    "format=pdf" in lc
                    or lc.endswith(".pdf")
                    or "reportviewerwebcontrol" in lc
                ):
                    continue
                try:
                    response = ctx.request.get(nurl)
                    if response.ok:
                        body = response.body()
                        if body[:4] == b"%PDF":
                            save_path.write_bytes(body)
                            captured.append(save_path)
                            print(
                                f"        [ssrs] L3 new-tab fetch -> "
                                f"{save_path.name} ({len(body):,} bytes)"
                            )
                            break
                except Exception as exc:  # noqa: BLE001
                    print(f"        [ssrs] L3 new-tab fetch raised: {exc}")
    finally:
        try:
            target_page.remove_listener("download", _save_download)
        except Exception:  # noqa: BLE001
            pass
        try:
            target_page.remove_listener("request", _snoop_request)
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.remove_listener("page", _on_new_page)
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.remove_listener("response", _on_response)
        except Exception:  # noqa: BLE001
            pass

    # Close any new tabs the PDF click spawned so they don't
    # accumulate (SSRS opens the export in a new window on some
    # builds). Do this AFTER capture so we don't kill an in-flight
    # download.
    for extra in [p for p in ctx.pages if p is not target_page]:
        try:
            extra.close()
        except Exception:  # noqa: BLE001
            pass

    if captured:
        return captured[0]

    print("        [ssrs] L3 FAIL: PDF click didn't produce a PDF response")
    _dump_popup_debug(target_page, file_stem, "ssrs_pdf_click_no_download")
    return None


def _find_ssrs_toggle(
    target_page: Page, timeout_ms: int = 15_000
) -> tuple[Locator | None, object]:
    """
    Locate the SSRS Export toggle in ANY frame of the popup page.

    Returns (locator, frame). The frame handle is returned so
    landmark 2 can search the SAME frame (menu usually appears
    where the toggle was) before falling back to a page-wide sweep.
    Returns (None, main_frame) if the toggle never appears.
    """
    poll_ms = 250
    elapsed = 0
    main_frame = target_page.main_frame
    while elapsed < timeout_ms:
        for frame in target_page.frames:
            for sel in SSRS_EXPORT_TOGGLE_SELECTORS:
                try:
                    loc = frame.locator(sel).first
                    if loc.count() and loc.is_visible():
                        return loc, frame
                except Exception:  # noqa: BLE001
                    continue
        target_page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    return None, main_frame


def _wait_for_ssrs_pdf_option(
    frame, timeout_ms: int = 8_000
) -> Locator | None:
    """Poll a specific frame for the PDF menu option after toggle click."""
    poll_ms = 200
    elapsed = 0
    while elapsed < timeout_ms:
        for sel in SSRS_PDF_OPTION_SELECTORS:
            try:
                loc = frame.locator(sel).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:  # noqa: BLE001
                continue
        frame.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    return None


def _find_ssrs_pdf_option_any_frame(
    target_page: Page,
) -> tuple[Locator | None, object]:
    """Last-ditch sweep for the PDF option across every frame."""
    for frame in target_page.frames:
        for sel in SSRS_PDF_OPTION_SELECTORS:
            try:
                loc = frame.locator(sel).first
                if loc.count() and loc.is_visible():
                    return loc, frame
            except Exception:  # noqa: BLE001
                continue
    return None, target_page.main_frame


def _dump_popup_debug(target_page: Page, file_stem: str, reason: str) -> None:
    """Snapshot the SSRS popup itself (URL, screenshot, clickables)."""
    if not _DEBUG_DOWNLOADS:
        return
    debug_dir = Path("debug")
    debug_dir.mkdir(exist_ok=True)
    safe = safe_filename(f"{file_stem}_{reason}")
    shot_path = debug_dir / f"{safe}.png"
    txt_path = debug_dir / f"{safe}.txt"
    try:
        target_page.screenshot(path=str(shot_path), full_page=True)
    except Exception as exc:  # noqa: BLE001
        shot_path = Path(f"<screenshot failed: {exc}>")
    try:
        items = target_page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'a, button, input, img'"
            ")).filter(e => e.offsetParent !== null).slice(0, 60).map(e => ({"
            "tag: e.tagName.toLowerCase(),"
            "type: e.getAttribute('type') || '',"
            "text: (e.innerText || e.value || '').toString().trim().slice(0, 60),"
            "title: e.getAttribute('title') || '',"
            "alt: e.getAttribute('alt') || '',"
            "src: (e.getAttribute('src') || '').slice(0, 120),"
            "href: (e.getAttribute('href') || '').slice(0, 120),"
            "id: e.getAttribute('id') || ''"
            "}))"
        )
    except Exception as exc:  # noqa: BLE001
        items = [f"<evaluate failed: {exc}>"]
    lines = [
        f"reason: {reason}",
        f"url: {target_page.url}",
        f"title: {target_page.title()}",
        "",
        "Visible elements (top 60):",
    ]
    for it in items:
        lines.append(f"  {it}")
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"        [debug-downloads] popup: {shot_path.name}  +  {txt_path.name}")


def _dump_download_debug(
    page: Page,
    file_stem: str,
    reason: str,
    url_before: str,
    download_count_before: int,
) -> None:
    """Write `debug/<file_stem>_<reason>.{png,txt}` to disk."""
    if not _DEBUG_DOWNLOADS:
        return
    debug_dir = Path("debug")
    debug_dir.mkdir(exist_ok=True)
    safe = safe_filename(f"{file_stem}_{reason}")
    shot_path = debug_dir / f"{safe}.png"
    txt_path = debug_dir / f"{safe}.txt"

    try:
        page.screenshot(path=str(shot_path), full_page=True)
    except Exception as exc:  # noqa: BLE001
        shot_path = Path(f"<screenshot failed: {exc}>")

    info = {
        "reason": reason,
        "url_before": url_before,
        "url_after": page.url,
        "download_count_before": download_count_before,
        "download_count_after": (
            page.locator(DOWNLOAD_BUTTON_SELECTOR).count()
        ),
        "tabs_in_context": len(page.context.pages),
        "row_count_visible": page.locator(ASSESSMENT_ROW_SELECTOR).count(),
        "back_button_visible_count": page.locator(BACK_BUTTON_SELECTOR).count(),
    }

    # Capture a list of visible clickables to see what state the
    # page is in after the click.
    try:
        clickables = page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'button, a, [role=\"button\"], span.send-icon, "
            "span.material-icons, span.material-icons-outlined'"
            ")).filter(e => e.offsetParent !== null).slice(0, 60).map(e => ({"
            "tag: e.tagName.toLowerCase(),"
            "text: (e.innerText || '').trim().slice(0, 60),"
            "title: e.getAttribute('title') || '',"
            "aria: e.getAttribute('aria-label') || ''"
            "}))"
        )
    except Exception as exc:  # noqa: BLE001
        clickables = [f"<evaluate failed: {exc}>"]

    lines = [f"{k}: {v}" for k, v in info.items()]
    lines.append("")
    lines.append("Visible clickables (top 60):")
    for c in clickables:
        lines.append(f"  {c}")
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"        [debug-downloads] {shot_path.name}  +  {txt_path.name}")


def _follow_eye_and_download(
    page: Page, eye: Locator, save_dir: Path, file_stem: str
) -> str:
    """
    Click the row's eye icon and capture the assessment as a file.

    Three flows are supported (the SPA picks one based on
    assessment type):
      A1. Same-tab URL navigation -> detail page with a PDF
          download button -> click -> save -> page.go_back().
      A2. In-place component swap (no URL change) -> same page
          renders Back + Download buttons -> click Download ->
          save -> click Back to restore the table.
      B.  Legacy new-tab popup -> older format with Export/CSV
          button -> click -> save -> close the popup.

    Returns "saved", "failed", or "empty" (no click effect).
    """
    ctx = page.context
    url_before = page.url
    # Modern assessments swap the detail view IN PLACE without
    # changing the URL. Snapshot the count of download-style buttons
    # before the click so we can detect when a new one appears.
    download_count_before = page.locator(DOWNLOAD_BUTTON_SELECTOR).count()

    # Drain any orphan tabs left over from previous clicks before
    # arming the listener, so a late-arriving popup from a previous
    # row doesn't get mis-attributed to this click.
    for orphan in [p for p in ctx.pages if p is not page]:
        try:
            orphan.close()
        except Exception:  # noqa: BLE001
            pass

    new_page_holder: dict[str, Page | None] = {"page": None}

    def _on_page(np: Page) -> None:
        if new_page_holder["page"] is None:
            new_page_holder["page"] = np

    ctx.on("page", _on_page)
    try:
        try:
            # `force=True` because the eye icon is a tooltip-trigger
            # span; the tooltip layer can intercept hit-testing and
            # cause normal click() to silently no-op.
            eye.click(force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"        ?? eye click raised: {exc}")
            _dump_download_debug(
                page, file_stem, "click_raised", url_before, download_count_before
            )
            return "failed"

        # Wait up to ~15s for any of: new tab, URL change, or a
        # Download button to appear on the same page (in-place
        # component swap). First click on the assessment-viewer
        # bundle can be slow on a cold session.
        deadline_ms = 15_000
        poll_ms = 100
        elapsed = 0
        new_tab: Page | None = None
        same_tab_nav = False
        in_place_detail = False
        while elapsed < deadline_ms:
            if new_page_holder["page"] is not None:
                new_tab = new_page_holder["page"]
                break
            if page.url != url_before:
                same_tab_nav = True
                break
            if (
                page.locator(DOWNLOAD_BUTTON_SELECTOR).count()
                > download_count_before
            ):
                in_place_detail = True
                break
            page.wait_for_timeout(poll_ms)
            elapsed += poll_ms
    finally:
        ctx.remove_listener("page", _on_page)

    if new_tab is not None:
        # Flow B: legacy new-tab (SSRS ReportViewer popup).
        try:
            new_tab.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        # SSRS ReportViewer runs a bunch of AJAX renders AFTER
        # DOMContentLoaded — the report content, page-count widget,
        # toolbar images, etc. Wait for networkidle so the report
        # is fully rendered before we invoke exportReport('PDF')
        # (otherwise the ReportViewer throws "The report or page
        # is being updated. Please wait for the current action to
        # complete."). networkidle == ~500ms of no network requests.
        try:
            new_tab.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            pass
        # Prefer the SSRS-specific path (toolbar icon -> PDF option);
        # fall back to the generic Download/Export button finder for
        # non-SSRS legacy popups.
        saved = _try_legacy_ssrs_export_pdf(new_tab, save_dir, file_stem)
        if saved is None:
            saved = _try_download_on_page(new_tab, save_dir, file_stem)
        try:
            new_tab.close()
        except Exception:  # noqa: BLE001
            pass
        # Restore focus to the main page; closing a popup can leave
        # the main page de-focused which makes the next paginator
        # navigation fail with "could not navigate to inner page N".
        try:
            page.bring_to_front()
            page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass
        if saved is not None:
            print(f"        + {saved.name} (legacy/new-tab)")
            return "saved"
        print(f"        ?? no download trigger on legacy popup for {file_stem}")
        _dump_download_debug(
            page, file_stem, "legacy_popup_no_download",
            url_before, download_count_before,
        )
        return "failed"

    if same_tab_nav:
        # Flow A1: modern same-tab navigation (URL changed).
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        saved = _try_download_on_page(page, save_dir, file_stem)
        try:
            page.go_back(wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            pass
        if saved is not None:
            print(f"        + {saved.name} (modern/same-tab)")
            return "saved"
        print(f"        ?? no download trigger on detail page for {file_stem}")
        _dump_download_debug(
            page, file_stem, "same_tab_no_download",
            url_before, download_count_before,
        )
        return "failed"

    if in_place_detail:
        # Flow A2: modern in-place component swap (no URL change).
        # Detail view renders alongside Back + Download buttons.
        saved = _try_download_on_page(page, save_dir, file_stem)
        # Click Back on the detail view to restore the assessment
        # table; `page.go_back()` would jump to the previous client
        # detail tab, not the Care Assessment tab.
        back = page.locator(BACK_BUTTON_SELECTOR).first
        try:
            if back.count():
                back.click()
                page.wait_for_selector(
                    ASSESSMENT_ROW_SELECTOR, timeout=10_000
                )
                # Wait for the in-place detail panel to fully unmount,
                # i.e. the Download button count returns to the
                # baseline we snapshotted before the eye click. If we
                # don't wait, the NEXT row's "count increased"
                # detection sees the stale button and clicks the
                # wrong thing.
                deadline = 5_000
                poll = 200
                elapsed = 0
                while (
                    page.locator(DOWNLOAD_BUTTON_SELECTOR).count()
                    > download_count_before
                    and elapsed < deadline
                ):
                    page.wait_for_timeout(poll)
                    elapsed += poll
        except Exception:  # noqa: BLE001
            pass
        if saved is not None:
            print(f"        + {saved.name} (modern/in-place)")
            return "saved"
        print(f"        ?? no download trigger on in-place detail for {file_stem}")
        _dump_download_debug(
            page, file_stem, "in_place_no_download",
            url_before, download_count_before,
        )
        return "failed"

    print(f"        ?? eye click had no observable effect for {file_stem}")
    _dump_download_debug(
        page, file_stem, "empty_no_effect", url_before, download_count_before
    )
    return "empty"


def _write_status_file(
    client_folder: Path,
    client: Client,
    all_rows: list[dict],
    position_by_aid: dict[str, int],
    total: int,
    pos_width: int,
) -> None:
    """
    Write a self-describing status file to `client_folder` so the
    user can see completion progress without opening anything.

    Filename:
        _STATUS_<saved>of<total>_<COMPLETE|MISSING>.txt
        e.g. "_STATUS_12of21_MISSING.txt"
             "_STATUS_21of21_COMPLETE.txt"

    The body lists every assessment row with its position, ID,
    status, and outcome (SAVED / MISSING / SKIP-InProgress).

    Old status files (any matching `_STATUS_*.txt`) are removed
    before the new one is written so the folder never has stale
    counts.
    """
    # Remove any old status files.
    for old in client_folder.glob("_STATUS_*.txt"):
        try:
            old.unlink()
        except OSError:
            pass

    # Count actual PDF/CSV files on disk and figure out which
    # assessment IDs they represent.
    on_disk = [
        f for f in client_folder.iterdir()
        if f.is_file() and f.suffix.lower() in {".pdf", ".csv"}
    ]
    name_prefix = name_to_folder(client.name)
    saved_aids: set[str] = set()
    for f in on_disk:
        stem = f.stem
        for aid in position_by_aid:
            aid_part = f"{name_prefix}_{safe_filename(aid)}"
            if stem == aid_part or stem.startswith(aid_part + "_"):
                saved_aids.add(aid)
                break

    n_present = len(on_disk)
    n_inprog = sum(
        1 for r in all_rows
        if (r["status"] or "").strip().lower() == "in-progress"
    )
    # Complete = every non-In-Progress row is on disk. In-Progress
    # rows are intentional skips, so they don't count as missing.
    complete = (n_present + n_inprog) >= total
    label = "COMPLETE" if complete else "MISSING"
    fname = f"_STATUS_{n_present}of{total}_{label}.txt"
    status_path = client_folder / fname

    lines = [
        f"Client:    {client.name}",
        f"Status:    {client.status}",
        f"ClientID:  {client.client_id}",
        "",
        f"Total assessments in table:           {total}",
        f"Files on disk (PDF/CSV):              {n_present}",
        f"In-Progress (skipped — trash icon):   {n_inprog}",
        f"Missing (failed or pending):          {max(0, total - n_present - n_inprog)}",
        "",
        "Per-row breakdown:",
        "  position   assessment_id   care_plan_status   download_result",
        "  --------   -------------   ----------------   ---------------",
    ]
    for r in all_rows:
        aid = r["assessment_id"]
        pos = position_by_aid.get(aid, 0)
        status = (r["status"] or "").strip()
        if not aid:
            state = "NO_ID"
        elif status.lower() == "in-progress":
            state = "SKIP (In-Progress)"
        elif aid in saved_aids:
            state = "SAVED"
        else:
            state = "MISSING"
        pos_str = f"{pos:0{pos_width}d}of{total}"
        lines.append(
            f"  {pos_str:<9s}  {aid:<14s}  {status:<17s}  {state}"
        )
    lines.append("")
    status_path.write_text("\n".join(lines), encoding="utf-8")


def download_care_assessments_for_client(
    page: Page,
    client: Client,
    out_root: Path,
    shutdown_event: threading.Event | None = None,
    force_aid: str = "",
) -> tuple[int, int, int]:
    """Returns (saved, skipped, failed) for this client.

    When `force_aid` is non-empty, only rows whose assessment_id
    equals that value are processed, and the resume-dedupe check is
    bypassed so an already-saved file gets re-downloaded. Any
    existing PDF/CSV matching that assessment_id is deleted first
    so the SSRS flow re-runs cleanly.
    """
    if not open_care_assessment_tab(page, client):
        print(f"    !! could not open Care Assessment for {client.name}")
        return (0, 0, 1)

    # Pre-walk every inner page to learn the assessment IDs and statuses.
    # This is cheap (text-only reads) and lets us:
    #   - flag the folder name BEFORE creating it, and
    #   - drive the download loop by assessment ID, which survives the
    #     table re-rendering after each download.
    all_rows = collect_assessment_rows(page)

    statuses_lc = [(r["status"] or "").strip().lower() for r in all_rows]
    has_in_progress = any(s == "in-progress" for s in statuses_lc)
    has_non_completed = any(
        s and s != "completed" for s in statuses_lc
    )
    total = len(all_rows)

    # Bucket each client under its status (active/inactive). Folder
    # name flag priority (most specific wins so the user can spot
    # the cause at a glance):
    #   _IN_PROGRESS_<Name>_<n>of<total>  -> at least one In-Progress
    #                              row (action icon is a TRASH can;
    #                              row is skipped to avoid deletion).
    #   _FLAG_<Name>_<n>of<total>  -> any other non-Completed row
    #                              (e.g. Pending, Submitted).
    #   <Name>_<n>of<total>        -> every row is Completed.
    # <n> is the number of PDF/CSV files on disk right now; <total>
    # is the number of assessments in the live table. So the folder
    # name itself tells you completeness: "63of63" means done,
    # "45of63" means 18 still to grab.
    status_bucket = (client.status or "unknown").strip().lower() or "unknown"
    base_label = name_to_folder(client.name)
    if has_in_progress:
        prefix = "_IN_PROGRESS_"
    elif has_non_completed:
        prefix = "_FLAG_"
    else:
        prefix = ""

    parent = out_root / status_bucket
    parent.mkdir(parents=True, exist_ok=True)

    def _saved_on_disk(folder: Path | None) -> int:
        if folder is None or not folder.exists():
            return 0
        return sum(
            1 for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in {".pdf", ".csv"}
        )

    def _make_folder_label(saved: int) -> str:
        if total:
            return f"{prefix}{base_label}_{saved}of{total}"
        return f"{prefix}{base_label}"

    # Migrate an older-naming folder if one exists. Covers every
    # prefix (clean, _FLAG_, _IN_PROGRESS_) AND every legacy suffix
    # variant (`_21`, `_45of63`, or no suffix at all).
    base_re = re.compile(
        r"^(_FLAG_|_IN_PROGRESS_)?"
        + re.escape(base_label)
        + r"(_.+)?$"
    )
    existing_folder: Path | None = None
    for p in parent.iterdir():
        if p.is_dir() and base_re.match(p.name):
            existing_folder = p
            break
    saved_at_start = _saved_on_disk(existing_folder)

    folder_label = _make_folder_label(saved_at_start)
    client_folder = parent / folder_label

    if existing_folder is not None and existing_folder != client_folder:
        try:
            existing_folder.rename(client_folder)
            print(
                f"    (migrated folder {existing_folder.name!r} -> "
                f"{client_folder.name!r})"
            )
        except OSError:
            pass
    client_folder.mkdir(parents=True, exist_ok=True)

    if not all_rows:
        print("    (no assessments on the Care Assessment tab)")
        return (0, 0, 0)

    distinct_statuses = sorted({(r["status"] or "").strip() for r in all_rows})
    flag_tag = (
        " [_IN_PROGRESS_]"
        if has_in_progress
        else (" [_FLAG_]" if has_non_completed else "")
    )
    print(
        f"    {len(all_rows)} assessment(s) across statuses: "
        f"{distinct_statuses}{flag_tag}"
    )

    # Map each assessment id to its 1-based overall position in the
    # pre-walk order (page 1 row 0 -> 1, ... page N last row -> total).
    # Files are named "<client>_<aid>_<pos>of<total>.pdf" using a
    # zero-padded position so the directory sorts naturally.
    position_by_aid: dict[str, int] = {}
    for idx, r in enumerate(all_rows, start=1):
        if r["assessment_id"]:
            position_by_aid[r["assessment_id"]] = idx
    pos_width = max(2, len(str(total)))

    # Group rows by inner-page number.
    rows_by_page: dict[int, list[dict]] = {}
    for r in all_rows:
        rows_by_page.setdefault(r["page_no"], []).append(r)

    saved = skipped = failed = 0

    for page_no in sorted(rows_by_page):
        if shutdown_event is not None and shutdown_event.is_set():
            break
        rows_on_page = rows_by_page[page_no]
        n_expected = len(rows_on_page)
        print(f"    -- inner page {page_no} ({n_expected} row(s)) --")

        # Drive by ROW INDEX, re-navigating to this inner page before
        # every row. This is brute-force but robust to whatever the
        # modern-flow page.go_back() does to inner-pagination state
        # (Angular sometimes resets to page 1, sometimes doesn't —
        # we don't have to care).
        for row_idx in range(n_expected):
            if shutdown_event is not None and shutdown_event.is_set():
                print("        (shutdown requested; stopping this client)")
                break
            if not _navigate_to_inner_page(page, client, page_no):
                print(f"    !! could not navigate to inner page {page_no}")
                failed += n_expected - row_idx
                break

            # The inner table can flicker to 0 rows for a brief
            # moment after re-rendering; retry-with-wait before
            # giving up so we don't skip rows for a timing race.
            trs = page.locator(ASSESSMENT_ROW_SELECTOR)
            wait_total_ms = 0
            while trs.count() <= row_idx and wait_total_ms < 5_000:
                page.wait_for_timeout(250)
                wait_total_ms += 250
                trs = page.locator(ASSESSMENT_ROW_SELECTOR)
            if trs.count() <= row_idx:
                print(
                    f"        ?? page {page_no} has only {trs.count()} "
                    f"row(s); expected row index {row_idx}"
                )
                failed += 1
                continue

            tr = trs.nth(row_idx)
            aid = _row_assessment_id(tr) or rows_on_page[row_idx]["assessment_id"]
            if not aid:
                failed += 1
                continue

            # --force-aid gate: skip every row whose assessment_id
            # isn't the one the user asked us to re-download.
            if force_aid and aid != force_aid:
                skipped += 1
                continue

            # SAFETY: skip In-Progress rows. Their action-column icon
            # is a TRASH can, not an eye, and clicking it deletes the
            # assessment. Re-read the row's status from the live DOM
            # in case the cached pre-walk value drifted.
            row_status = (
                _row_status(tr) or rows_on_page[row_idx]["status"] or ""
            ).strip().lower()
            if row_status == "in-progress":
                print(
                    f"        ~ skip {aid}: status=In-Progress "
                    f"(trash icon, not eye)"
                )
                skipped += 1
                continue

            # File stem = "<client>_<aid>_<pos>of<total>". Position
            # is the row's 1-based ordinal in pre-walk order;
            # pos_width = max(2, digits-in-total) so a 21-item
            # client sorts 01..21 alphabetically.
            pos = position_by_aid.get(aid, 0)
            aid_part = f"{name_to_folder(client.name)}_{safe_filename(aid)}"
            if pos:
                file_stem = f"{aid_part}_{pos:0{pos_width}d}of{total}"
            else:
                file_stem = aid_part

            # Resume / dedupe: any file whose name starts with the
            # client+aid prefix counts as already-downloaded. This
            # catches both old-style names ("Adams_Doreen_1845.pdf")
            # and the new "<aid>_<N>of<M>" form. If we find an
            # old-style file, rename it to the new convention so
            # the folder ends up consistent.
            #
            # `--force-aid` bypasses this check AND deletes any
            # existing file for this AID first so the re-download
            # writes cleanly.
            existing = list(client_folder.glob(f"{aid_part}*"))
            if existing and force_aid and aid == force_aid:
                for old in existing:
                    try:
                        old.unlink()
                        print(
                            f"        [force-aid] deleted existing "
                            f"{old.name!r} to force re-download"
                        )
                    except OSError as exc:
                        print(
                            f"        [force-aid] could not delete "
                            f"{old.name!r}: {exc}"
                        )
                existing = []
            if existing:
                old_file = existing[0]
                if old_file.stem == aid_part and old_file.stem != file_stem:
                    try:
                        new_path = old_file.with_name(
                            f"{file_stem}{old_file.suffix}"
                        )
                        if not new_path.exists():
                            old_file.rename(new_path)
                    except OSError:
                        pass
                skipped += 1
                continue

            eye = tr.locator(ASSESSMENT_EYE_ICON_SELECTOR).first
            if not eye.count():
                print(f"        ?? no eye icon for assessment {aid}")
                failed += 1
                continue

            # Rows below the fold can swallow `force=True` clicks
            # because Chromium's hit-testing dispatches at coordinates
            # outside the viewport. Scroll the row into view first.
            try:
                tr.scroll_into_view_if_needed(timeout=3_000)
            except Exception:  # noqa: BLE001
                pass

            result = _follow_eye_and_download(
                page, eye, client_folder, file_stem
            )
            if result == "saved":
                saved += 1
            else:
                failed += 1

    # If the saved count changed during this run, rename the folder
    # so its name reflects the new tally (e.g. Blaney_..._45of63 ->
    # Blaney_..._63of63 once we finish the rest).
    new_saved = _saved_on_disk(client_folder)
    if new_saved != saved_at_start:
        new_label = _make_folder_label(new_saved)
        new_path = parent / new_label
        if new_path != client_folder:
            try:
                client_folder.rename(new_path)
                print(
                    f"    (renamed folder to {new_path.name!r} "
                    f"[{new_saved}of{total}])"
                )
                client_folder = new_path
            except OSError as exc:
                print(f"    !! folder rename failed: {exc}")

    # Always rewrite the at-a-glance status file so the filename
    # itself shows how many files are on disk vs the expected total.
    try:
        _write_status_file(
            client_folder, client, all_rows, position_by_aid, total, pos_width
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    !! could not write status file: {exc}")

    return (saved, skipped, failed)


# ─── main ──────────────────────────────────────────────────────────────────


def _refresh_status_file_at_startup(
    folder: Path, base_prefix: str, total: int
) -> bool:
    """
    Rewrite the folder's `_STATUS_*.txt` based on current disk
    state. Parses the previous status file to preserve per-row
    assessment IDs + care-plan statuses, then re-computes each
    row's SAVED / MISSING / SKIP outcome against the current PDF
    inventory. Returns True if a refresh happened.

    Skips the folder if no prior status file exists (there's no
    row-level data to reconstruct without hitting the network).
    """
    status_files = list(folder.glob("_STATUS_*.txt"))
    if not status_files:
        return False
    prev = status_files[0]
    try:
        text = prev.read_text(encoding="utf-8")
    except OSError:
        return False

    # Header fields.
    client_name = "unknown"
    client_status_bucket = "unknown"
    client_id = "unknown"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Client:"):
            client_name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Status:"):
            client_status_bucket = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("ClientID:"):
            client_id = stripped.split(":", 1)[1].strip()

    # Per-row breakdown table.
    rows: list[dict] = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("position") and "assessment_id" in stripped:
            in_table = True
            continue
        if not in_table:
            continue
        if stripped.startswith("---") or not stripped:
            if not stripped and rows:
                break
            continue
        # `01of21     1845            Completed          SAVED`
        parts = stripped.split(None, 3)
        if len(parts) < 3:
            continue
        pos_str = parts[0]
        aid = parts[1]
        cp_status = parts[2]
        m = re.match(r"^(\d+)of(\d+)$", pos_str)
        if not m:
            continue
        rows.append(
            {
                "position": int(m.group(1)),
                "assessment_id": aid,
                "care_plan_status": cp_status,
            }
        )
    if not rows:
        return False

    pos_width = max(2, len(str(total)))
    aid_to_pos = {r["assessment_id"]: r["position"] for r in rows}

    # Rename manually-dropped files whose stem is just the AID (or
    # AID + something) into the full `<base>_<aid>_<pos>of<total>`
    # convention so the resume-dedupe glob picks them up. Matches:
    #   * `1462.pdf`               (bare aid)
    #   * `1462_manual.pdf`        (aid + suffix)
    #   * `care_plan_1462.pdf`     (aid embedded, matched by contains
    #                              a token equal to a known aid)
    # Files that already start with `<base>_<aid>` are left alone.
    for f in list(folder.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in {".pdf", ".csv"}:
            continue
        if f.name.startswith("_STATUS_"):
            continue
        stem = f.stem
        if stem.startswith(base_prefix + "_"):
            continue  # already conventional
        matched_aid = None
        # Prefer exact-token match; also accept "starts with aid_" or
        # "ends with _aid" so manual naming variants get caught.
        tokens = re.split(r"[^A-Za-z0-9]+", stem)
        for aid in aid_to_pos:
            if aid in tokens or stem == aid or stem.startswith(aid + "_"):
                matched_aid = aid
                break
        if matched_aid is None:
            continue
        pos = aid_to_pos[matched_aid]
        new_stem = (
            f"{base_prefix}_{matched_aid}_{pos:0{pos_width}d}of{total}"
        )
        new_path = f.with_name(f"{new_stem}{f.suffix}")
        if new_path == f or new_path.exists():
            continue
        try:
            f.rename(new_path)
            print(
                f"    [startup] renamed manual file "
                f"{f.name!r} -> {new_path.name!r}"
            )
        except OSError as exc:
            print(
                f"    [startup] rename failed for {f.name!r}: {exc}"
            )

    # Recompute SAVED by matching filenames against `<base>_<aid>*`.
    on_disk = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in {".pdf", ".csv"}
    ]
    saved_aids: set[str] = set()
    for f in on_disk:
        stem = f.stem
        for r in rows:
            aid_part = f"{base_prefix}_{r['assessment_id']}"
            if stem == aid_part or stem.startswith(aid_part + "_"):
                saved_aids.add(r["assessment_id"])
                break

    n_present = len(on_disk)
    n_inprog = sum(
        1 for r in rows
        if r["care_plan_status"].lower() == "in-progress"
    )
    complete = (n_present + n_inprog) >= total
    label = "COMPLETE" if complete else "MISSING"

    # Delete every existing status file, then write the new one.
    for old in status_files:
        try:
            old.unlink()
        except OSError:
            pass

    new_path = folder / f"_STATUS_{n_present}of{total}_{label}.txt"
    lines = [
        f"Client:    {client_name}",
        f"Status:    {client_status_bucket}",
        f"ClientID:  {client_id}",
        "",
        f"Total assessments in table:           {total}",
        f"Files on disk (PDF/CSV):              {n_present}",
        f"In-Progress (skipped \u2014 trash icon):   {n_inprog}",
        f"Missing (failed or pending):          "
        f"{max(0, total - n_present - n_inprog)}",
        "",
        "Per-row breakdown:",
        "  position   assessment_id   care_plan_status   download_result",
        "  --------   -------------   ----------------   ---------------",
    ]
    for r in rows:
        pos = r["position"]
        aid = r["assessment_id"]
        cp = r["care_plan_status"]
        if cp.lower() == "in-progress":
            state = "SKIP (In-Progress)"
        elif aid in saved_aids:
            state = "SAVED"
        else:
            state = "MISSING"
        pos_str = f"{pos:0{pos_width}d}of{total}"
        lines.append(f"  {pos_str:<9s}  {aid:<14s}  {cp:<17s}  {state}")
    lines.append("")
    new_path.write_text("\n".join(lines), encoding="utf-8")
    return True


def _update_folder_totals_at_startup(out_root: Path) -> None:
    """
    Walk `out_root/{active,inactive}` and refresh each client
    folder's `<saved>of<total>` count in both the folder name AND
    its `_STATUS_*.txt` breakdown. No network required.

    Folder-name handling:
      * `Smith_John`               -> skipped (no numeric suffix).
      * `Smith_John_15`            -> renamed to `Smith_John_NofM`
                                      using the `15` as the total.
      * `Smith_John_10of15`        -> re-counted; renamed only if
                                      the on-disk count changed.
      * `_FLAG_...` / `_IN_PROGRESS_...` prefixes are preserved
        as-is (they reflect the last-known status; per-client
        loop refreshes them when it re-scrapes).

    Status-file handling:
      * Parses the existing `_STATUS_*.txt` (if any) to keep the
        per-row assessment IDs / care-plan statuses.
      * Re-computes each row's SAVED / MISSING / SKIP outcome
        against the current PDF inventory.
      * Rewrites the status file with the updated counts and
        filename.
    """
    if not out_root.exists():
        return
    renamed = 0
    status_refreshed = 0
    scanned = 0
    for bucket_name in ("active", "inactive"):
        parent = out_root / bucket_name
        if not parent.exists():
            continue
        for folder in parent.iterdir():
            if not folder.is_dir():
                continue
            scanned += 1
            name = folder.name
            m = re.search(r"_(\d+)(?:of(\d+))?$", name)
            if not m:
                continue
            total = int(m.group(2)) if m.group(2) else int(m.group(1))
            head = name[: m.start()]
            if head.startswith("_IN_PROGRESS_"):
                prefix = "_IN_PROGRESS_"
                base = head[len(prefix):]
            elif head.startswith("_FLAG_"):
                prefix = "_FLAG_"
                base = head[len(prefix):]
            else:
                prefix = ""
                base = head
            saved = sum(
                1 for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() in {".pdf", ".csv"}
            )
            new_name = f"{prefix}{base}_{saved}of{total}"

            # Rename the folder if the count in its name is stale.
            current_folder = folder
            if new_name != name:
                try:
                    folder.rename(parent / new_name)
                    print(f"    [startup] renamed {name!r} -> {new_name!r}")
                    renamed += 1
                    current_folder = parent / new_name
                except OSError as exc:
                    print(
                        f"    [startup] rename failed for {name!r}: {exc}"
                    )

            # Refresh the status file inside the (possibly renamed)
            # folder so the per-row breakdown and filename reflect
            # the current disk state.
            try:
                if _refresh_status_file_at_startup(
                    current_folder, base, total
                ):
                    status_refreshed += 1
            except Exception as exc:  # noqa: BLE001
                print(
                    f"    [startup] status refresh failed for "
                    f"{current_folder.name!r}: {exc}"
                )
    if scanned == 0:
        print("[*] No existing client folders to refresh.")
        return
    parts = []
    if renamed:
        parts.append(f"renamed {renamed} folder(s)")
    if status_refreshed:
        parts.append(f"refreshed {status_refreshed} status file(s)")
    if parts:
        print(
            f"[*] Startup pass: {', '.join(parts)} "
            f"(scanned {scanned})."
        )
    else:
        print(
            f"[*] Scanned {scanned} folder(s); already up to date."
        )


class _ControlWindow:
    """
    Always-on-top tkinter window that combines two roles:

      1. Status picker (Inactive / Active / Both) with a green
         "Start Scraping" button. Skipped if a status was already
         supplied on the command line.
      2. Red "Close and Update Data" button while the scraper is
         running. Clicking it (or closing the window with the X)
         sets `shutdown_event`; the main loop checks the flag
         between rows / clients, stops cleanly, closes the
         browser, then refreshes every folder name +
         `_STATUS_*.txt` before exiting.

    Runs on a background daemon thread so the scraper's main loop
    keeps running. Falls back to `gui_available = False` if
    tkinter isn't installed or a display can't be opened; in that
    case the caller should fall through to the terminal prompt.
    """

    def __init__(self, initial_status: str | None = None) -> None:
        self.shutdown_event = threading.Event()
        self.start_event = threading.Event()
        self.status_choice = initial_status or "Inactive"
        self.gui_available = False
        self._initial_status = initial_status
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait_for_start(self, timeout: float | None = None) -> bool:
        """Block until Start (or close) is clicked. Returns True
        if start_event was set; False if the timeout elapsed."""
        return self.start_event.wait(timeout=timeout)

    def _run(self) -> None:
        try:
            self._run_body()
        except Exception:  # noqa: BLE001
            # Print full traceback so the user knows why the window
            # died, instead of silently disappearing.
            print("[control-window] tkinter thread crashed:", flush=True)
            traceback.print_exc()
            # Release any waiter so main() doesn't hang forever.
            self.start_event.set()

    def _run_body(self) -> None:
        try:
            import tkinter as tk
        except ImportError:
            # No tkinter available — release any waiter so the
            # main thread doesn't hang.
            self.start_event.set()
            return
        try:
            root = tk.Tk()
        except tk.TclError:
            self.start_event.set()
            return

        self.gui_available = True
        root.title("Care-plan scraper — control")
        try:
            root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        root.geometry("400x260+80+80")

        # Widgets built up-front; toggled between "pick status" and
        # "running" states.
        picker_frame = tk.Frame(root)
        running_frame = tk.Frame(root)

        # ── Picker state ──────────────────────────────────────
        tk.Label(
            picker_frame,
            text="Which clients do you want to scrape?",
            font=("Segoe UI", 10, "bold"),
        ).pack(pady=(10, 4))

        status_var = tk.StringVar(value=self.status_choice)
        radio_row = tk.Frame(picker_frame)
        radio_row.pack(pady=6)
        for label, val in (
            ("Inactive", "Inactive"),
            ("Active", "Active"),
            ("Both", "Both"),
        ):
            tk.Radiobutton(
                radio_row,
                text=label,
                variable=status_var,
                value=val,
            ).pack(side="left", padx=10)

        def _enter_running_state() -> None:
            picker_frame.pack_forget()
            running_frame.pack(fill="both", expand=True)
            running_label.config(
                text=(
                    f"Scraping {self.status_choice} clients.\n\n"
                    "Click below to stop cleanly and refresh\n"
                    "all folder names + status files."
                )
            )

        def _on_start() -> None:
            try:
                print("[control-window] Start clicked", flush=True)
                self.status_choice = status_var.get()
                print(
                    f"[control-window] status_choice = "
                    f"{self.status_choice!r}",
                    flush=True,
                )
                self.start_event.set()
                _enter_running_state()
                print("[control-window] entered running state", flush=True)
            except Exception:  # noqa: BLE001
                print("[control-window] _on_start crashed:", flush=True)
                traceback.print_exc()
                # Ensure the main thread still unblocks.
                self.start_event.set()

        start_btn = tk.Button(
            picker_frame,
            text="Start Scraping",
            command=_on_start,
            bg="#27ae60",
            fg="white",
            padx=16,
            pady=8,
            font=("Segoe UI", 10, "bold"),
        )
        start_btn.pack(pady=10)

        # ── Running state ─────────────────────────────────────
        running_label = tk.Label(
            running_frame,
            text="",
            padx=10,
            pady=10,
            justify="center",
        )
        running_label.pack()

        def _on_close() -> None:
            self.shutdown_event.set()
            # If the user closed BEFORE clicking Start, still
            # release the waiter so main() can exit cleanly.
            self.start_event.set()
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

        close_btn = tk.Button(
            running_frame,
            text="Close and Update Data",
            command=_on_close,
            bg="#c0392b",
            fg="white",
            padx=16,
            pady=8,
            font=("Segoe UI", 10, "bold"),
        )
        close_btn.pack(pady=10)

        # Show the appropriate frame at startup.
        if self._initial_status:
            self.start_event.set()
            _enter_running_state()
        else:
            picker_frame.pack(fill="both", expand=True)

        root.protocol("WM_DELETE_WINDOW", _on_close)
        try:
            root.mainloop()
        except Exception:  # noqa: BLE001
            pass


def _prompt_status_choice() -> str:
    """
    Ask the user which status bucket to scrape. Returns one of
    "Inactive" / "Active" / "Both". Falls back to "Inactive" when
    stdin isn't a TTY (e.g. running under a scheduler).
    """
    if not sys.stdin.isatty():
        return "Inactive"
    print()
    print("Which client status do you want to scrape?")
    print("  1) Inactive  (default)")
    print("  2) Active")
    print("  3) Both      (Inactive first, then Active)")
    while True:
        try:
            raw = input("Choice [1]: ").strip().lower()
        except EOFError:
            return "Inactive"
        if raw in ("", "1", "i", "inactive"):
            return "Inactive"
        if raw in ("2", "a", "active"):
            return "Active"
        if raw in ("3", "b", "both"):
            return "Both"
        print("  ! please enter 1, 2, or 3.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mass-download Care Assessment PDFs from Caresmartz360."
    )
    parser.add_argument(
        "--status",
        choices=["Active", "Inactive", "Both"],
        default=None,
        help="Which status filter(s) to walk. If omitted, an "
             "interactive prompt asks you (Inactive default).",
    )
    parser.add_argument(
        "--headless", action="store_true", help="Run without a visible browser."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("Hospall_Care_Assesments"),
        help="Root output folder for downloaded PDFs. Per-client folders "
             "are created under <out>/active/ or <out>/inactive/. "
             "Default: Hospall_Care_Assesments.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N clients (0 = no limit). Useful for debugging.",
    )
    parser.add_argument(
        "--client-id",
        default="",
        help="Only process this single ClientID; skips the list walk.",
    )
    parser.add_argument(
        "--force-aid",
        default="",
        metavar="ASSESSMENT_ID",
        help="Only download this specific assessment_id (e.g. '158'); "
             "skips every other row AND bypasses the resume-dedupe "
             "check so an already-saved file is re-downloaded. Use "
             "with --client-id to force-retry one SSRS assessment.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Walk the client list and print every client, then exit. "
             "No detail pages, no downloads.",
    )
    parser.add_argument(
        "--pause",
        type=int,
        default=0,
        metavar="SECONDS",
        help="After finishing, keep the browser open this many seconds so "
             "you can inspect the final page (headed mode only).",
    )
    parser.add_argument(
        "--debug-paginator",
        action="store_true",
        help="On the first paginator-not-found, save a screenshot and "
             "dump candidate button/link info to help write a selector.",
    )
    parser.add_argument(
        "--debug-downloads",
        action="store_true",
        help="On every failed eye-click / in-place-download, save a "
             "screenshot + page-state dump under debug/ to help "
             "diagnose why the row didn't save.",
    )
    args = parser.parse_args()

    global _DEBUG_PAGINATOR
    _DEBUG_PAGINATOR = args.debug_paginator
    global _DEBUG_DOWNLOADS
    _DEBUG_DOWNLOADS = args.debug_downloads

    load_dotenv()
    username = os.getenv("CARESMARTZ_USERNAME")
    password = os.getenv("CARESMARTZ_PASSWORD")
    if not username or not password:
        print(
            "ERROR: set CARESMARTZ_USERNAME and CARESMARTZ_PASSWORD in .env",
            file=sys.stderr,
        )
        return 2

    args.out.mkdir(parents=True, exist_ok=True)

    # Refresh <saved>of<total> counts on existing client folders
    # before we touch the network. Purely local; no-op if the tree
    # is empty.
    print("[*] Updating existing folder totals...")
    _update_folder_totals_at_startup(args.out)

    # Show the always-on-top control window RIGHT NOW so it's
    # available during login, the client-list walk, and the
    # downloads. It also acts as the status picker when --status
    # wasn't supplied on the CLI.
    #
    # SKIP the window in single-client debug mode (--client-id or
    # --force-aid) — the window's close button sets a shutdown
    # event, and it's easy to accidentally close it mid-run
    # thinking the run is hung. In debug mode we WANT the run to
    # complete without any external "stop" signal.
    debug_mode = bool(args.client_id or args.force_aid)
    if debug_mode:
        print("[*] Debug mode (--client-id/--force-aid): skipping control window.")

        class _NoopControl:
            shutdown_event = threading.Event()
            start_event = threading.Event()
            gui_available = False
            status_choice = args.status or "Inactive"

            def start(self) -> None:
                self.start_event.set()

            def wait_for_start(self, timeout: float | None = None) -> bool:  # noqa: ARG002
                return True

        control = _NoopControl()  # type: ignore[assignment]
        control.start()
    else:
        control = _ControlWindow(initial_status=args.status)
        control.start()

    if args.status is None:
        # Give tkinter a beat to spin up its window/`gui_available`.
        control.wait_for_start(timeout=1.5)
        if control.gui_available:
            print(
                "[*] Waiting for status choice in the control "
                "window (click 'Start Scraping')...",
                flush=True,
            )
            control.wait_for_start()
            print(
                f"[*] Start received (status={control.status_choice!r}, "
                f"shutdown={control.shutdown_event.is_set()})",
                flush=True,
            )
            if control.shutdown_event.is_set() and not control.start_event.is_set():
                print("[*] Control window closed without starting; exiting.")
                return 0
            args.status = control.status_choice
        else:
            # No GUI available — fall back to terminal prompt.
            args.status = _prompt_status_choice()
        print(f"[*] Status filter: {args.status}", flush=True)

    # Inactive first when both are requested — that's the bigger
    # bucket and the one the user wants prioritised, so it shouldn't
    # have to wait for the Active walk to finish.
    statuses = (
        ["Inactive", "Active"] if args.status == "Both" else [args.status]
    )

    total_clients = total_saved = total_skipped = total_failed = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            print("[*] Logging in...")
            login(page, username, password)

            if args.client_id:
                clients = [
                    Client(
                        status="(direct)",
                        name=args.client_id,
                        client_id=args.client_id,
                        href=f"/v2/client/client-detail?ClientID={args.client_id}",
                    )
                ]
            else:
                print(f"[*] Opening {CLIENT_LIST_URL}")
                page.goto(CLIENT_LIST_URL, wait_until="domcontentloaded")
                page.wait_for_selector(NAME_LINK_SELECTOR, timeout=30_000)
                clients = collect_clients(page, statuses)
                print(f"[*] {len(clients)} clients found.")

            if args.list_only:
                print()
                print(f"[+] {len(clients)} client(s) across {', '.join(statuses)}:")
                for idx, c in enumerate(clients, 1):
                    print(f"    {idx:>4}. [{c.status}] {c.name}  ({c.client_id})")
                if args.pause and not args.headless:
                    print(f"[*] Pausing {args.pause}s so you can inspect the browser...")
                    page.wait_for_timeout(args.pause * 1000)
                return 0

            print("[*] Starting downloads...")

            for client in clients:
                if control.shutdown_event.is_set():
                    print(
                        "[*] Close-and-update-data requested; "
                        "stopping cleanly."
                    )
                    break
                total_clients += 1
                print(
                    f"[client {total_clients}] {client.status}: "
                    f"{client.name} ({client.client_id})"
                )
                s, sk, f = download_care_assessments_for_client(
                    page, client, args.out,
                    shutdown_event=control.shutdown_event,
                    force_aid=args.force_aid,
                )
                total_saved += s
                total_skipped += sk
                total_failed += f
                if args.limit and total_clients >= args.limit:
                    print(f"[*] --limit {args.limit} reached, stopping.")
                    break

            if args.pause and not args.headless:
                print(f"[*] Pausing {args.pause}s so you can inspect the browser...")
                page.wait_for_timeout(args.pause * 1000)
        finally:
            context.close()
            browser.close()

    # Refresh every folder name + status file so their contents
    # reflect the final on-disk state (including anything saved
    # during THIS run, plus any manually-dropped files).
    print("[*] Updating folder totals + status files with final state...")
    _update_folder_totals_at_startup(args.out)

    print()
    print(f"[+] Clients processed : {total_clients}")
    print(f"    PDFs saved        : {total_saved}")
    print(f"    PDFs skipped      : {total_skipped}")
    print(f"    Failures          : {total_failed}")
    print(f"    Output folder     : {args.out}")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
