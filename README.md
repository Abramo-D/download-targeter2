# download-targeter2

Python + Playwright script that mass-downloads **Care Assessment PDFs**
from Caresmartz360.

**Two layers of pagination:**

1. Outer: the Client List, walked across both **Active** and
   **Inactive** status filters and every paginated page.
2. Inner: per-client Care Assessment table, also paginated. Every row's
   `chevron_right` action is clicked to capture whatever PDF comes
   down — either a direct download, or via a Download / Print button
   on the assessment detail page that the chevron opens.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env
# Open .env and fill in CARESMARTZ_USERNAME / CARESMARTZ_PASSWORD
```

## Run

```powershell
python main.py                          # both statuses, headed, -> care_assessment\
python main.py --status Active          # active only
python main.py --headless               # run hidden once selectors are stable
python main.py --out downloads          # change output folder
python main.py --limit 5                # only the first 5 clients (debug)
python main.py --client-id <guid>       # only one client by ID (debug)
```

## Output layout

```
care_assessment/
  Adams_Doreen/
    2024-12-15 Initial Assessment.pdf
    2025-03-02 Reassessment.pdf
  Bryant_John/
    2024-09-10 Initial Assessment.pdf
  ...
```

Files already on disk are skipped, so re-running resumes where it
stopped.

## Selectors at a glance

| Element                         | Selector                                                                     |
| ------------------------------- | ---------------------------------------------------------------------------- |
| Client name link                | `a[id^="fullname-"]`                                                         |
| Status filter trigger           | `[data-testid="cs-select-trigger"]`                                          |
| Filter option                   | `.cdk-overlay-pane` text match                                               |
| Care Assessment tab             | `.client-detail-component-nav-link:has-text("Care Assessment")`              |
| Row chevron                     | `span.material-icons-outlined:has-text("chevron_right")`                     |
| Paginator next                  | `button.mat-mdc-paginator-navigation-next`, `button[aria-label="Next page"]` |
| Post-navigation download button | any visible `Download` / `Print` / `PDF` / `Export` button or link           |

## Gotchas already handled

- **`cs-select` overlay**: never presses Escape between option click
  and the table repaint — Material discards the pending change.
- **Already-selected guard**: skips the dropdown click if the trigger
  already shows the desired status.
- **Pagination end detection**: uses both the next-button's
  `disabled`/`aria-disabled` attribute _and_ a row-swap watcher.
- **Resume**: each PDF's destination path is checked before clicking;
  existing files are silently skipped.
- **Two click outcomes** for the chevron — direct download vs. navigate
  to a detail page — are both handled by trying `expect_download`
  directly, then (if navigation happened) looking for a download
  button on the detail page and clicking that inside
  `expect_download` before `go_back`.

## If the site DOM changes

The fastest way to refresh selectors:

```powershell
playwright codegen https://hospall.caresmartz360.com/Login.aspx
```

Step through login → client click → Care Assessment tab → chevron →
whatever pops up. Copy the generated selectors over the constants at
the top of `main.py`.

## Debug recipe

For fastest iteration on the inner flow, pick one ClientID from a
manual browser session and:

```powershell
python main.py --client-id 55022624-8111-45c5-8fee-02efdd57fde0
```

The browser is headed by default, so you can watch every click. Once
the inner flow is reliable, drop `--client-id`, add `--headless`, and
let it walk the whole client base.
