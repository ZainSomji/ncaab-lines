"""One tracker update for GitHub Actions: save a snapshot of the Covers board,
rebuild the dashboard and write it to site/index.html for GitHub Pages."""
import shutil
import sys
from pathlib import Path

import ncaab_odds as n

HERE = Path(__file__).parent
ok = True
try:
    msg = n.clean_bad_history()
    if msg:
        n.log(msg)
except Exception as e:
    n.log(f"[warn] cleanup failed: {e}")
try:
    n.snapshot(n.SHARP_BOOKS, False)
except Exception as e:  # still rebuild the page so it shows how stale the data is
    n.log(f"[error] {e}")
    ok = False

import ncaab_dashboard as d  # noqa: E402

d.build_dashboard(open_browser=False)
html = d.DASHBOARD_PATH.read_text(encoding="utf-8").replace(
    "To load the newest lines, close this tab and double-click Open Dashboard again.",
    "This page updates about every 10-15 minutes. Refresh to see the latest.")
site = HERE / "site"
site.mkdir(exist_ok=True)
(site / "index.html").write_text(html, encoding="utf-8")
(site / ".nojekyll").write_text("")

# keep the log from growing forever
log = HERE / "tracker_log.txt"
if log.exists():
    lines = log.read_text(encoding="utf-8").splitlines()[-3000:]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

sys.exit(0 if ok else 1)
