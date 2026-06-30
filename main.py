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


def _navigate_to_inner_page(page: Page, client: Client, target_page_no: int) -> bool:
    """
    Reset the Care Assessment table to inner page `target_page_no` by
    re-opening the tab (which always lands on page 1) and clicking the
    paginator-next N-1 times.
    """
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
SSRS_EXPORT_TOGGLE_SELECTORS = (
    'a[title="Export drop-down menu"]',
    'a[title*="drop-down" i]',
    'input[type="image"][title*="Export drop-down" i]',
    'a[title="Export"]',
    'a[title*="Export" i]',
    'input[type="image"][title*="Export" i]',
    'input[type="image"][title*="Save" i]',
    'a[title*="Save" i]',
)
SSRS_PDF_OPTION_SELECTORS = (
    'a[title="PDF"]',
    'a[title="Acrobat (PDF) file"]',
    'a[title*="PDF" i]',
    'a:has-text("PDF")',
    'td:has-text("PDF")',
)


def _try_legacy_ssrs_export_pdf(
    target_page: Page, save_dir: Path, file_stem: str
) -> Path | None:
    """
    Drive the SSRS ReportViewer toolbar on the legacy
    ReportSummaryForServicePlan.aspx popup.

    The toolbar has a floppy-disk "Export" icon with a dropdown
    arrow; clicking the arrow reveals a menu of formats. We pick
    "PDF" and capture the download.

    Returns the saved Path on success, or None to let the caller
    fall back to the generic `_try_download_on_page`.
    """
    target_page.wait_for_timeout(1_200)  # SSRS toolbar takes a moment

    toggle: Locator | None = None
    for sel in SSRS_EXPORT_TOGGLE_SELECTORS:
        loc = target_page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                toggle = loc
                break
        except Exception:  # noqa: BLE001
            continue
    if toggle is None:
        return None

    try:
        toggle.click()
    except Exception:  # noqa: BLE001
        return None
    target_page.wait_for_timeout(500)  # let the menu render

    pdf_link: Locator | None = None
    for sel in SSRS_PDF_OPTION_SELECTORS:
        loc = target_page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                pdf_link = loc
                break
        except Exception:  # noqa: BLE001
            continue
    if pdf_link is None:
        return None

    save_path = save_dir / f"{file_stem}.pdf"

    # SSRS PDF export usually triggers a real download (Content-
    # Disposition: attachment), but some installs serve it inline
    # and rely on the browser's "save as" prompt. Try expect_download
    # first; on timeout, fall back to capturing the response body.
    try:
        with target_page.expect_download(timeout=20_000) as dl_info:
            pdf_link.click()
        dl = dl_info.value
        dl.save_as(str(save_path))
        return save_path
    except PlaywrightTimeoutError:
        pass

    # Fallback: snag the PDF response by content-type or `.pdf` URL.
    try:
        with target_page.expect_response(
            lambda r: (
                "pdf" in (r.headers.get("content-type", "") or "").lower()
                or r.url.lower().endswith(".pdf")
                or "format=pdf" in r.url.lower()
            ),
            timeout=20_000,
        ) as resp_info:
            try:
                pdf_link.click()
            except Exception:  # noqa: BLE001
                pass
        resp = resp_info.value
        save_path.write_bytes(resp.body())
        return save_path
    except PlaywrightTimeoutError:
        return None



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
        if saved is not None:
            print(f"        + {saved.name} (legacy/new-tab)")
            return "saved"
        print(f"        ?? no download trigger on legacy popup for {file_stem}")
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
        return "failed"

    print(f"        ?? eye click had no observable effect for {file_stem}")
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
        lines.append(
            f"  {pos:0{pos_width}d}of{total}  aid={aid:<8s}  "
            f"status={status:<14s}  {state}"
        )
    lines.append("")
    status_path.write_text("\n".join(lines), encoding="utf-8")


def download_care_assessments_for_client(
    page: Page, client: Client, out_root: Path
) -> tuple[int, int, int]:
    """Returns (saved, skipped, failed) for this client."""
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
    #   _IN_PROGRESS_<Name>_<N>  -> at least one In-Progress row
    #                              (action icon is a TRASH can; row
    #                              is skipped to avoid deletion).
    #   _FLAG_<Name>_<N>         -> any other non-Completed row
    #                              (e.g. Pending, Submitted).
    #   <Name>_<N>               -> every row is Completed.
    # <N> is the total assessment count read from the live table,
    # so the user can tell at a glance whether the folder is
    # missing any files.
    status_bucket = (client.status or "unknown").strip().lower() or "unknown"
    base_label = name_to_folder(client.name)
    if has_in_progress:
        prefix = "_IN_PROGRESS_"
    elif has_non_completed:
        prefix = "_FLAG_"
    else:
        prefix = ""
    folder_label = f"{prefix}{base_label}_{total}" if total else f"{prefix}{base_label}"
    client_folder = out_root / status_bucket / folder_label

    # Migrate an older-naming folder if one exists (any prefix, with
    # or without the _<N> suffix). This carries already-downloaded
    # files into the new folder so resume-dedupe still works.
    if not client_folder.exists():
        legacy_candidates = [
            out_root / status_bucket / f"{p}{base_label}{suf}"
            for p in ("", "_FLAG_", "_IN_PROGRESS_")
            for suf in (("", f"_{total}") if total else ("",))
        ]
        for old in legacy_candidates:
            if old != client_folder and old.exists():
                try:
                    old.rename(client_folder)
                    print(f"    (migrated folder {old.name!r} -> {client_folder.name!r})")
                    break
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
        rows_on_page = rows_by_page[page_no]
        n_expected = len(rows_on_page)
        print(f"    -- inner page {page_no} ({n_expected} row(s)) --")

        # Drive by ROW INDEX, re-navigating to this inner page before
        # every row. This is brute-force but robust to whatever the
        # modern-flow page.go_back() does to inner-pagination state
        # (Angular sometimes resets to page 1, sometimes doesn't —
        # we don't have to care).
        for row_idx in range(n_expected):
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
            existing = list(client_folder.glob(f"{aid_part}*"))
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mass-download Care Assessment PDFs from Caresmartz360."
    )
    parser.add_argument(
        "--status",
        choices=["Active", "Inactive", "Both"],
        default="Both",
        help="Which status filter(s) to walk in the client list.",
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
    args = parser.parse_args()

    global _DEBUG_PAGINATOR
    _DEBUG_PAGINATOR = args.debug_paginator

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
                total_clients += 1
                print(
                    f"[client {total_clients}] {client.status}: "
                    f"{client.name} ({client.client_id})"
                )
                s, sk, f = download_care_assessments_for_client(page, client, args.out)
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

    print()
    print(f"[+] Clients processed : {total_clients}")
    print(f"    PDFs saved        : {total_saved}")
    print(f"    PDFs skipped      : {total_skipped}")
    print(f"    Failures          : {total_failed}")
    print(f"    Output folder     : {args.out}")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
